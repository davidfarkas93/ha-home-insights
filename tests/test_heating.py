"""Burner transitions, observation gaps, mode attribution and safe report boundaries."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.home_insights import api, heating, reporting
from custom_components.home_insights.models import daily_window

from .test_runtime import running_coordinator

DAY = date(2026, 11, 9)
PROFILE = heating.validate_profile(
    {
        "version": 1,
        "revision": "test.1",
        "sources": {k: "binary_sensor." + k for k in heating.SOURCES},
    }
)


def fixture(day=DAY, zone="Europe/Budapest"):
    a, b = daily_window(day, zone)
    history = {
        source: [
            State(
                source,
                "on" if key in ("heating", "connectivity") else "off",
                last_updated=datetime.fromtimestamp(a - 86400, UTC),
            )
        ]
        for key, source in PROFILE["sources"].items()
    }
    return history, [{"start": a - 86400, "end": b, "closed_incorrect": False}]


def event(history, key, seconds, state, day=DAY, attrs=None):
    a, _ = daily_window(day, "Europe/Budapest")
    source = PROFILE["sources"][key]
    history[source].append(
        State(
            source, state, attributes=attrs, last_updated=datetime.fromtimestamp(a + seconds, UTC)
        )
    )


def observed(day=DAY):
    history, runs = fixture(day)
    for a, b in [(60, 360), (600, 660), (1200, 1260), (1800, 1860)]:
        event(history, "flame", a, "on", day)
        event(history, "flame", b, "off", day)
    return history, runs


def facts(history=None, runs=None, day=DAY):
    if history is None:
        history, runs = observed(day)
    return heating.compute_facts(PROFILE, "demo", day, "Europe/Budapest", history, runs)


def test_flame_not_thermostat_demand_drives_counts_and_lengths():
    f = facts()
    assert f["burner"]["observed_ignitions"] == 4
    assert f["burner"]["observed_seconds"] == 480
    assert not f["burner"]["counts_are_lower_bounds"]
    assert f["cycles"]["heating"] == 4 and f["cycles"]["short_heating"] == 3
    assert f["cycles"]["heating_median_seconds"] == 60
    assert f["cycles"]["heating_mean_seconds"] == 120
    assert f["cycles"]["short_cycle_pattern"]
    assert f["assessment"] == "attention_needed" and f["coverage_percent"] == 100


def test_dhw_and_mixed_burns_never_count_as_short_heating():
    h, r = observed()
    event(h, "heating", 599, "off")
    event(h, "dhw", 599, "on")
    event(h, "dhw", 661, "off")
    event(h, "heating", 661, "on")
    event(h, "dhw", 1230, "on")
    event(h, "dhw", 1261, "off")
    f = facts(h, r)
    assert f["cycles"]["heating"] == 2 and f["cycles"]["dhw"] == 1 and f["cycles"]["mixed"] == 1
    assert f["cycles"]["short_heating"] == 1
    assert not f["cycles"]["short_cycle_pattern"]


def test_unknown_mode_cannot_be_invented_and_idle_edges_are_bounded():
    h, r = observed()
    event(h, "heating", 59, "off")
    event(h, "heating", 61, "on")
    f = facts(h, r)
    assert f["cycles"]["heating"] == 4
    event(h, "heating", 620, "unavailable")
    event(h, "heating", 640, "on")
    f = facts(h, r)
    assert f["cycles"]["unclassified"] == 1
    assert f["assessment"] == "insufficient_data"
    assert not f["burner"]["counts_are_lower_bounds"], "mode gaps do not erase known flame edges"


@pytest.mark.parametrize(
    "source,state",
    [("flame", "unavailable"), ("connectivity", "off"), ("connectivity", "unavailable")],
)
def test_gap_inside_burn_breaks_cycle_and_recovery_is_not_ignition(source, state):
    h, r = observed()
    event(h, source, 610, state)
    event(h, source, 630, "on")
    f = facts(h, r)
    assert f["burner"]["observed_ignitions"] == 4
    assert f["cycles"]["complete"] == 3
    assert f["cycles"]["censored"] == 2
    assert f["unobserved_seconds"] == 20
    assert f["assessment"] == "insufficient_data"
    assert f["burner"]["counts_are_lower_bounds"]


def test_carry_in_and_open_day_end_are_distinct_from_new_complete_cycles():
    h, r = fixture()
    event(h, "flame", -120, "on")
    event(h, "flame", 120, "off")
    event(h, "flame", 86300, "on")
    f = facts(h, r)
    assert f["burner"]["carried_into_day"] and f["burner"]["observed_ignitions"] == 1
    assert f["burner"]["observed_seconds"] == 220
    assert f["cycles"]["heating"] == 1 and f["cycles"]["heating_median_seconds"] == 240
    assert f["cycles"]["samples"][-1]["reason"] == "day_end"
    assert f["cycles"]["samples"][-1]["duration_seconds"] is None
    assert f["cycles"]["samples"][0]["carried_into_day"]


def test_unknown_start_does_not_create_an_ignition_or_complete_duration():
    h, r = fixture()
    source = PROFILE["sources"]["flame"]
    h[source][0] = State(source, "on", last_updated=h[source][0].last_updated)
    event(h, "flame", 120, "off")
    f = facts(h, r)
    assert f["burner"]["observed_ignitions"] == 0
    assert f["cycles"]["complete"] == 0 and f["cycles"]["censored"] == 1
    assert f["cycles"]["heating_median_seconds"] is None
    assert f["cycles"]["short_heating"] is None


def test_recorder_outage_prevents_state_carry_and_closed_incorrect_runs_are_unknown():
    h, r = observed()
    a, b = daily_window(DAY, "Europe/Budapest")
    r = [
        {"start": a - 86400, "end": a + 610, "closed_incorrect": False},
        {"start": a + 630, "end": b, "closed_incorrect": False},
    ]
    f = facts(h, r)
    assert f["cycles"]["complete"] <= 3
    assert f["unobserved_seconds"] >= 20
    assert f["assessment"] == "insufficient_data"
    for run in r:
        run["closed_incorrect"] = True
    f = facts(h, r)
    assert f["assessment"] == "not_run" and f["burner"]["observed_ignitions"] is None
    assert f["recorder"]["untrusted_runs"] == 2


def test_missing_restored_conflicting_and_future_states_never_create_a_clean_zero():
    h, r = fixture()
    h.pop(PROFILE["sources"]["flame"])
    assert facts(h, r)["assessment"] == "not_run"
    h, r = fixture()
    event(h, "connectivity", 100, "on", attrs={"restored": True})
    assert facts(h, r)["assessment"] == "insufficient_data"
    h, r = fixture()
    event(h, "flame", 100, "on")
    event(h, "flame", 100, "off")
    assert facts(h, r)["recorder"]["conflicting_states"] == 1
    h, r = fixture()
    event(h, "flame", 86401, "on")
    assert facts(h, r)["burner"]["observed_ignitions"] == 0


def test_idle_with_complete_feedback_is_zero_without_synthetic_cycle_lengths():
    h, r = fixture()
    f = facts(h, r)
    assert f["assessment"] == "no_anomaly_found"
    assert f["burner"]["observed_ignitions"] == 0 and f["burner"]["observed_seconds"] == 0
    assert f["cycles"]["short_heating"] is None and f["cycles"]["heating_median_seconds"] is None


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_calendar_windows(day, hours):
    h, r = fixture(day)
    f = facts(h, r, day)
    assert f["window_seconds"] == hours * 3600 and f["coverage_percent"] == 100


def test_profile_is_explicit_and_has_no_control_or_duplicate_sources():
    for update in (
        {"service": "climate.set_temperature"},
        {"short_burn_seconds": 0},
        {"sources": {k: "binary_sensor.same" for k in heating.SOURCES}},
        {"sources": {**PROFILE["sources"], "flame": "climate.thermostat"}},
    ):
        with pytest.raises(ValueError):
            heating.validate_profile({**PROFILE, **update})


def test_narrative_is_grounded_and_short_burn_findings_cannot_disappear():
    bundle = reporting.evidence_bundle(facts())
    assert bundle["scope"] == "heating_cycles" and "summary_options" in reporting.instructions(
        bundle
    )
    for option in bundle["summary_options"]:
        assert "short_burns" in option["refs"]
        result = reporting.validate_narrative({"summary": option["text"]}, bundle)
        assert result["evidence_refs"] == option["refs"] and result["suggested_check"]
        assert not result["hypothesis"]
    with pytest.raises(ValueError):
        reporting.validate_narrative({"summary": "A kazán biztosan meghibásodott."}, bundle)
    h, r = observed()
    event(h, "connectivity", 5000, "off")
    event(h, "connectivity", 5020, "on")
    partial = reporting.evidence_bundle(facts(h, r))
    result = reporting.validate_narrative(
        {"summary": partial["summary_options"][0]["text"]}, partial
    )
    assert result["summary"].startswith("A fűtési visszajelzések megfigyelése hiányos volt.")


async def test_profile_persistence_recorder_executor_and_report_scope_isolation(hass):
    await hass.config.async_set_time_zone("Europe/Budapest")
    owner = await running_coordinator(hass)
    try:
        saved = await owner.heating.set_profile("demo", PROFILE)
        with pytest.raises(ValueError, match="changed"):
            await owner.heating.set_profile("demo", PROFILE)
        await owner.heating.set_profile("demo", PROFILE, saved["profile_hash"])
        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=SimpleNamespace(async_add_executor_job=AsyncMock(return_value=observed())),
        ):
            f = await owner.heating.facts("demo", DAY)
        assert f["cycles"]["short_heating"] == 3
        bundle = reporting.evidence_bundle(f)
        response = {"summary": bundle["summary_options"][0]["text"]}
        with (
            patch.object(owner.heating, "facts", AsyncMock(return_value=f)),
            patch.object(
                reporting, "summarize", AsyncMock(return_value=SimpleNamespace(data=response))
            ) as ai,
        ):
            first = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), heating_profile="demo"
            )
            second = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), heating_profile="demo"
            )
        assert (
            first["attempt"]["status"] == "completed" and second["cached"] and ai.await_count == 1
        )
        snapshot = await owner.reports.read(None, heating_profile="demo")
        assert snapshot["policy_id"] == "heating:demo" and snapshot["heating_profile"] == "demo"
        with pytest.raises(ValueError):
            await owner.reports.read(None, energy_profile="demo", heating_profile="demo")
        with patch.object(owner.heating, "facts", AsyncMock(side_effect=RuntimeError("private"))):
            result = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), True, heating_profile="demo"
            )
        assert result["attempt"]["error"] == "collection_failed"
        assert (await owner.reports.read(None, heating_profile="demo"))["last_success"][
            "id"
        ] == first["attempt"]["id"]
    finally:
        await owner.async_close()
    owner = await running_coordinator(hass, entry=owner.entry)
    try:
        assert owner.heating.get_profile("demo")["profile"] == PROFILE
    finally:
        await owner.async_close()


async def test_native_actions_dispatch_correctly_and_permissions_are_preserved(hass):
    owner = await running_coordinator(hass)
    try:
        await owner.heating.set_profile("demo", PROFILE)
        await api.async_setup(hass)
        with (
            patch.object(api, "coordinator", return_value=owner),
            patch.object(
                owner.reports, "generate", AsyncMock(return_value={"cached": True})
            ) as generate,
        ):
            await hass.services.async_call(
                "home_insights",
                "generate_heating_report",
                {"profile_id": "demo", "ai_task_entity": "ai_task.example"},
                blocking=True,
                return_response=True,
            )
            assert (
                generate.call_args.kwargs["heating_profile"] == "demo"
                and generate.call_args.kwargs["energy_profile"] is None
            )
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        for name, data in (
            ("get_heating_facts", {"profile_id": "demo"}),
            ("set_heating_profile", {"profile_id": "demo", "profile": PROFILE}),
            (
                "generate_heating_report",
                {"profile_id": "demo", "ai_task_entity": "ai_task.example"},
            ),
        ):
            with pytest.raises(Unauthorized):
                await hass.services.async_call(
                    "home_insights",
                    name,
                    data,
                    blocking=True,
                    return_response=True,
                    context=Context(user_id="normal"),
                )
        connection = SimpleNamespace(user=SimpleNamespace(is_admin=False), send_result=Mock())
        with pytest.raises(Unauthorized):
            api.ws_heating_profile(hass, connection, {"id": 1, "profile_id": "demo"})
        with (
            patch.object(api, "coordinator", return_value=owner),
            pytest.raises(HomeAssistantError),
        ):
            await hass.services.async_call(
                "home_insights",
                "get_heating_facts",
                {"profile_id": "demo", "date": "2099-01-01"},
                blocking=True,
                return_response=True,
            )
    finally:
        await owner.async_close()
