"""Measured restart spacing and thermal context, never a boiler fault diagnosis."""

import math
from bisect import bisect_right
from datetime import datetime


def temperature(row):
    if row.attributes.get("restored"):
        return None
    try:
        value = float(row.state)
    except ValueError, TypeError:
        return None
    unit = row.attributes.get("unit_of_measurement")
    if unit == "°F":
        value = (value - 32) * 5 / 9
    elif unit != "°C":
        return None
    if not math.isfinite(value) or not -20 <= value <= 120:
        return None
    return round(value, 3)


def append_interval(intervals, start, end):
    if intervals and intervals[-1][1] == start:
        intervals[-1] = (intervals[-1][0], end)
    else:
        intervals.append((start, end))


class CycleContext:
    def __init__(self, profile):
        self.profile = profile
        self.safe, self.heating, self.warm, self.thermal_known = [], [], [], []
        self.barriers = []

    def observe(self, at, until, flame, mode, temperatures):
        if flame == "unknown" or mode not in ("heating", "idle"):
            return
        append_interval(self.safe, at, until)
        if mode == "heating":
            append_interval(self.heating, at, until)
        if not temperatures or any(v is None for v in temperatures.values()):
            return
        append_interval(self.thermal_known, at, until)
        flow, ret, target = (temperatures[k]["celsius"] for k in ("flow", "return", "target"))
        margin = self.profile["warm_margin_celsius"]
        if 20 <= target <= 90 and ret >= target - margin and flow >= target - margin:
            append_interval(self.warm, at, until)

    def analyze(self, cycles):
        indexes = {
            id(rows): [a for a, _ in rows]
            for rows in (self.safe, self.heating, self.warm, self.thermal_known)
        }

        def covered(rows, a, b):
            i = bisect_right(indexes[id(rows)], a) - 1
            barrier = bisect_right(self.barriers, a)
            return (
                i >= 0
                and rows[i][1] >= b
                and (barrier == len(self.barriers) or self.barriers[barrier] > b)
            )

        def stamp(value):
            return datetime.fromisoformat(value).timestamp()

        groups, group = [], []
        previous = None
        for cycle in cycles:
            cycle.update(
                previous_pause_seconds=None,
                start_interval_seconds=None,
                heating_feedback_continuous=None,
                thermal_context="unknown",
                rapid_restart=False,
            )
            eligible = cycle["complete"] and cycle["mode"] == "heating"
            short = eligible and cycle["duration_seconds"] < self.profile["short_burn_seconds"]
            linked = False
            if eligible and previous and previous["complete"]:
                at, last = stamp(cycle["started_at"]), stamp(previous["ended_at"])
                if covered(self.safe, last, at):
                    pause = at - last
                    spacing = at - stamp(previous["started_at"])
                    cycle.update(
                        previous_pause_seconds=round(pause, 3),
                        start_interval_seconds=round(spacing, 3),
                        heating_feedback_continuous=covered(self.heating, last, at),
                    )
                    warm = (
                        previous["mode"] == "heating"
                        and pause <= 1800
                        and covered(self.warm, at - self.profile["warm_window_seconds"], at)
                    )
                    thermal_known = covered(
                        self.thermal_known, at - self.profile["warm_window_seconds"], at
                    )
                    cycle["thermal_context"] = (
                        "warm_operation"
                        if warm
                        else "not_established"
                        if thermal_known
                        else "unknown"
                    )
                    linked = (
                        short
                        and previous["mode"] == "heating"
                        and previous["duration_seconds"] < self.profile["short_burn_seconds"]
                        and pause <= self.profile["rapid_pause_seconds"]
                        and spacing <= self.profile["rapid_start_interval_seconds"]
                    )
            if short:
                if linked:
                    group.append(cycle)
                else:
                    if len(group) >= self.profile["minimum_short_cycles"]:
                        groups.append(group)
                    group = [cycle]
            else:
                if len(group) >= self.profile["minimum_short_cycles"]:
                    groups.append(group)
                group = []
            previous = cycle
        if len(group) >= self.profile["minimum_short_cycles"]:
            groups.append(group)
        for group in groups:
            for cycle in group:
                cycle["rapid_restart"] = True
        short = [
            c
            for c in cycles
            if c["complete"]
            and c["mode"] == "heating"
            and c["duration_seconds"] < self.profile["short_burn_seconds"]
        ]
        return {
            "analysis_version": 2,
            "rapid_restart_sequences": len(groups),
            "rapid_restart_burns": sum(len(g) for g in groups),
            "warm_short_heating": sum(c["thermal_context"] == "warm_operation" for c in short),
            "short_context_unknown": sum(c["thermal_context"] == "unknown" for c in short),
            "short_other": sum(c["thermal_context"] == "not_established" for c in short),
            "short_cycle_pattern": bool(groups),
        }
