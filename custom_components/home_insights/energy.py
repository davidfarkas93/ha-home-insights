"""Bounded, read-only Recorder energy measurements for completed local days."""

import asyncio
import math
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from statistics import median

import probatio as vol
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from . import energy_forecast, energy_planning
from .models import daily_window, digest, iso

METRICS = ("solar", "grid_import", "grid_export", "ev")
NAMES = {
    "solar": "Napelemtermelés",
    "grid_import": "Hálózati import",
    "grid_export": "Hálózati export",
    "ev": "Autótöltés",
}
PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("version"): vol.Equal(1),
        vol.Required("revision"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Required("metrics"): {
            vol.Required(k): vol.Match(r"^sensor\.[a-z0-9_]+$") for k in METRICS
        },
        vol.Optional("solar_name"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Optional("charging_planning"): {
            **{
                vol.Required(k): vol.Match(r"^sensor\.[a-z0-9_]+$")
                for k in (
                    "forecast_entity",
                    "remaining_entity",
                    "peak_power_entity",
                    "peak_time_entity",
                    "updated_entity",
                )
            },
            vol.Optional("minimum_charging_power_entity"): vol.Match(r"^sensor\.[a-z0-9_]+$"),
            vol.Required("allow_grid_assist"): bool,
            vol.Required("minimum_remaining_kwh"): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=1000)
            ),
            vol.Required("minimum_peak_kw"): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=1000)
            ),
            vol.Optional("maximum_age_hours", default=6): vol.All(int, vol.Range(min=1, max=12)),
        },
        vol.Optional("solar_forecast"): {
            vol.Required("entity"): vol.Match(r"^sensor\.[a-z0-9_]+$"),
            vol.Optional("snapshot_time", default="06:00"): vol.Match(r"^([01]\d|2[0-3]):[0-5]\d$"),
            vol.Optional("maximum_age_hours", default=6): vol.All(int, vol.Range(min=1, max=12)),
        },
        vol.Optional("baseline_days", default=14): vol.All(int, vol.Range(min=7, max=28)),
        vol.Optional("minimum_baseline_days", default=7): vol.All(int, vol.Range(min=7, max=28)),
        vol.Optional("deviation_percent", default=50): vol.All(
            vol.Coerce(float), vol.Range(min=10, max=500)
        ),
        vol.Optional("deviation_kwh", default=3): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=1000)
        ),
    }
)


def validate_profile(value):
    try:
        profile = PROFILE_SCHEMA(deepcopy(value))
    except (vol.Invalid, TypeError) as error:
        raise ValueError("Invalid energy profile") from error
    if (
        len(set(profile["metrics"].values())) != len(METRICS)
        or profile["minimum_baseline_days"] > profile["baseline_days"]
        or any(not math.isfinite(profile[k]) for k in ("deviation_percent", "deviation_kwh"))
        or any(
            not math.isfinite(v)
            for k, v in profile.get("charging_planning", {}).items()
            if k in ("minimum_remaining_kwh", "minimum_peak_kw")
        )
    ):
        raise ValueError("Invalid energy profile")
    return profile


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measured_day(rows, start, end):
    """Require every UTC hour and the preceding sum; never bridge a missing bucket."""
    expected = int((end - start) / 3600)
    empty = {"value": None, "hours": 0, "expected_hours": expected, "reason": "missing_statistics"}
    if start % 3600 or end % 3600:
        return {**empty, "reason": "unsupported_midnight_alignment"}
    indexes = {}
    for row in rows:
        at = row.get("start")
        if finite(at) and start - 3600 <= at < end:
            if at in indexes:
                return {**empty, "reason": "duplicate_statistics"}
            indexes[at] = row
    valid = {}
    for at, row in indexes.items():
        if (
            at % 3600 == 0
            and finite(row.get("sum"))
            and finite(row.get("state"))
            and row.get("end") == at + 3600
        ):
            valid[at] = row["sum"]
    count = sum(start + i * 3600 in valid for i in range(expected))
    result = {**empty, "hours": count}
    times = [start - 3600 + i * 3600 for i in range(expected + 1)]
    if not all(t in valid for t in times):
        return result
    if any(valid[b] - valid[a] < -0.000001 for a, b in zip(times, times[1:], strict=False)):
        return {**result, "reason": "negative_statistics_change"}
    return {
        **result,
        "value": round(max(0, valid[times[-1]] - valid[times[0]]), 3),
        "reason": None,
    }


def history_quality(rows, start, end):
    """History is independent of statistics: report unknown boundaries, not zero use."""
    points = sorted(
        (s.last_updated.timestamp(), s.state) for s in rows if s.last_updated.timestamp() < end
    )

    def numeric(value):
        try:
            return math.isfinite(float(value)) and float(value) >= 0
        except ValueError, TypeError:
            return False

    previous = None
    cursor = start
    unknown = 0.0
    initial = False
    for at, value in points:
        if at <= start:
            previous = value
            initial = numeric(value)
            continue
        if not numeric(previous):
            unknown += at - cursor
        cursor, previous = at, value
    if not numeric(previous):
        unknown += end - cursor
    return {
        "boundary_states_known": initial and numeric(previous),
        "unavailable_seconds": round(unknown, 3),
        "status": "known_boundaries" if initial and numeric(previous) else "unknown_boundaries",
    }


def compute_facts(profile, profile_id, day, zone, statistics, metadata, histories):
    start, end = daily_window(day, zone)
    values = []
    baseline_days = profile["baseline_days"]
    for key in METRICS:
        source = profile["metrics"][key]
        meta = metadata.get(source)
        supported = bool(
            meta
            and meta[1].get("has_sum")
            and meta[1].get("unit_of_measurement") in EnergyConverter.VALID_UNITS
        )
        rows = statistics.get(source, []) if supported else []
        measured = measured_day(rows, start, end)
        quality = history_quality(histories.get(source, []), start, end)
        baseline = []
        for ago in range(1, baseline_days + 1):
            a, b = daily_window(day - timedelta(days=ago), zone)
            historic = measured_day(rows, a, b)
            if historic["value"] is not None:
                baseline.append(
                    {"day": (day - timedelta(days=ago)).isoformat(), "value": historic["value"]}
                )
        enough = len(baseline) >= profile["minimum_baseline_days"]
        reference = round(median(v["value"] for v in baseline), 3) if enough else None
        # Statistical totals may still be useful during recovered internal outages.
        # Unknown boundaries cannot establish a complete day's measured consumption.
        status = (
            "unsupported_statistic"
            if not supported
            else measured["reason"]
            or (None if quality["boundary_states_known"] else "unknown_history_boundaries")
        )
        current = measured["value"]
        delta = (
            round(current - reference, 3) if current is not None and reference is not None else None
        )
        percent = round(100 * delta / reference, 1) if delta is not None and reference > 0 else None
        deviation = None
        if not status and delta is not None:
            larger = abs(delta) >= profile["deviation_kwh"]
            relative = (
                reference == 0 or abs(delta) >= reference * profile["deviation_percent"] / 100
            )
            if larger and relative:
                if key == "solar" and delta < 0:
                    deviation = "below_recent_days"
                elif key == "grid_import" and delta > 0:
                    deviation = "above_recent_days"
        values.append(
            {
                "key": key,
                "name": profile.get("solar_name", NAMES[key]) if key == "solar" else NAMES[key],
                "source": source,
                "value": current if not status else None,
                "recorded_delta_kwh": current,
                "unit": "kWh",
                "status": status or "complete",
                "statistics": {k: v for k, v in measured.items() if k != "value"},
                "history": quality,
                "baseline": {
                    "status": "complete" if enough else "insufficient_data",
                    "days": len(baseline),
                    "requested_days": baseline_days,
                    "median": reference,
                    "samples": baseline,
                    "method": "hourly_statistics_only",
                },
                "delta_kwh": delta,
                "delta_percent": percent,
                "deviation": deviation,
            }
        )
    complete = all(v["status"] == "complete" for v in values)
    comparisons = all(
        v["baseline"]["status"] == "complete"
        for v in values
        if v["key"] in ("solar", "grid_import")
    )
    assessment = (
        "insufficient_data"
        if not complete or not comparisons
        else "attention_needed"
        if any(v["deviation"] for v in values)
        else "no_anomaly_found"
    )
    coverage = min(v["statistics"]["hours"] for v in values) * 3600
    result = {
        "schema_version": 1,
        "scope": "energy_statistics",
        "profile_id": profile_id,
        "profile_hash": digest(profile),
        "profile": deepcopy(profile),
        "day": day.isoformat(),
        "timezone": zone,
        "start": iso(start),
        "end": iso(end),
        "window_seconds": end - start,
        "coverage_seconds": coverage,
        "coverage_percent": round(100 * coverage / (end - start), 1),
        "assessment": assessment,
        "data_quality": "complete" if complete and comparisons else "insufficient_data",
        "metrics": values,
        "counts": {},
        "limitations": [
            "hourly_statistics_are_not_device_uptime",
            "recovered_internal_counter_gaps_do_not_imply_lost_energy",
            "recent_days_are_not_weather_or_usage_normalized",
            "forecast_is_independent_context_not_a_causal_diagnosis",
            "no_ev_solar_share_or_home_consumption_inferred",
            "no_claim_about_faults_outside_energy_measurements",
        ],
    }

    if profile.get("solar_forecast"):
        config = profile["solar_forecast"]
        result["solar_forecast"] = energy_forecast.snapshot(
            histories.get(config["entity"], []), config, day, zone
        )
    return result


def read_recorder(hass, start, end, history_start, sources):
    """Runs only on Recorder's own executor. No direct SQL or state writes."""
    from homeassistant.components.recorder.history import get_significant_states
    from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period

    metadata = get_metadata(hass, statistic_ids=set(sources))
    data = statistics_during_period(
        hass, start, end, set(sources), "hour", {"energy": "kWh"}, {"sum", "state"}
    )
    histories = get_significant_states(
        hass, history_start, end, sources, include_start_time_state=True, no_attributes=False
    )
    return data, metadata, histories


class EnergyReports:
    def __init__(self, owner):
        self.owner = owner
        self.store = Store(owner.hass, 1, f"home_insights.{owner.entry.entry_id}.energy")
        self.profiles = {}
        self.lock = asyncio.Lock()

    async def load(self):
        self.profiles = {
            k: validate_profile(v) for k, v in (await self.store.async_load() or {}).items()
        }

    async def set_profile(self, name, profile, expected_hash=None):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", name):
            raise ValueError("Invalid energy profile ID")
        value = validate_profile(profile)
        async with self.lock:
            old = self.profiles.get(name)
            if old and expected_hash != digest(old):
                raise ValueError("Energy profile changed; read its hash before replacing it")
            if len(self.profiles) >= 10 and not old:
                raise ValueError("Too many energy profiles")
            await self.store.async_save({**self.profiles, name: value})
            self.profiles = {**self.profiles, name: value}
        return {"profile_id": name, "profile_hash": digest(value), "profile": deepcopy(value)}

    def get_profile(self, name):
        if name not in self.profiles:
            raise ValueError("Unknown energy profile")
        value = self.profiles[name]
        return {"profile_id": name, "profile_hash": digest(value), "profile": deepcopy(value)}

    def planning(self, name, day, now):
        config = self.get_profile(name)["profile"].get("charging_planning")
        return energy_planning.collect(self.owner.hass, config, day, now) if config else None

    async def facts(self, name, day):
        from homeassistant.components.recorder import get_instance

        profile = deepcopy(self.get_profile(name)["profile"])
        zone = self.owner.hass.config.time_zone
        start, end = daily_window(day, zone)
        baseline_start, _ = daily_window(day - timedelta(days=profile["baseline_days"]), zone)
        job = partial(
            read_recorder,
            self.owner.hass,
            datetime.fromtimestamp(baseline_start - 3600, UTC),
            datetime.fromtimestamp(end, UTC),
            datetime.fromtimestamp(start, UTC),
            list(profile["metrics"].values())
            + ([profile["solar_forecast"]["entity"]] if profile.get("solar_forecast") else []),
        )
        async with asyncio.timeout(60):
            data, metadata, histories = await get_instance(self.owner.hass).async_add_executor_job(
                job
            )
        return compute_facts(profile, name, day, zone, data, metadata, histories)
