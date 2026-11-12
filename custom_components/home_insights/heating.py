"""Observed burner cycles, separated from room demand and domestic hot water."""

import asyncio
import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from statistics import mean, median

import probatio as vol
from homeassistant.helpers.storage import Store

from .heating_analysis import CycleContext, append_interval, temperature
from .models import daily_window, digest, iso
from .recorder_history import read_recorder

SOURCES = ("flame", "heating", "dhw", "connectivity")
MAX_ROWS = 20000
PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("version"): vol.Equal(1),
        vol.Required("revision"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Required("sources"): {
            vol.Required(k): vol.Match(r"^binary_sensor\.[a-z0-9_]+$") for k in SOURCES
        },
        vol.Optional("short_burn_seconds", default=180): vol.All(int, vol.Range(min=15, max=900)),
        vol.Optional("minimum_short_cycles", default=3): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional("mode_edge_seconds", default=2): vol.All(int, vol.Range(min=0, max=10)),
        vol.Optional("temperatures"): {
            vol.Required(k): vol.Match(r"^sensor\.[a-z0-9_]+$")
            for k in ("flow", "return", "target")
        },
        vol.Optional("rapid_pause_seconds", default=120): vol.All(int, vol.Range(min=10, max=300)),
        vol.Optional("rapid_start_interval_seconds", default=300): vol.All(
            int, vol.Range(min=30, max=900)
        ),
        vol.Optional("warm_margin_celsius", default=5): vol.All(int, vol.Range(min=1, max=10)),
        vol.Optional("warm_window_seconds", default=600): vol.All(int, vol.Range(min=60, max=1800)),
        vol.Optional("warm_short_burns_expected", default=False): bool,
    }
)


def validate_profile(value):
    try:
        profile = PROFILE_SCHEMA(deepcopy(value))
    except (vol.Invalid, TypeError) as error:
        raise ValueError("Invalid heating profile") from error
    if len(set(profile["sources"].values())) != len(SOURCES):
        raise ValueError("Heating sources must be distinct")
    if "temperatures" in profile and len(set(profile["temperatures"].values())) != 3:
        raise ValueError("Heating temperatures must be distinct")
    return profile


def stamp(value):
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp()


def normalize(histories, sources, start, end, numeric=False):
    points = {}
    conflicts = 0
    for key, source in sources.items():
        rows = histories.get(source, [])
        if len(rows) > MAX_ROWS:
            raise ValueError("Heating history exceeds the bounded query budget")
        values = {}
        for row in rows:
            at = stamp(row.last_updated)
            if not math.isfinite(at) or at >= end:
                continue
            at = max(start, at)
            value = (
                temperature(row)
                if numeric
                else row.state
                if row.state in ("on", "off")
                else "unknown"
            )
            if row.attributes.get("restored"):
                value = None if numeric else "unknown"
            previous_value = values.get(at)
            if numeric and previous_value is not None:
                previous_value = previous_value["celsius"]
            if at in values and previous_value != value:
                conflicts += 1
                value = None if numeric else "unknown"
            if numeric and value is not None:
                value = {"celsius": value, "recorded_at": iso(stamp(row.last_updated))}
            values[at] = value
        points[key] = values
    return points, conflicts


def mode_of(values):
    ch, dhw = values["heating"], values["dhw"]
    if ch == "unknown" or dhw == "unknown":
        return "unknown"
    if ch == "on" and dhw == "on":
        return "mixed"
    return "heating" if ch == "on" else "dhw" if dhw == "on" else "idle"


def classify(parts, started, ended, edge):
    if ended - started <= 2 * edge or any(p[2] == "unknown" for p in parts):
        return "unclassified"
    full = {p[2] for p in parts}
    if "mixed" in full or {"heating", "dhw"} <= full:
        return "mixed"
    core = {mode for a, b, mode in parts if min(b, ended - edge) > max(a, started + edge)}
    return next(iter(core)) if core in ({"heating"}, {"dhw"}) else "unclassified"


def compute_facts(profile, profile_id, day, zone, histories, runs):
    start, end = daily_window(day, zone)
    lookback = start - 86400
    points, conflicts = normalize(histories, profile["sources"], lookback, end)
    thermal, thermal_conflicts = normalize(
        histories, profile.get("temperatures", {}), lookback, end, True
    )
    context = CycleContext(profile)
    trusted = [
        (max(lookback, r["start"]), min(end, r["end"] or end))
        for r in runs
        if not r["closed_incorrect"] and r["start"] < end and (r["end"] or end) > lookback
    ]
    # Every Recorder run boundary breaks continuity, including adjacent runs.
    boundaries = {lookback, start, end}
    for a, b in trusted:
        boundaries.update((a, b))
    for values in points.values():
        boundaries.update(values)
    for changes in thermal.values():
        boundaries.update(changes)
    times = sorted(t for t in boundaries if lookback <= t <= end)
    run_starts = {a for a, _ in trusted}
    values = {k: "unknown" for k in SOURCES}
    temperatures = {k: None for k in thermal}
    recovery_points = {}
    was_connected = False
    previous = "unknown"
    active = None
    cycles = []
    ignitions = 0
    covered = 0.0
    mode_covered = 0.0
    burning = 0.0
    carried = False
    gaps = []

    def close(at, reason):
        nonlocal active
        if active is None:
            return
        if at > start and active["observed_from"] < end:
            complete = reason == "off" and active["started"] is not None
            began = active["started"]
            cycles.append(
                {
                    "started_at": iso(began) if began is not None else None,
                    "observed_from": iso(active["observed_from"]),
                    "ended_at": iso(at) if reason == "off" else None,
                    "observed_until": iso(at),
                    "complete": complete,
                    "reason": reason if began is not None else "start_unknown",
                    "carried_into_day": active["observed_from"] < start,
                    "duration_seconds": round(at - began, 3) if complete else None,
                    "mode": classify(active["parts"], began, at, profile["mode_edge_seconds"])
                    if complete
                    else "unclassified",
                    "temperatures_at_start": active["temperatures"],
                }
            )
        active = None

    for i, at in enumerate(times[:-1]):
        if at in run_starts:
            close(at, "recorder_boundary")
            values = {k: "unknown" for k in SOURCES}
            previous = "unknown"
            temperatures = {k: None for k in thermal}
            recovery_points = {}
            was_connected = False
            context.barriers.append(at)
        for key, changes in points.items():
            if at in changes:
                values[key] = changes[at]
        connected = values["connectivity"] == "on"
        if was_connected and not connected:
            recovery_points = {}
        for key, changes in thermal.items():
            if at in changes:
                recovery_points[key] = (at, changes[at])
        if not connected:
            temperatures = {k: None for k in thermal}
        else:
            if not was_connected:
                # Async entity updates can precede the bus-on update by microseconds.
                # Reconcile only explicit non-restored points from this run, <=2s old.
                temperatures = {
                    k: recovery_points[k][1]
                    if k in recovery_points and 0 <= at - recovery_points[k][0] <= 2
                    else None
                    for k in thermal
                }
            for key, changes in thermal.items():
                if at in changes:
                    temperatures[key] = changes[at]
        was_connected = connected
        recording = any(a <= at < b for a, b in trusted)
        flame = values["flame"] if recording and values["connectivity"] == "on" else "unknown"
        if flame != previous:
            if previous == "on":
                close(at, "off" if flame == "off" else "observation_gap")
            if flame == "on":
                began = at if previous == "off" else None
                if began is not None and start <= at < end:
                    ignitions += 1
                active = {
                    "started": began,
                    "observed_from": at,
                    "parts": [],
                    "temperatures": dict(temperatures),
                }
            previous = flame
        until = times[i + 1]
        a, b = max(start, at), min(end, until)
        context.observe(at, until, flame, mode_of(values), temperatures)
        if b > a and flame == "unknown":
            append_interval(gaps, a, b)
        if b > a and flame != "unknown":
            covered += b - a
            if flame == "off" or mode_of(values) != "unknown":
                mode_covered += b - a
            if flame == "on":
                burning += b - a
        if at == start and flame == "on":
            carried = True
        if active is not None:
            active["parts"].append((at, until, mode_of(values)))
    close(end, "day_end")
    complete = [c for c in cycles if c["complete"]]
    heating = [c for c in complete if c["mode"] == "heating"]
    short = [c for c in heating if c["duration_seconds"] < profile["short_burn_seconds"]]
    durations = [c["duration_seconds"] for c in heating]
    cycle_analysis = context.analyze(cycles)
    good = covered >= end - start - 0.001 and mode_covered >= end - start - 0.001 and not conflicts
    minor_gaps = (
        0 < end - start - covered <= 300
        and all(b - a <= 120 for a, b in gaps)
        and covered - mode_covered < 0.001
        and not conflicts
        and not any(
            c["reason"] in ("observation_gap", "recorder_boundary", "start_unknown") for c in cycles
        )
    )
    finding = cycle_analysis["short_cycle_pattern"]
    assessment = (
        "not_run"
        if covered == 0
        else "attention_needed"
        if finding
        else "insufficient_data"
        if not good and not minor_gaps
        else "no_anomaly_found"
    )
    return {
        "schema_version": 1,
        "scope": "heating_cycles",
        "profile_id": profile_id,
        "profile_hash": digest(profile),
        "profile": deepcopy(profile),
        "day": day.isoformat(),
        "timezone": zone,
        "start": iso(start),
        "end": iso(end),
        "window_seconds": end - start,
        "coverage_seconds": round(covered, 3),
        "coverage_percent": 100
        if covered >= end - start - 0.001
        else min(99.999, round(100 * covered / (end - start), 3)),
        "mode_coverage_seconds": round(mode_covered, 3),
        "unobserved_seconds": round(max(0, end - start - covered), 3),
        "assessment": assessment,
        "data_quality": "complete" if good else "minor_gaps" if minor_gaps else "insufficient_data",
        "observation_gaps": {
            "count": len(gaps),
            "longest_seconds": round(max((b - a for a, b in gaps), default=0), 3),
            "intervals": [
                {"start": iso(a), "end": iso(b), "seconds": round(b - a, 3)} for a, b in gaps[:200]
            ],
            "truncated": len(gaps) > 200,
            "minor_policy": {
                "maximum_total_seconds": 300,
                "maximum_single_seconds": 120,
                "requires_no_interrupted_burns": True,
            },
        },
        "counts": {},
        "burner": {
            "observed_ignitions": ignitions if covered else None,
            "observed_seconds": round(burning, 3) if covered else None,
            "counts_are_lower_bounds": covered < end - start - 0.001 or bool(conflicts),
            "carried_into_day": carried,
        },
        "cycles": {
            "complete": len(complete),
            "heating": len(heating),
            "dhw": sum(c["mode"] == "dhw" for c in complete),
            "mixed": sum(c["mode"] == "mixed" for c in complete),
            "unclassified": sum(c["mode"] == "unclassified" for c in complete),
            "censored": len(cycles) - len(complete),
            "heating_median_seconds": round(median(durations), 3) if durations else None,
            "heating_mean_seconds": round(mean(durations), 3) if durations else None,
            "heating_min_seconds": min(durations) if durations else None,
            "heating_max_seconds": max(durations) if durations else None,
            "short_heating": len(short) if heating else None,
            **cycle_analysis,
            "samples": sorted(cycles, key=lambda c: c["observed_from"])[-200:],
            "truncated": len(cycles) > 200,
        },
        "recorder": {
            "runs": len(runs),
            "untrusted_runs": sum(r["closed_incorrect"] for r in runs),
            "conflicting_states": conflicts,
            "thermal_conflicting_states": thermal_conflicts,
        },
        "limitations": [
            "recorded_flame_feedback_not_thermostat_demand",
            "unknown_intervals_can_hide_starts",
            "only_complete_heating_burns_enter_duration_and_short_burn_statistics",
            "dhw_mixed_and_unclassified_burns_do_not_create_short_heating_alerts",
            "short_burn_threshold_is_a_review_heuristic_not_a_fault_diagnosis",
            "rapid_sequences_require_consecutive_short_heating_burns_and_known_spacing",
            "warm_context_never_suppresses_rapid_restart_sequences",
            "held_recorded_temperatures_are_not_independent_fresh_sensor_samples",
            "no_gas_consumption_or_room_comfort_inferred",
        ],
    }


class HeatingReports:
    def __init__(self, owner):
        self.owner = owner
        self.store = Store(owner.hass, 1, f"home_insights.{owner.entry.entry_id}.heating")
        self.profiles = {}
        self.lock = asyncio.Lock()

    async def load(self):
        self.profiles = {
            k: validate_profile(v) for k, v in (await self.store.async_load() or {}).items()
        }

    def get_profile(self, name):
        if name not in self.profiles:
            raise ValueError("Unknown heating profile")
        value = self.profiles[name]
        return {"profile_id": name, "profile_hash": digest(value), "profile": deepcopy(value)}

    async def set_profile(self, name, profile, expected_hash=None):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", name):
            raise ValueError("Invalid heating profile ID")
        value = validate_profile(profile)
        async with self.lock:
            old = self.profiles.get(name)
            if old and expected_hash != digest(old):
                raise ValueError("Heating profile changed; read its hash before replacing it")
            if not old and len(self.profiles) >= 10:
                raise ValueError("Too many heating profiles")
            await self.store.async_save({**self.profiles, name: value})
            self.profiles = {**self.profiles, name: value}
        return self.get_profile(name)

    async def facts(self, name, day):
        from homeassistant.components.recorder import get_instance

        profile = deepcopy(self.get_profile(name)["profile"])
        zone = self.owner.hass.config.time_zone
        start, end = daily_window(day, zone)
        job = partial(
            read_recorder,
            self.owner.hass,
            datetime.fromtimestamp(start - 86400, UTC),
            datetime.fromtimestamp(end, UTC),
            list(profile["sources"].values()) + list(profile.get("temperatures", {}).values()),
        )
        async with asyncio.timeout(60):
            history, runs = await get_instance(self.owner.hass).async_add_executor_job(job)
        return compute_facts(profile, name, day, zone, history, runs)
