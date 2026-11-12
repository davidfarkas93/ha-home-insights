"""Policy import persistence, canonical episodes, permissions and native HA changes."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import Unauthorized

from custom_components.home_insights.models import digest

from .test_runtime import running_coordinator

POLICY = {
    "version": 1,
    "revision": "test.1",
    "rules": [
        {"id": "warm", "entity": "sensor.temperature", "above": 42, "message": "Warm"},
        {"id": "hidden", "entity": "sensor.private", "state": "on", "message": "Private"},
    ],
}


async def test_policy_runs_without_browser_and_keeps_episodes_after_reload(hass):
    hass.states.async_set("sensor.temperature", "44")
    coordinator = await running_coordinator(hass)
    try:
        result = await coordinator.attention.set_policy("example", POLICY)
        assert result["policy_hash"] == digest(POLICY)
        first = await coordinator.attention.read(
            "example", "hu", SimpleNamespace(is_admin=True), True
        )
        active = next(r for r in first["rows"] if r["rule"] == "warm")
        assert active["status"] == "active"
        episode = active["episodeId"]
        hass.states.async_set("sensor.temperature", "40")
        await hass.async_block_till_done()
        await coordinator.async_flush()
        assert (
            next(
                r for r in coordinator.attention.results["example"]["rows"] if r["rule"] == "warm"
            )["status"]
            == "inactive"
        )
        hass.states.async_set("sensor.temperature", "44")
        await hass.async_block_till_done()
        await coordinator.async_flush()
        current = next(
            r for r in coordinator.attention.results["example"]["rows"] if r["rule"] == "warm"
        )
        assert current["episodeId"] != episode
        assert current["key"] != active["key"]
        with pytest.raises(ValueError, match="Policy changed"):
            await coordinator.attention.set_policy("example", POLICY)
        assert "attention_started" in str(await coordinator.async_history(limit=100))
        user = SimpleNamespace(
            is_admin=False,
            permissions=SimpleNamespace(
                check_entity=lambda identifier, _: identifier != "sensor.private"
            ),
        )
        visible = await coordinator.attention.read("example", "en", user)
        assert all(r["source"] != "sensor.private" for r in visible["rows"])
        assert "contract_input" not in visible
    finally:
        await coordinator.async_close()
    second = await running_coordinator(hass, entry=coordinator.entry)
    try:
        assert second.attention.policies["example"] == POLICY
        result = await second.attention.read("example")
        assert (
            next(r for r in result["rows"] if r["rule"] == "warm")["episodeId"]
            == current["episodeId"]
        )
    finally:
        await second.async_close()


async def test_invalid_import_does_not_replace_valid_policy(hass):
    coordinator = await running_coordinator(hass)
    try:
        await coordinator.attention.set_policy("example", POLICY)
        with pytest.raises(ValueError):
            await coordinator.attention.set_policy(
                "example", {**POLICY, "execute": "shell"}, digest(POLICY)
            )
        assert coordinator.attention.policies["example"] == POLICY
    finally:
        await coordinator.async_close()


async def test_restart_read_preserves_snooze_during_cached_clearance(hass):
    hass.states.async_set("sensor.temperature", "44")
    owner = await running_coordinator(hass)
    admin = SimpleNamespace(id="user", name="User", is_admin=True)
    await owner.attention.set_policy("example", POLICY)
    first = await owner.attention.read("example", user=admin)
    row = next(r for r in first["rows"] if r["rule"] == "warm")
    until = time.time() + 3600
    await owner.attention.handle(
        {
            "policy_id": "example",
            "request_id": "snooze",
            "operation": "snooze",
            "until": until,
            "targets": [
                {"row_id": row["id"], "episode_id": row["episodeId"], "expected_revision": 0}
            ],
        },
        admin,
    )
    entry = owner.entry
    await owner.async_close()
    hass.states.async_set("sensor.temperature", "40")
    owner = await running_coordinator(hass, entry=entry)
    try:
        current = await owner.attention.read("example", user=admin)
        pending = next(r for r in current["rows"] if r["rule"] == "warm")
        assert pending["status"] == "loading" and pending["episodeId"] == row["episodeId"]
        hass.states.async_set("sensor.temperature", "44")
        await hass.async_block_till_done()
        current = await owner.attention.read("example", user=admin)
        active = next(r for r in current["rows"] if r["rule"] == "warm")
        assert active["episodeId"] == row["episodeId"] and active["key"] == row["key"]
        assert active["snoozedUntil"] == until
    finally:
        await owner.async_close()


async def test_metadata_is_audited_authorized_and_never_calls_source_services(hass):
    hass.states.async_set("sensor.temperature", "44")
    coordinator = await running_coordinator(hass)
    admin = SimpleNamespace(id="real-user", name="Real User", is_admin=True)
    try:
        await coordinator.attention.set_policy("example", POLICY)
        first = await coordinator.attention.read("example", user=admin)
        active = next(r for r in first["rows"] if r["rule"] == "warm")
        value = {
            "policy_id": "example",
            "request_id": "test",
            "operation": "claimed_fixed",
            "targets": [
                {"row_id": active["id"], "episode_id": active["episodeId"], "expected_revision": 0}
            ],
            "note": "Checked the wiring",
        }
        assert active["canHandle"]
        with patch.object(type(hass.services), "async_call", new_callable=AsyncMock) as services:
            assert (await coordinator.attention.handle(value, admin))["ok"]
            services.assert_not_called()
        after = await coordinator.attention.read("example", user=admin)
        current = next(r for r in after["rows"] if r["rule"] == "warm")
        assert current["status"] == "active" and current["handling"]["claimed_fixed_at"]
        detail = await coordinator.attention.episode("example", active["episodeId"], admin)
        assert detail["events"][0]["actor_name"] == "Real User"
        assert detail["events"][0]["note"] == "Checked the wiring"
        readonly = SimpleNamespace(
            id="reader",
            name="Reader",
            is_admin=False,
            permissions=SimpleNamespace(
                check_entity=lambda entity, permission: (
                    entity == "sensor.temperature" and permission == "read"
                )
            ),
        )
        visible = await coordinator.attention.read("example", user=readonly)
        assert not next(r for r in visible["rows"] if r["rule"] == "warm")["canHandle"]
        assert (await coordinator.attention.episode("example", active["episodeId"], readonly))[
            "episode_id"
        ] == active["episodeId"]
        with pytest.raises(Unauthorized):
            await coordinator.attention.handle({**value, "request_id": "reader"}, readonly)
        denied = SimpleNamespace(
            is_admin=False, permissions=SimpleNamespace(check_entity=lambda *_: False)
        )
        with pytest.raises(Unauthorized):
            await coordinator.attention.episode("example", active["episodeId"], denied)
        with pytest.raises(Unauthorized):
            await coordinator.attention.episode("example", active["episodeId"], None)
    finally:
        await coordinator.async_close()
