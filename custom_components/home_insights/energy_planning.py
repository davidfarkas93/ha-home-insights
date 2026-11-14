"""A dated, expiring forecast opportunity; never a charging command or surplus claim."""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .models import previous_day


def numeric(state, converter, unit):
    if (
        state is None
        or state.attributes.get("restored")
        or state.attributes.get("dataCorrect") is False
    ):
        return None
    try:
        value = float(state.state)
        if not math.isfinite(value) or not 0 <= value <= 100000:
            return None
        source_unit = state.attributes.get("unit_of_measurement")
        if source_unit not in converter.VALID_UNITS:
            return None
        return round(converter.convert(value, source_unit, unit), 3)
    except ValueError, TypeError, OverflowError:
        return None


def timestamp(state):
    if state is None or state.attributes.get("restored"):
        return None
    try:
        value = datetime.fromisoformat(state.state)
        return value.timestamp() if value.tzinfo is not None else None
    except ValueError, TypeError, OverflowError:
        return None


def collect(hass, config, report_day, now):
    zone = hass.config.time_zone
    local = datetime.fromtimestamp(now, ZoneInfo(zone))
    if report_day != previous_day(now, zone):
        return None  # Today's forecast never rewrites a historical day's facts.
    states = {
        key: hass.states.get(config[key])
        for key in (
            "forecast_entity",
            "remaining_entity",
            "peak_power_entity",
            "peak_time_entity",
            "updated_entity",
        )
    }
    result = {
        "day": local.date().isoformat(),
        "timezone": zone,
        "saved_at": now,
        "status": "insufficient_data",
        "reason": "missing_or_invalid_forecast",
        "forecast_kwh": numeric(states["forecast_entity"], EnergyConverter, "kWh"),
        "remaining_kwh": numeric(states["remaining_entity"], EnergyConverter, "kWh"),
        "peak_kw": numeric(states["peak_power_entity"], PowerConverter, "kW"),
        "peak_at": timestamp(states["peak_time_entity"]),
        "forecast_updated_at": timestamp(states["updated_entity"]),
        "minimum_charging_kw": numeric(
            hass.states.get(config["minimum_charging_power_entity"]), PowerConverter, "kW"
        )
        if config.get("minimum_charging_power_entity")
        else None,
        "sources": {k: config[k] for k in states},
        "thresholds": {k: config[k] for k in ("minimum_remaining_kwh", "minimum_peak_kw")},
        "allow_grid_assist": config["allow_grid_assist"],
        "valid_until": None,
        "surplus_known": False,
        "charging_need_known": False,
    }
    if any(
        result[k] is None
        for k in ("forecast_kwh", "remaining_kwh", "peak_kw", "peak_at", "forecast_updated_at")
    ):
        return result
    updated, peak = result["forecast_updated_at"], result["peak_at"]
    if (
        not 0 <= now - updated <= config["maximum_age_hours"] * 3600
        or abs(peak - now) > 36 * 3600
        or datetime.fromtimestamp(updated, ZoneInfo(zone)).date() != local.date()
        or datetime.fromtimestamp(peak, ZoneInfo(zone)).date() != local.date()
    ):
        return {**result, "reason": "stale_or_wrong_day"}
    # A recent poll cannot make an older/restored component reading fresh.
    if any(
        s is None
        or s.attributes.get("restored")
        or s.last_updated.timestamp() > now + 60
        or datetime.fromtimestamp(s.last_updated.timestamp(), ZoneInfo(zone)).date() != local.date()
        for s in states.values()
    ):
        return {**result, "reason": "stale_source"}
    if result["remaining_kwh"] > result["forecast_kwh"]:
        return {**result, "reason": "inconsistent_remaining_energy"}
    midnight = datetime.combine(
        local.date() + timedelta(days=1), datetime.min.time(), ZoneInfo(zone)
    ).timestamp()
    until = min(midnight, updated + config["maximum_age_hours"] * 3600, peak + 2 * 3600)
    result["valid_until"] = until
    if until <= now:
        return {**result, "status": "elapsed", "reason": "planning_window_elapsed"}
    opportunity = (
        result["remaining_kwh"] >= config["minimum_remaining_kwh"]
        and result["peak_kw"] >= config["minimum_peak_kw"]
    )
    # Gross PV power does not establish available surplus, even above charger minimum.
    if not config["allow_grid_assist"]:
        return {**result, "status": "insufficient_data", "reason": "verified_surplus_required"}
    return {
        **result,
        "status": "opportunity" if opportunity else "no_opportunity",
        "reason": "forecast_thresholds_met" if opportunity else "below_advisory_thresholds",
    }


def narrative(planning):
    if planning is None:
        return None
    ready = planning["status"] == "opportunity"
    return {
        "day": planning["day"],
        "saved_at": planning["saved_at"],
        "valid_until": planning["valid_until"],
        "status": planning["status"],
        "headline": (
            "Kedvező napelemtermelés várható; ha töltenéd az autót, érdemes napközbenre időzíteni."
        )
        if ready
        else "",
        "summary": (
            "Ha ma töltenéd az autót, érdemes napközbenre időzíteni: "
            "kedvező napelemtermelés várható. A ház fogyasztásától függően "
            "hálózati rásegítésre is szükség lehet."
        )
        if ready
        else "A mentett előrejelzésből most nem adható megalapozott töltési javaslat."
        if planning["status"] == "insufficient_data"
        else "A mentett töltési javaslat időablaka már elmúlt."
        if planning["status"] == "elapsed"
        else "A mentett előrejelzés alapján nincs kiemelendő töltési lehetőség.",
        "limitation": (
            "A napelemtermelésből a ház is fogyaszt. A szabad többlet és az autó "
            "töltési igénye ebből nem állapítható meg."
        ),
        "evidence_refs": ["today_forecast"] if ready else [],
    }
