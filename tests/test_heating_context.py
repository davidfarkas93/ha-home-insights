"""Thermal context and spacing must change interpretation without hiding evidence."""

from datetime import UTC, datetime

import pytest
from homeassistant.core import State

from custom_components.home_insights import heating, reporting
from custom_components.home_insights.models import daily_window

from .test_heating import DAY, PROFILE, event, facts, fixture


def thermal_fixture(return_value="31", attrs=None):
    h, runs = fixture()
    profile = heating.validate_profile(
        {
            **PROFILE,
            "temperatures": {k: "sensor." + k for k in ("return", "flow", "target")},
            "warm_short_burns_expected": True,
        }
    )
    at, _ = daily_window(DAY, "Europe/Budapest")
    for key, value in (("return", return_value), ("flow", "33"), ("target", "35")):
        source = profile["temperatures"][key]
        h[source] = [
            State(
                source,
                value,
                attributes=attrs if attrs is not None else {"unit_of_measurement": "°C"},
                last_updated=datetime.fromtimestamp(at - 86400, UTC),
            )
        ]
    return profile, h, runs


def compute(profile, h, runs):
    return heating.compute_facts(profile, "test", DAY, "Europe/Budapest", h, runs)


def burn(h, at, length=30):
    event(h, "flame", at, "on")
    event(h, "flame", at + length, "off")


def test_warm_short_burns_with_long_pauses_are_context_not_a_fault():
    p, h, r = thermal_fixture()
    burn(h, 60, 600)
    for at in (1000, 1900, 2800):
        burn(h, at)
    f = compute(p, h, r)
    assert f["cycles"]["short_heating"] == 3
    assert f["cycles"]["warm_short_heating"] == 3
    assert f["cycles"]["rapid_restart_sequences"] == 0
    assert f["assessment"] == "no_anomaly_found"
    cycle = f["cycles"]["samples"][1]
    assert cycle["previous_pause_seconds"] == 340
    assert cycle["temperatures_at_start"]["target"]["celsius"] == 35
    # A held value is usable recorded context, not falsely a fresh physical reading.
    assert cycle["temperatures_at_start"]["target"]["recorded_at"] < cycle["started_at"]
    bundle = reporting.evidence_bundle(f)
    for option in bundle["summary_options"]:
        assert {"warm_operation", "heating_burns"} <= set(option["refs"])
        assert "megszokott" in option["text"]


def test_warm_context_cannot_suppress_actual_rapid_restart_sequences():
    p, h, r = thermal_fixture()
    burn(h, 60, 600)
    for at in (1000, 1120, 1240, 1360):
        burn(h, at)
    f = compute(p, h, r)
    assert f["cycles"]["warm_short_heating"] == 4
    assert f["cycles"]["rapid_restart_sequences"] == 1
    assert f["cycles"]["rapid_restart_burns"] == 4
    assert f["assessment"] == "attention_needed"
    assert all(
        "rapid_restarts" in o["refs"] for o in reporting.evidence_bundle(f)["summary_options"]
    )


@pytest.mark.parametrize("break_kind", ["long_burn", "dhw", "mode_unknown", "bus_gap"])
def test_rapid_sequences_do_not_bridge_other_burns_or_unknown_intervals(break_kind):
    h, r = fixture()
    for at in (1000, 1120, 1240):
        burn(h, at, 180 if at == 1120 and break_kind == "long_burn" else 30)
    if break_kind == "long_burn":
        # Avoid overlapping the deliberately longer middle burn.
        h, r = fixture()
        for at, length in ((1000, 30), (1120, 180), (1360, 30)):
            burn(h, at, length)
    elif break_kind == "dhw":
        event(h, "heating", 1119, "off")
        event(h, "dhw", 1119, "on")
        event(h, "dhw", 1151, "off")
        event(h, "heating", 1151, "on")
    elif break_kind == "mode_unknown":
        event(h, "heating", 1190, "unavailable")
        event(h, "heating", 1200, "on")
    else:
        event(h, "connectivity", 1190, "off")
        event(h, "connectivity", 1200, "on")
    assert not facts(h, r)["cycles"]["short_cycle_pattern"]


@pytest.mark.parametrize(
    "value,attrs,expected",
    [
        ("20", {"unit_of_measurement": "°C"}, "not_established"),
        ("31", {"unit_of_measurement": "°C", "restored": True}, "unknown"),
        ("31", {}, "unknown"),
        ("unavailable", {"unit_of_measurement": "°C"}, "unknown"),
    ],
)
def test_cold_missing_restored_or_unitless_temperatures_cannot_be_called_warm(
    value, attrs, expected
):
    p, h, r = thermal_fixture(value, attrs)
    burn(h, 60, 600)
    burn(h, 1000)
    f = compute(p, h, r)
    assert f["cycles"]["warm_short_heating"] == 0
    assert f["cycles"]["samples"][-1]["thermal_context"] == expected


def test_bus_recovery_cannot_reuse_old_thermal_measurements():
    p, h, r = thermal_fixture()
    burn(h, 60, 600)
    event(h, "connectivity", 700, "off")
    event(h, "connectivity", 710, "on")
    burn(h, 1400)
    f = compute(p, h, r)
    assert f["cycles"]["samples"][-1]["thermal_context"] == "unknown"
    assert not f["cycles"]["warm_short_heating"]


def test_small_idle_gaps_are_disclosed_without_invalidating_whole_day():
    h, r = fixture()
    for a, b in ((100, 160), (200, 245)):
        event(h, "connectivity", a, "off")
        event(h, "connectivity", b, "on")
    f = facts(h, r)
    assert f["data_quality"] == "minor_gaps"
    assert f["assessment"] == "no_anomaly_found"
    assert f["burner"]["counts_are_lower_bounds"]
    assert f["coverage_percent"] < 100
    assert f["observation_gaps"]["count"] == 2
    bundle = reporting.evidence_bundle(f)
    result = reporting.validate_narrative({"summary": bundle["summary_options"][0]["text"]}, bundle)
    assert not result["summary"].startswith("A fűtési visszajelzések megfigyelése hiányos volt.")
    event(h, "connectivity", 1000, "off")
    event(h, "connectivity", 1121, "on")
    assert facts(h, r)["assessment"] == "insufficient_data"


def test_confirmed_rapid_findings_remain_primary_with_major_coverage_gaps():
    h, r = fixture()
    for at in (60, 180, 300):
        burn(h, at)
    event(h, "connectivity", 1000, "off")
    event(h, "connectivity", 5000, "on")
    f = facts(h, r)
    assert f["assessment"] == "attention_needed"
    assert f["data_quality"] == "insufficient_data"
    assert all(
        {"rapid_restarts", "coverage"} <= set(o["refs"])
        for o in reporting.evidence_bundle(f)["summary_options"]
    )


@pytest.mark.parametrize(
    "lead,restored,warm", [(0.000138, False, True), (3, False, False), (0.000138, True, False)]
)
def test_explicit_temperature_before_bus_on_is_reconciled_only_within_boundary(
    lead, restored, warm
):
    p, h, r = thermal_fixture()
    event(h, "connectivity", 700, "off")
    a, _ = daily_window(DAY, "Europe/Budapest")
    for key, value in (("flow", "33"), ("return", "31"), ("target", "35")):
        source = p["temperatures"][key]
        h[source].append(
            State(
                source,
                value,
                attributes={"unit_of_measurement": "°C", "restored": restored},
                last_updated=datetime.fromtimestamp(a + 710 - lead, UTC),
            )
        )
    event(h, "connectivity", 710, "on")
    burn(h, 800, 300)
    burn(h, 1400)
    f = compute(p, h, r)
    assert (f["cycles"]["samples"][-1]["thermal_context"] == "warm_operation") is warm
