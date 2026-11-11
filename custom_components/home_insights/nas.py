"""Read-only NAS/UPS retrospective from native Recorder state and attributes."""

import asyncio
import math
import re
import time
from bisect import bisect_right
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial

import probatio as vol
from homeassistant.helpers.storage import Store

from . import nas_events
from .models import daily_window, digest, iso, previous_day
from .recorder_history import read_recorder, stamp

BINARY = vol.Match(r"^binary_sensor\.[a-z0-9_]+$")
ENTITY = vol.Match(r"^(?:binary_sensor|sensor)\.[a-z0-9_]+$")
DISK = {
    vol.Required("id"): vol.Match(r"^[a-z0-9_-]{1,64}$"),
    vol.Required("name"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Required("health"): BINARY,
    vol.Required("temperature"): {
        vol.Required("entity"): ENTITY,
        vol.Optional("attribute"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("unit"): vol.In(["°C", "°F"]),
    },
    vol.Required("warn_celsius"): vol.All(vol.Coerce(float), vol.Range(min=20, max=100)),
}
PROFILE = vol.Schema(
    {
        vol.Required("version"): vol.Equal(1),
        vol.Required("revision"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Required("sources"): {
            vol.Required("array_running"): BINARY,
            vol.Required("parity_problem"): BINARY,
            vol.Required("parity_running"): BINARY,
            vol.Required("ups_status"): vol.Match(r"^sensor\.[a-z0-9_]+$"),
        },
        vol.Required("disks"): vol.All([DISK], vol.Length(min=1, max=32)),
        vol.Optional("unraid_notifications", default=[]): vol.All(
            [vol.Match(r"^event\.[a-z0-9_]+$")], vol.Length(max=5)
        ),
        vol.Optional("problem_sources", default=[]): vol.All([BINARY], vol.Length(max=20)),
        vol.Optional("minimum_hot_seconds", default=600): vol.All(int, vol.Range(min=60, max=3600)),
        vol.Optional("parity_error_attribute", default="errors"): vol.All(
            str, vol.Length(min=1, max=64)
        ),
    }
)
UPS_KNOWN = set("OL OB LB RB CHRG DISCHRG BYPASS CAL OFF OVER TRIM BOOST FSD ALARM ECO ESS".split())
UPS_WARN = {"LB", "RB", "BYPASS", "OFF", "OVER", "FSD", "ALARM"}


def validate_profile(value):
    try:
        profile = PROFILE(deepcopy(value))
    except (vol.Invalid, TypeError) as error:
        raise ValueError("Invalid NAS profile") from error
    disks = profile["disks"]
    if (
        len({d["id"] for d in disks}) != len(disks)
        or len({d["health"] for d in disks}) != len(disks)
        or len(set(profile["sources"].values())) != 4
        or len(set(profile["problem_sources"])) != len(profile["problem_sources"])
        or len({(d["temperature"]["entity"], d["temperature"].get("attribute")) for d in disks})
        != len(disks)
        or any(not math.isfinite(d["warn_celsius"]) for d in disks)
    ):
        raise ValueError("NAS source identities must be distinct and thresholds finite")
    return profile


def source_ids(profile):
    return sorted(
        set(profile["sources"].values())
        | set(profile["problem_sources"])
        | {d["health"] for d in profile["disks"]}
        | {d["temperature"]["entity"] for d in profile["disks"]}
    )


def valid(row):
    return (
        row is not None
        and row.state not in ("unknown", "unavailable", "")
        and not row.attributes.get("restored")
    )


def binary(row):
    return (row.state == "on") if valid(row) and row.state in ("on", "off") else None


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and not isinstance(value, bool) else None
    except TypeError, ValueError:
        return None


def temperature(row, spec):
    if not valid(row):
        return None, False
    standby = row.attributes.get("standby") is True or row.attributes.get("spinning") is False
    if standby:
        return None, True
    raw = row.attributes.get(spec["attribute"]) if "attribute" in spec else row.state
    n = number(raw)
    unit = spec["unit"] if "attribute" in spec else row.attributes.get("unit_of_measurement")
    if n is None or unit not in ("°C", "°F"):
        return None, False
    n = (n - 32) * 5 / 9 if unit == "°F" else n
    return (n if 0 < n <= 120 else None), False


def append_gap(gaps, start, end):
    if gaps and gaps[-1][1] == start:
        gaps[-1] = (gaps[-1][0], end)
    else:
        gaps.append((start, end))


def compute_facts(profile, profile_id, day, zone, histories, runs):
    start, end = daily_window(day, zone)
    lookback = start - 3600
    if sum(len(rows) for rows in histories.values()) > 100000:
        raise ValueError("NAS history exceeds its total query budget")
    points, conflicts = {}, 0
    for entity in source_ids(profile):
        rows = histories.get(entity, [])
        if len(rows) > 20000:
            raise ValueError("NAS history exceeds its bounded query budget")
        points[entity] = {}
        for row in rows:
            at = stamp(row.last_updated)
            if not math.isfinite(at) or at >= end:
                continue
            at = max(lookback, at)
            old = points[entity].get(at)
            if at in points[entity] and (
                old is None or old.state != row.state or old.attributes != row.attributes
            ):
                conflicts += 1
                points[entity][at] = None
            else:
                points[entity][at] = row
    trusted = [
        (max(lookback, r["start"]), min(end, r["end"] or end))
        for r in runs
        if not r["closed_incorrect"] and r["start"] < end and (r["end"] or end) > lookback
    ]
    trusted.sort()
    if any(b > c for (_, b), (c, _) in zip(trusted, trusted[1:], strict=False)):
        raise ValueError("Overlapping Recorder runs cannot establish NAS coverage")
    trusted_starts = [a for a, _ in trusted]
    run_starts = set(trusted_starts)
    times = sorted(
        {lookback, start, end}
        | {v for a, b in trusted for v in (a, b)}
        | {t for values in points.values() for t in values}
    )
    current = {k: None for k in points}
    roles = profile["sources"]
    signals = {
        k: {
            "source": v,
            "known_seconds": 0.0,
            "active_seconds": 0.0,
            "inactive_seconds": 0.0,
            "observed_starts": 0,
        }
        for k, v in roles.items()
    }
    problems = {
        k: {"source": k, "known_seconds": 0.0, "active_seconds": 0.0}
        for k in profile["problem_sources"]
    }
    disks = [
        {
            "id": d["id"],
            "name": d["name"],
            "source": d["temperature"]["entity"],
            "health_source": d["health"],
            "warn_celsius": d["warn_celsius"],
            "maximum_celsius": None,
            "maximum_recorded_at": None,
            "known_seconds": 0.0,
            "standby_seconds": 0.0,
            "health_known_seconds": 0.0,
            "health_problem_seconds": 0.0,
            "hot_seconds": 0.0,
            "longest_hot_seconds": 0.0,
        }
        for d in profile["disks"]
    ]
    hot_runs = [0.0] * len(disks)
    previous = {k: None for k in signals}
    gaps, covered, any_covered = [], 0.0, 0.0
    ups_flags, observed_ups_flags, battery_periods = set(), set(), []
    parity_errors = None
    interrupted_attention = False
    previous_attention = False
    for i, at in enumerate(times[:-1]):
        boundary = at in run_starts
        if boundary:
            current = {k: None for k in points}
            previous = {k: None for k in signals}
            hot_runs = [0.0] * len(disks)
        for entity, values in points.items():
            if at in values:
                current[entity] = values[at]
        run_index = bisect_right(trusted_starts, at) - 1
        recording = run_index >= 0 and at < trusted[run_index][1]
        until = times[i + 1]
        a, b = max(start, at), min(end, until)
        duration = max(0, b - a)
        observations = {
            k: binary(current[v]) if recording else None
            for k, v in roles.items()
            if k != "ups_status"
        }
        ups_row = current[roles["ups_status"]]
        tokens = set(ups_row.state.split()) if recording and valid(ups_row) else set()
        ups_known = (
            bool(tokens)
            and not (tokens - UPS_KNOWN)
            and bool(tokens & {"OL", "OB", "OFF"})
            and not {"OL", "OB"} <= tokens
        )
        observations["ups_status"] = (
            ("OB" in tokens or bool(tokens & UPS_WARN)) if ups_known else None
        )
        battery = "OB" in tokens if ups_known else None
        if duration:
            ups_flags |= tokens & UPS_WARN
            observed_ups_flags |= tokens & UPS_KNOWN
            if battery:
                if boundary:
                    battery_periods.append((a, b))
                else:
                    append_gap(battery_periods, a, b)
        attention = observations["array_running"] is False or any(
            v is True
            for k, v in observations.items()
            if k not in ("array_running", "parity_running")
        )
        required = [v is not None for v in observations.values()]
        for key, result in signals.items():
            value = observations[key]
            counting_value = battery if key == "ups_status" else value
            if duration and value is not None:
                result["known_seconds"] += duration
                result["active_seconds"] += duration * bool(counting_value)
                result["inactive_seconds"] += duration * (not bool(counting_value))
                if counting_value and previous[key] is False and start <= at < end:
                    result["observed_starts"] += 1
            previous[key] = counting_value
        parity = current[roles["parity_problem"]]
        errors = (
            number(parity.attributes.get(profile["parity_error_attribute"]))
            if recording and valid(parity)
            else None
        )
        if duration and errors is not None and errors >= 0:
            parity_errors = max(parity_errors or 0, errors)
            attention |= errors > 0
        for entity, result in problems.items():
            value = binary(current[entity]) if recording else None
            required.append(value is not None)
            if duration and value is not None:
                result["known_seconds"] += duration
                result["active_seconds"] += duration * value
                attention |= value
        for n, (spec, result) in enumerate(zip(profile["disks"], disks, strict=True)):
            health = binary(current[spec["health"]]) if recording else None
            temp, standby = (
                temperature(current[spec["temperature"]["entity"]], spec["temperature"])
                if recording
                else (None, False)
            )
            health_row = current[spec["health"]]
            if health is not None and (
                health_row.attributes.get("standby") is True
                or health_row.attributes.get("spinning") is False
            ):
                temp, standby = None, True
            required.extend((health is not None, temp is not None or standby))
            if not duration:
                continue
            if health is not None:
                result["health_known_seconds"] += duration
                result["health_problem_seconds"] += duration * health
                attention |= health
            if standby:
                result["standby_seconds"] += duration
            if temp is not None:
                result["known_seconds"] += duration
                if result["maximum_celsius"] is None or temp > result["maximum_celsius"]:
                    result["maximum_celsius"] = round(temp, 3)
                    result["maximum_recorded_at"] = iso(
                        stamp(current[spec["temperature"]["entity"]].last_updated)
                    )
            if temp is not None and temp >= spec["warn_celsius"]:
                result["hot_seconds"] += duration
                hot_runs[n] += duration
                result["longest_hot_seconds"] = max(result["longest_hot_seconds"], hot_runs[n])
            else:
                hot_runs[n] = 0.0
        if duration:
            known = all(required)
            any_covered += duration * any(required)
            covered += duration * known
            if not known:
                append_gap(gaps, a, b)
                interrupted_attention |= previous_attention
            if boundary:
                interrupted_attention |= previous_attention
            previous_attention = attention
    for result in disks:
        result["sustained_hot"] = result["longest_hot_seconds"] >= profile["minimum_hot_seconds"]
    window = end - start
    missing = max(0, window - covered)
    complete = missing < 0.001 and not conflicts
    minor = (
        0 < missing <= 300
        and all(b - a <= 120 for a, b in gaps)
        and not conflicts
        and not interrupted_attention
    )
    finding = (
        signals["array_running"]["inactive_seconds"] > 0
        or any(
            s["active_seconds"] > 0
            for k, s in signals.items()
            if k not in ("array_running", "parity_running")
        )
        or bool(ups_flags)
        or bool(parity_errors)
        or any(s["active_seconds"] > 0 for s in problems.values())
        or any(d["health_problem_seconds"] > 0 or d["sustained_hot"] for d in disks)
    )
    quality = "complete" if complete else "minor_gaps" if minor else "insufficient_data"
    for result in [*signals.values(), *problems.values(), *disks]:
        for key, value in result.items():
            if isinstance(value, float):
                result[key] = round(value, 3)
    return {
        "schema_version": 1,
        "scope": "nas_health",
        "profile_id": profile_id,
        "profile_hash": digest(profile),
        "profile": deepcopy(profile),
        "day": day.isoformat(),
        "timezone": zone,
        "start": iso(start),
        "end": iso(end),
        "window_seconds": window,
        "coverage_seconds": round(covered, 3),
        "coverage_percent": 100 if complete else min(99.999, round(100 * covered / window, 3)),
        "unobserved_seconds": round(missing, 3),
        "data_quality": quality,
        "assessment": "not_run"
        if not any_covered and not finding
        else "attention_needed"
        if finding
        else "no_anomaly_found"
        if complete or minor
        else "insufficient_data",
        "counts": {},
        "signals": signals,
        "problem_sources": list(problems.values()),
        "disks": disks,
        "ups_warning_flags": sorted(ups_flags),
        "ups_observed_flags": sorted(observed_ups_flags),
        "ups_battery_periods": [
            {"start": iso(a), "end": iso(b), "seconds": round(b - a, 3)}
            for a, b in battery_periods[:200]
        ],
        "ups_battery_periods_truncated": len(battery_periods) > 200,
        "parity_max_reported_errors": parity_errors,
        "counts_are_lower_bounds": not complete,
        "observation_gaps": {
            "count": len(gaps),
            "intervals": [
                {"start": iso(a), "end": iso(b), "seconds": round(b - a, 3)} for a, b in gaps[:200]
            ],
            "truncated": len(gaps) > 200,
        },
        "recorder": {"conflicting_states": conflicts},
        "limitations": [
            "selected_sources_only_not_a_full_smart_audit",
            "recorded_held_values_not_independent_fresh_samples",
            "unknown_intervals_can_hide_events",
            "standby_temperature_is_not_zero",
            "parity_error_maximum_is_not_new_daily_errors",
            "ups_battery_operation_does_not_prove_grid_failure",
            "thresholds_are_review_heuristics_not_manufacturer_limits",
        ],
    }


class NasReports:
    def __init__(self, owner):
        self.owner = owner
        self.store = Store(owner.hass, 1, f"home_insights.{owner.entry.entry_id}.nas")
        self.profiles = {}
        self.lock = asyncio.Lock()

    async def load(self):
        self.profiles = {
            k: validate_profile(v) for k, v in (await self.store.async_load() or {}).items()
        }

    def get_profile(self, name):
        if name not in self.profiles:
            raise ValueError("Unknown NAS profile")
        value = self.profiles[name]
        return {"profile_id": name, "profile_hash": digest(value), "profile": deepcopy(value)}

    async def set_profile(self, name, profile, expected_hash=None):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", name):
            raise ValueError("Invalid NAS profile ID")
        value = validate_profile(profile)
        async with self.lock:
            old = self.profiles.get(name)
            if old and expected_hash != digest(old):
                raise ValueError("NAS profile changed; read its hash before replacing it")
            if not old and len(self.profiles) >= 10:
                raise ValueError("Too many NAS profiles")
            await self.store.async_save({**self.profiles, name: value})
            self.profiles = {**self.profiles, name: value}
        return self.get_profile(name)

    async def facts(self, name, day):
        from homeassistant.components.recorder import get_instance

        profile = deepcopy(self.get_profile(name)["profile"])
        zone = self.owner.hass.config.time_zone
        start, end = daily_window(day, zone)
        now = time.time()
        observed_until = now if previous_day(now, zone) == day else end
        job = partial(
            collect_facts, self.owner.hass, profile, name, day, zone, start, end, observed_until
        )
        async with asyncio.timeout(60):
            return await get_instance(self.owner.hass).async_add_executor_job(job)


def collect_facts(hass, profile, name, day, zone, start, end, observed_until):
    history, runs = read_recorder(
        hass,
        datetime.fromtimestamp(start - 3600, UTC),
        datetime.fromtimestamp(observed_until, UTC),
        sorted(set(source_ids(profile)) | set(profile["unraid_notifications"])),
    )
    facts = compute_facts(profile, name, day, zone, history, runs)
    sources = profile["unraid_notifications"]
    facts["notifications"] = nas_events.collect_events(history, sources, start, end)
    if facts["notifications"]["attention"]:
        facts["assessment"] = "attention_needed"
    if facts["notifications"]["invalid_events"] or facts["notifications"]["truncated"]:
        facts["data_quality"] = "insufficient_data"
        if facts["assessment"] == "no_anomaly_found":
            facts["assessment"] = "insufficient_data"
    if observed_until > end:
        facts["since_midnight"] = nas_events.collect_events(history, sources, end, observed_until)
    return facts
