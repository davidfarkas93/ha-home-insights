"""Native HA event capture, unload cleanup, startup and error semantics."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import CoreState, callback
from homeassistant.helpers import issue_registry

from custom_components.home_insights import adapters
from custom_components.home_insights.coordinator import Coordinator

from .conftest import config_entry


async def running_coordinator(hass, entities=None):
    hass.set_state(CoreState.running)
    entry = config_entry(data={"monitored_entities": entities or []})
    coordinator = Coordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_setup()
    await hass.async_block_till_done()
    await coordinator.async_flush()
    return coordinator


async def test_native_notifications_are_captured_without_browser_or_token(hass):
    coordinator = await running_coordinator(hass)
    try:
        assert coordinator.data["status"] == "recording"
        persistent_notification.async_create(hass, "Watering due", "Plants", "plants")
        persistent_notification.async_dismiss(hass, "plants")
        await coordinator.async_flush()
        history = await coordinator.async_history()
        kinds = [event["type"] for event in history["events"]]
        assert "source_appeared" in kinds
        assert "notification_removed" in kinds
        assert "source_cleared" not in kinds
        assert coordinator.data["open_count"] == 0
    finally:
        await coordinator.async_close()
    count = coordinator.data["event_count"]
    persistent_notification.async_create(hass, "After unload", "Ignored", "after")
    assert coordinator.queue.empty()
    assert coordinator.data["event_count"] == count


async def test_unknown_measurement_is_not_zero_or_healthy_and_attributes_are_minimal(hass):
    hass.states.async_set("sensor.test", "unavailable", {"access_token": "DO-NOT-STORE"})
    coordinator = await running_coordinator(hass, ["sensor.test"])
    try:
        assert coordinator.data["status"] == "partial"
        hass.states.async_set(
            "sensor.test", "12", {"unit_of_measurement": "W", "access_token": "DO-NOT-STORE"}
        )
        await hass.async_block_till_done()
        await coordinator.async_flush()
        assert coordinator.data["status"] == "recording"
        history = str(await coordinator.async_history())
        assert "DO-NOT-STORE" not in history
        assert "'state': '12'" in history
        assert "entity_state_changed" in history
    finally:
        await coordinator.async_close()


async def test_startup_waits_for_ha_running(hass):
    coordinator = Coordinator(hass, config_entry())
    await coordinator.async_setup()
    try:
        await hass.async_block_till_done()
        assert not coordinator.started
        assert coordinator.data["status"] == "starting"
        with pytest.raises(RuntimeError, match="not recording"):
            await coordinator.async_daily_facts()
    finally:
        await coordinator.async_close()


async def test_native_repair_and_translation_failure_preserve_source(hass):
    issue_registry.async_create_issue(
        hass,
        "example",
        "disk",
        is_fixable=False,
        severity=issue_registry.IssueSeverity.ERROR,
        translation_key="disk",
        translation_placeholders={"server": "Example server"},
    )
    coordinator = await running_coordinator(hass)
    try:
        assert coordinator.data["open_count"] == 1
        row = coordinator.data["open_episodes"][0]["body"]
        assert row["adapter"] == "repairs"
        assert row["facts"]["placeholders"]["server"] == "Example server"
        rows = adapters.repairs(hass)
        with patch.object(adapters, "async_get_translations", side_effect=RuntimeError("failed")):
            assert (await adapters.translate_repairs(hass, rows))[0].source_id == "example:disk"
        issue_registry.async_delete_issue(hass, "example", "disk")
        await hass.async_block_till_done()
        await coordinator.async_flush()
        assert coordinator.data["open_count"] == 0
        assert "repair_no_longer_reported" in str(await coordinator.async_history())
    finally:
        await coordinator.async_close()


async def test_disk_failure_is_visible_then_recovery_resynchronizes(hass):
    coordinator = await running_coordinator(hass)
    try:
        with patch.object(coordinator.journal, "checkpoint", side_effect=OSError("disk full")):
            coordinator.enqueue("checkpoint")
            await coordinator.async_flush()
        assert coordinator.failed
        with pytest.raises(RuntimeError):
            await coordinator.async_daily_facts()
        coordinator.enqueue("checkpoint")
        await coordinator.async_flush()
        assert not coordinator.failed
        assert "storage_write_failed" in str(await coordinator.async_history())
    finally:
        await coordinator.async_close()


async def test_bounded_queue_overflow_is_a_gap_not_silent_success(hass):
    coordinator = await running_coordinator(hass)
    try:
        # Enqueue synchronously, before the worker can consume the queue.
        for _ in range(coordinator.queue.maxsize + 10):
            coordinator.enqueue("checkpoint")
        await coordinator.async_flush()
        assert coordinator.data["status"] == "recording"
        assert "queue_overflow" in str(await coordinator.async_history())
    finally:
        await coordinator.async_close()


async def test_entity_adapter_does_not_call_device_services(hass):
    coordinator = await running_coordinator(hass, ["sensor.test"])
    calls = []

    @callback
    def forbidden(call):
        calls.append(call)

    hass.services.async_register("switch", "turn_on", forbidden)
    hass.services.async_register("update", "install", forbidden)
    try:
        hass.states.async_set("sensor.test", "0")
        await asyncio.sleep(0)
        await coordinator.async_flush()
        assert calls == []
    finally:
        await coordinator.async_close()


async def test_native_ha_stop_drains_and_closes_journal(hass):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    coordinator = await running_coordinator(hass)
    persistent_notification.async_create(hass, "Before stopping", "Test", "stop")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert coordinator.closed
    assert coordinator.data["status"] == "stopped"
    assert coordinator.queue.empty()


async def test_ignored_native_sources_are_not_open_errors_or_data_gaps(hass):
    from homeassistant.config_entries import SOURCE_IGNORE

    ignored = config_entry(domain="example", source=SOURCE_IGNORE)
    hass.config_entries._entries[ignored.entry_id] = ignored
    issue_registry.async_create_issue(
        hass,
        "example",
        "ignored",
        is_fixable=False,
        severity=issue_registry.IssueSeverity.WARNING,
        translation_key="ignored",
    )
    issue_registry.async_get(hass).async_ignore("example", "ignored", True)
    coordinator = await running_coordinator(hass)
    try:
        assert coordinator.data["status"] == "recording"
        assert coordinator.data["open_count"] == 0
        assert adapters.integrations(hass)[0].status == "excluded"
        assert adapters.repairs(hass)[0].status == "excluded"
    finally:
        await coordinator.async_close()
