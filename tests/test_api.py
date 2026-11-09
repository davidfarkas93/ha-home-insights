"""Read-only responses, permissions, date validation and current failure state."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.home_insights import api
from custom_components.home_insights.config_flow import HomeInsightsConfigFlow

from .conftest import config_entry
from .test_runtime import running_coordinator


async def test_daily_facts_action_is_response_only_and_reads_persisted_data(hass):
    coordinator = await running_coordinator(hass)
    try:
        await api.async_setup(hass)
        with patch.object(api, "coordinator", return_value=coordinator):
            result = await hass.services.async_call(
                "home_insights", "get_daily_facts", {}, blocking=True, return_response=True
            )
            assert result["assessment"] == "not_run"
            assert result["counts"] == {}
            assert "Luma custom rules" in str(result["limitations"])
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "home_insights", "get_daily_facts", {}, blocking=True
                )
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "home_insights",
                    "get_daily_facts",
                    {"date": "2099-01-01"},
                    blocking=True,
                    return_response=True,
                )
            hass.auth = SimpleNamespace(
                async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
            )
            with pytest.raises(Unauthorized):
                await hass.services.async_call(
                    "home_insights",
                    "get_daily_facts",
                    {},
                    blocking=True,
                    return_response=True,
                    context=Context(user_id="normal"),
                )
    finally:
        await coordinator.async_close()


async def test_websocket_read_api_requires_admin(hass):
    connection = SimpleNamespace(user=SimpleNamespace(is_admin=False), send_result=Mock())
    with pytest.raises(Unauthorized):
        api.ws_snapshot(hass, connection, {"id": 1, "type": "home_insights/snapshot"})
    assert not connection.send_result.called


def test_only_completed_calendar_dates_allowed():
    assert api.requested_day("2020-01-01", "Europe/Budapest") == date(2020, 1, 1)
    assert api.requested_day(None, "Europe/Budapest") is None
    with pytest.raises(ValueError):
        api.requested_day("2020-02-30", "Europe/Budapest")


async def test_config_flow_is_local_and_single_instance(hass):
    flow = HomeInsightsConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.flow_id = "test"
    flow.handler = "home_insights"
    result = await flow.async_step_user()
    assert result["type"] == "form"
    assert "token" not in str(result["data_schema"])
    entry = config_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    from homeassistant.data_entry_flow import AbortFlow

    with pytest.raises(AbortFlow, match="already_configured"):
        await flow.async_step_user({})
