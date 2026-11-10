"""Read-only journal APIs. No generic service, template, shell or SQL endpoint."""

import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import probatio as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .models import previous_day


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
            return await coordinator(hass).async_daily_facts(day, call.data.get("policy_id"))
        except (ValueError, RuntimeError) as error:
            raise HomeAssistantError(str(error)) from error

    hass.services.async_register(
        DOMAIN,
        "get_daily_facts",
        get_daily_facts,
        schema=vol.Schema({vol.Optional("date"): cv.string, vol.Optional("policy_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    async def set_attention_policy(call):
        await _require_admin(hass, call)
        try:
            return await coordinator(hass).attention.set_policy(
                call.data["policy_id"], call.data["policy"], call.data.get("expected_hash")
            )
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error

    hass.services.async_register(
        DOMAIN,
        "set_attention_policy",
        set_attention_policy,
        schema=vol.Schema(
            {
                vol.Required("policy_id"): cv.string,
                vol.Required("policy"): dict,
                vol.Optional("expected_hash"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    async def generate_report(call):
        await _require_admin(hass, call)
        try:
            day = requested_day(call.data.get("date"), hass.config.time_zone) or previous_day(
                time.time(), hass.config.time_zone
            )
            return await coordinator(hass).reports.generate(
                call.data["policy_id"],
                call.data["ai_task_entity"],
                day,
                call.context,
                call.data.get("force", False),
            )
        except (ValueError, RuntimeError) as error:
            raise HomeAssistantError(str(error)) from error

    hass.services.async_register(
        DOMAIN,
        "generate_report",
        generate_report,
        schema=vol.Schema(
            {
                vol.Required("policy_id"): cv.string,
                vol.Required("ai_task_entity"): cv.entity_domain("ai_task"),
                vol.Optional("date"): cv.string,
                vol.Optional("force", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    for command in (
        ws_snapshot,
        ws_history,
        ws_daily_facts,
        ws_attention,
        ws_episode,
        ws_handle,
        ws_report,
    ):
        websocket_api.async_register_command(hass, command)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "home_insights/report",
        vol.Required("policy_id"): str,
        vol.Optional("due_time", default="08:00"): str,
        vol.Optional("grace_minutes", default=10): vol.All(int, vol.Range(min=0, max=60)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_report(hass, connection, msg):
    try:
        result = await coordinator(hass).reports.read(
            msg["policy_id"], msg["due_time"], msg["grace_minutes"]
        )
    except ValueError as error:
        connection.send_error(msg["id"], "report_unavailable", str(error))
    else:
        connection.send_result(msg["id"], result)


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
        vol.Optional("policy_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_daily_facts(hass, connection, msg):
    try:
        day = requested_day(msg.get("date"), hass.config.time_zone)
        result = await coordinator(hass).async_daily_facts(day, msg.get("policy_id"))
    except (ValueError, RuntimeError) as error:
        connection.send_error(msg["id"], "invalid_request", str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "home_insights/episode",
        vol.Required("policy_id"): str,
        vol.Required("episode_id"): str,
        vol.Optional("before"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("limit", default=20): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
    }
)
@websocket_api.async_response
async def ws_episode(hass, connection, msg):
    try:
        result = await coordinator(hass).attention.episode(
            msg["policy_id"], msg["episode_id"], connection.user, msg.get("before"), msg["limit"]
        )
    except ValueError as error:
        connection.send_error(msg["id"], "episode_unavailable", str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "home_insights/record_handling",
        vol.Required("policy_id"): str,
        vol.Required("request_id"): str,
        vol.Required("operation"): vol.In(["seen", "claimed_fixed", "snooze", "unsnooze", "note"]),
        vol.Required("targets"): [
            {
                vol.Required("row_id"): str,
                vol.Required("episode_id"): str,
                vol.Required("expected_revision"): vol.All(int, vol.Range(min=0)),
            }
        ],
        vol.Optional("until"): vol.Coerce(float),
        vol.Optional("note", default=""): str,
    }
)
@websocket_api.async_response
async def ws_handle(hass, connection, msg):
    try:
        result = await coordinator(hass).attention.handle(
            {k: v for k, v in msg.items() if k not in ("id", "type")}, connection.user
        )
    except ValueError as error:
        connection.send_error(msg["id"], "handling_conflict", str(error))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "home_insights/attention",
        vol.Required("policy_id"): str,
        vol.Optional("language", default="en"): vol.In(["en", "hu"]),
        vol.Optional("debug", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_attention(hass, connection, msg):
    if connection.user is None or msg["debug"] and not connection.user.is_admin:
        raise Unauthorized()
    try:
        result = await coordinator(hass).attention.read(
            msg["policy_id"], msg["language"], connection.user, msg["debug"]
        )
    except ValueError as error:
        connection.send_error(msg["id"], "attention_unavailable", str(error))
    else:
        connection.send_result(msg["id"], result)
