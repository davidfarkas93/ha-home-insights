"""Read-only journal APIs. No generic service, template, shell or SQL endpoint."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN


def coordinator(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data and not entry.runtime_data.closed:
            return entry.runtime_data
    raise HomeAssistantError("Home Insights is not loaded")


def requested_day(value: str | None, zone: str) -> date | None:
    if value is None:
        return None
    day = date.fromisoformat(value)
    if day >= datetime.now(ZoneInfo(zone)).date():
        raise ValueError("Only completed local calendar days are supported")
    return day


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    # Native automations have no user context. AI receives only the response data.
    if call.context.user_id is not None:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None or not user.is_admin:
            raise Unauthorized()


async def async_setup(hass: HomeAssistant) -> None:
    async def get_daily_facts(call: ServiceCall):
        await _require_admin(hass, call)
        try:
            day = requested_day(call.data.get("date"), hass.config.time_zone)
            return await coordinator(hass).async_daily_facts(day)
        except (ValueError, RuntimeError) as error:
            raise HomeAssistantError(str(error)) from error

    hass.services.async_register(
        DOMAIN,
        "get_daily_facts",
        get_daily_facts,
        schema=vol.Schema({vol.Optional("date"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    for command in (ws_snapshot, ws_history, ws_daily_facts):
        websocket_api.async_register_command(hass, command)


@websocket_api.websocket_command({vol.Required("type"): "home_insights/snapshot"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_snapshot(hass, connection, msg):
    current = coordinator(hass)
    await current.async_flush()
    connection.send_result(
        msg["id"], {**current.data, "status": "error" if current.failed else current.data["status"]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "home_insights/history",
        vol.Optional("before"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("limit", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_history(hass, connection, msg):
    result = await coordinator(hass).async_history(msg.get("before"), msg["limit"])
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "home_insights/daily_facts",
        vol.Optional("date"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_daily_facts(hass, connection, msg):
    try:
        day = requested_day(msg.get("date"), hass.config.time_zone)
        result = await coordinator(hass).async_daily_facts(day)
    except (ValueError, RuntimeError) as error:
        connection.send_error(msg["id"], "invalid_request", str(error))
    else:
        connection.send_result(msg["id"], result)
