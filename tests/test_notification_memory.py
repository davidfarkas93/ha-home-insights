"""Real HA dismiss services, replay, changed content and observed condition reset."""

from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import Context

from custom_components.home_insights import adapters, notification_memory
from custom_components.home_insights.policy import validate_policy

from .test_runtime import running_coordinator

BINDING = {"notification_id": "robot-check", "entity": "sensor.robot_check", "state": "rain"}
POLICY = {
    "version": 1,
    "revision": "memory.1",
    "rules": [],
    "adapters": {
        "notifications": True,
        "notification_acknowledgements": [BINDING],
    },
}


async def setup(hass):
    await persistent_notification.async_setup(hass, {})
    hass.states.async_set("sensor.robot_check", "rain")
    owner = await running_coordinator(hass)
    await owner.attention.set_policy("home", POLICY)
    owner.notification_memory.settle_until = 0
    return owner


async def flush(hass, owner):
    await hass.async_block_till_done()
    await owner.async_flush()


async def dismiss(hass, owner):
    await hass.services.async_call(
        "persistent_notification",
        "dismiss",
        {
            "notification_id": "robot-check",
        },
        blocking=True,
        context=Context(user_id="actual-user"),
    )
    await flush(hass, owner)


async def test_user_dismissal_survives_restart_but_changed_content_is_visible(hass):
    owner = await setup(hass)
    persistent_notification.async_create(hass, "Rain protection", "Robot", "robot-check")
    await flush(hass, owner)
    await dismiss(hass, owner)
    assert (
        await owner.execute(
            lambda: owner.journal.db.execute(
                "SELECT actor FROM notification_acknowledgements"
            ).fetchone()[0]
        )
        == "actual-user"
    )
    entry = owner.entry
    await owner.async_close()
    persistent_notification.async_create(hass, "Rain protection", "Robot", "robot-check")
    owner = await running_coordinator(hass, entry=entry)
    try:
        await flush(hass, owner)
        assert not adapters.notifications(hass)
        assert "notification_redelivery_suppressed" in str(await owner.async_history())
        persistent_notification.async_create(hass, "Boundary safety issue", "Robot", "robot-check")
        await flush(hass, owner)
        assert adapters.notifications(hass)[0].message == "Boundary safety issue"
        persistent_notification.async_create(hass, "Rain protection", "Robot", "robot-check")
        await flush(hass, owner)
        assert adapters.notifications(hass)[0].message == "Rain protection", (
            "changed content resets the old acknowledgement"
        )
    finally:
        await owner.async_close()


async def test_real_clearance_resets_memory_and_unknown_does_not_hide_notices(hass):
    owner = await setup(hass)
    try:
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        await dismiss(hass, owner)
        hass.states.async_set("sensor.robot_check", "unavailable")
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        assert adapters.notifications(hass), "uncertain context cannot suppress a notice"
        hass.states.async_set("sensor.robot_check", "normal")
        await flush(hass, owner)
        hass.states.async_set("sensor.robot_check", "rain")
        await flush(hass, owner)
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        assert adapters.notifications(hass), "a genuine recurrence must be visible"
    finally:
        await owner.async_close()


async def test_unbound_explicit_dismiss_is_audited_without_suppressing_redelivery(hass):
    owner = await setup(hass)
    try:
        persistent_notification.async_create(hass, "Check details", "Other", "other-notice")
        await flush(hass, owner)
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": "other-notice"},
            blocking=True,
            context=Context(user_id="actual-user"),
        )
        await flush(hass, owner)
        history = await owner.async_history()
        assert any(
            e["type"] == "notification_user_dismissed"
            and e["body"]["notification_id"] == "other-notice"
            for e in history["events"]
        )
        persistent_notification.async_create(hass, "Check details", "Other", "other-notice")
        await flush(hass, owner)
        assert any(r.source_id == "other-notice" for r in adapters.notifications(hass))
    finally:
        await owner.async_close()


async def test_producer_removal_and_automation_dismissal_do_not_acknowledge(hass):
    owner = await setup(hass)
    try:
        for user in (None, "producer", "automation"):
            persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
            await flush(hass, owner)
            if user is None:
                await hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": "robot-check"},
                    blocking=True,
                )
            elif user == "automation":
                await hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": "robot-check"},
                    blocking=True,
                    context=Context(user_id="actual-user", parent_id="automation-context"),
                )
            else:
                persistent_notification.async_dismiss(hass, "robot-check")
            await flush(hass, owner)
        persistent_notification.async_create(hass, "Unbound", "Other", "other-notice")
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": "other-notice"},
            blocking=True,
            context=Context(user_id="actual-user"),
        )
        await flush(hass, owner)
        assert (
            await owner.execute(
                lambda: owner.journal.db.execute(
                    "SELECT count(*) FROM notification_acknowledgements"
                ).fetchone()[0]
            )
            == 0
        )
    finally:
        await owner.async_close()


async def test_failed_service_cannot_remember_a_later_producer_removal(hass):
    owner = await setup(hass)
    try:
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        with patch(
            "homeassistant.components.persistent_notification.services.async_dismiss",
            side_effect=RuntimeError("failed"),
        ):
            with pytest.raises(RuntimeError):
                await dismiss(hass, owner)
        await flush(hass, owner)
        persistent_notification.async_dismiss(hass, "robot-check")
        await flush(hass, owner)
        assert (
            await owner.execute(
                lambda: owner.journal.db.execute(
                    "SELECT count(*) FROM notification_acknowledgements"
                ).fetchone()[0]
            )
            == 0
        )
    finally:
        await owner.async_close()


def test_bindings_are_explicit_bounded_and_cannot_match_unknown():
    validate_policy(POLICY)
    for binding in (
        {**BINDING, "state": "unknown"},
        {**BINDING, "entity": "switch.robot"},
        {**BINDING, "extra": True},
    ):
        with pytest.raises(ValueError):
            validate_policy({**POLICY, "adapters": {"notification_acknowledgements": [binding]}})


async def test_startup_context_blip_does_not_erase_dismissal(hass):
    owner = await setup(hass)
    try:
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        await dismiss(hass, owner)
        owner.notification_memory.settle_until = float("inf")
        hass.states.async_set("sensor.robot_check", "normal")
        await flush(hass, owner)
        hass.states.async_set("sensor.robot_check", "rain")
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        assert not adapters.notifications(hass)
    finally:
        await owner.async_close()


async def test_dismiss_all_remembers_only_the_explicit_condition_binding(hass):
    owner = await setup(hass)
    try:
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        persistent_notification.async_create(hass, "Backup failed", "Backup", "backup")
        await flush(hass, owner)
        await hass.services.async_call(
            "persistent_notification",
            "dismiss_all",
            {},
            blocking=True,
            context=Context(user_id="actual-user"),
        )
        await flush(hass, owner)
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        persistent_notification.async_create(hass, "Backup failed", "Backup", "backup")
        await flush(hass, owner)
        assert [r.source_id for r in adapters.notifications(hass)] == ["backup"]
    finally:
        await owner.async_close()


async def test_changed_notice_during_storage_read_is_never_dismissed(hass):
    owner = await setup(hass)
    try:
        persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
        await flush(hass, owner)
        await dismiss(hass, owner)
        execute = owner.execute

        async def raced(function, *args):
            result = await execute(function, *args)
            if function is notification_memory.match_and_reset and result:
                persistent_notification.async_create(
                    hass, "New safety issue", "Robot", "robot-check"
                )
            return result

        with patch.object(owner, "execute", side_effect=raced):
            persistent_notification.async_create(hass, "Rain", "Robot", "robot-check")
            await flush(hass, owner)
        assert adapters.notifications(hass)[0].message == "New safety issue"
    finally:
        await owner.async_close()
