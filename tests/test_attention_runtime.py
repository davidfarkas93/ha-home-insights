"""Policy import persistence, canonical episodes, permissions and native HA changes."""

from types import SimpleNamespace

import pytest

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
