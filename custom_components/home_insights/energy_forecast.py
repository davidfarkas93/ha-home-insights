"""A fixed local-time forecast snapshot, not a hindsight forecast or causal diagnosis."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from homeassistant.util.unit_conversion import EnergyConverter

from .models import iso


def snapshot(rows, config, day, zone):
    cutoff = datetime.combine(
        day, time.fromisoformat(config["snapshot_time"]), ZoneInfo(zone)
    ).timestamp()
    eligible = []
    for row in rows:
        at = row.last_updated.timestamp()
        if (
            datetime.fromtimestamp(at, ZoneInfo(zone)).date() == day
            and at <= cutoff
            and cutoff - at <= config["maximum_age_hours"] * 3600
        ):
            eligible.append(row)
    base = {
        "source": config["entity"],
        "cutoff_at": iso(cutoff),
        "value_kwh": None,
        "recorded_at": None,
        "status": "missing_snapshot",
        "comparison_basis": "independent_forecast",
    }
    if not eligible:
        return base
    row = max(eligible, key=lambda s: s.last_updated.timestamp())
    at = row.last_updated.timestamp()
    if sum(s.last_updated.timestamp() == at for s in eligible) != 1:
        return {**base, "status": "ambiguous_snapshot"}
    try:
        value = float(row.state)
        unit = row.attributes.get("unit_of_measurement")
        if (
            row.attributes.get("restored")
            or unit not in EnergyConverter.VALID_UNITS
            or not 0 <= value <= 100000
        ):
            return base
        value = EnergyConverter.convert(value, unit, "kWh")
    except ValueError, TypeError:
        return base
    return {**base, "status": "available", "value_kwh": round(value, 3), "recorded_at": iso(at)}
