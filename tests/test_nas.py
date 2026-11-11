"""NAS health, recorded temperatures, UPS tokens and honest observation quality."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.home_insights import api, nas, reporting
from custom_components.home_insights.models import daily_window

from .test_runtime import running_coordinator

DAY = date(2026, 11, 10)
PROFILE = nas.validate_profile(
    {
        "version": 1,
        "revision": "demo.1",
        "sources": {
            "array_running": "binary_sensor.array",
            "parity_problem": "binary_sensor.parity_problem",
            "parity_running": "binary_sensor.parity_running",
            "ups_status": "sensor.ups_status",
        },
        "disks": [
            {
                "id": "disk1",
                "name": "Disk one",
                "health": "binary_sensor.disk1_health",
                "temperature": {
                    "entity": "binary_sensor.disk1_health",
                    "attribute": "temperature",
                    "unit": "°C",
                },
                "warn_celsius": 50,
            }
        ],
    }
)


def fixture(day=DAY):
    a, b = daily_window(day, "Europe/Budapest")
    h = {}
    for entity in nas.source_ids(PROFILE):
        state = (
            "OL"
            if entity == "sensor.ups_status"
            else "on"
            if entity == "binary_sensor.array"
            else "off"
        )
        h[entity] = [
            State(
                entity,
                state,
                attributes={"temperature": 35, "spinning": True, "errors": 0},
                last_updated=datetime.fromtimestamp(a - 3600, UTC),
            )
        ]
    return h, [{"start": a - 7200, "end": b, "closed_incorrect": False}]


def event(h, entity, at, state, attrs=None, day=DAY):
    a, _ = daily_window(day, "Europe/Budapest")
    h[entity].append(
        State(
            entity, state, attributes=attrs or {}, last_updated=datetime.fromtimestamp(a + at, UTC)
        )
    )


def compute(h=None, r=None, day=DAY, profile=PROFILE):
    if h is None:
        h, r = fixture(day)
    return nas.compute_facts(profile, "demo", day, "Europe/Budapest", h, r)


def test_clean_day_and_running_parity_are_not_faults():
    h, r = fixture()
    event(h, "binary_sensor.parity_running", 60, "on")
    event(h, "binary_sensor.parity_running", 3660, "off")
    f = compute(h, r)
    assert f["assessment"] == "no_anomaly_found" and f["coverage_percent"] == 100
    assert f["signals"]["array_running"]["active_seconds"] == 86400
    assert f["signals"]["array_running"]["inactive_seconds"] == 0
    assert f["signals"]["parity_running"]["active_seconds"] == 3600
    assert f["signals"]["parity_running"]["observed_starts"] == 1
    assert f["disks"][0]["maximum_celsius"] == 35
    assert f["signals"]["ups_status"]["observed_starts"] == 0


@pytest.mark.parametrize(
    "entity,state,attrs",
    [
        ("binary_sensor.array", "off", {}),
        ("binary_sensor.parity_problem", "on", {"errors": 3}),
        ("binary_sensor.disk1_health", "on", {"temperature": 35, "spinning": True}),
        ("sensor.ups_status", "OL LB", {}),
        ("sensor.ups_status", "OB", {}),
        ("sensor.ups_status", "OL RB", {}),
    ],
)
def test_measured_findings_take_priority_with_partial_observation(entity, state, attrs):
    h, r = fixture()
    event(h, entity, 600, state, attrs)
    event(h, "binary_sensor.parity_running", 8000, "unavailable")
    f = compute(h, r)
    assert f["assessment"] == "attention_needed"
    assert f["data_quality"] == "insufficient_data"
    bundle = reporting.evidence_bundle(f)
    for option in bundle["summary_options"]:
        result = reporting.validate_narrative({"summary": option["text"]}, bundle)
        assert result["evidence_refs"] and not result["hypothesis"]


def test_battery_starts_are_edges_not_initial_observations_or_gap_recoveries():
    h, r = fixture()
    for at, state in (
        (60, "OB DISCHRG"),
        (120, "OL CHRG"),
        (180, "unavailable"),
        (200, "OB"),
        (240, "OL"),
    ):
        event(h, "sensor.ups_status", at, state)
    f = compute(h, r)
    assert f["signals"]["ups_status"]["observed_starts"] == 1
    assert f["signals"]["ups_status"]["active_seconds"] == 100
    assert f["counts_are_lower_bounds"]


def test_hot_peak_is_not_sustained_and_standby_is_not_zero_temperature():
    h, r = fixture()
    event(h, "binary_sensor.disk1_health", 60, "off", {"temperature": 55, "spinning": True})
    event(h, "binary_sensor.disk1_health", 360, "off", {"temperature": 0, "standby": True})
    f = compute(h, r)
    assert f["disks"][0]["maximum_celsius"] == 55
    assert f["disks"][0]["hot_seconds"] == 300 and not f["disks"][0]["sustained_hot"]
    assert f["disks"][0]["standby_seconds"] == 86040
    assert f["assessment"] == "no_anomaly_found"
    event(h, "binary_sensor.disk1_health", 600, "off", {"temperature": 55, "spinning": True})
    event(h, "binary_sensor.disk1_health", 1200, "off", {"temperature": 40, "spinning": True})
    assert compute(h, r)["disks"][0]["sustained_hot"]
    assert compute(h, r)["assessment"] == "attention_needed"


def test_unknown_restored_or_zero_temperatures_are_never_a_clean_measurement():
    for value in (
        {"temperature": 0},
        {"temperature": float("nan")},
        {"temperature": 35, "restored": True},
        {},
    ):
        h, r = fixture()
        event(h, "binary_sensor.disk1_health", 0, "off", value)
        f = compute(h, r)
        assert f["assessment"] == "insufficient_data" and f["disks"][0]["maximum_celsius"] is None


def test_separate_temperature_missing_during_confirmed_standby_is_expected():
    p = nas.validate_profile(
        {
            **PROFILE,
            "disks": [
                {
                    **PROFILE["disks"][0],
                    "temperature": {"entity": "sensor.disk_temperature", "unit": "°C"},
                }
            ],
        }
    )
    h, r = fixture()
    a, _ = daily_window(DAY, "Europe/Budapest")
    h["sensor.disk_temperature"] = [
        State(
            "sensor.disk_temperature",
            "unavailable",
            last_updated=datetime.fromtimestamp(a - 3600, UTC),
        )
    ]
    event(h, "binary_sensor.disk1_health", 0, "off", {"standby": True})
    f = compute(h, r, profile=p)
    assert f["assessment"] == "no_anomaly_found"
    assert f["disks"][0]["standby_seconds"] == 86400 and f["disks"][0]["maximum_celsius"] is None
    event(h, "binary_sensor.disk1_health", 60, "off", {})
    assert compute(h, r, profile=p)["assessment"] == "insufficient_data", (
        "healthy is not proof of standby"
    )


def test_minor_idle_gaps_are_disclosed_without_invalidating_whole_day():
    h, r = fixture()
    event(h, "sensor.ups_status", 600, "unavailable")
    event(h, "sensor.ups_status", 660, "OL")
    f = compute(h, r)
    assert f["data_quality"] == "minor_gaps" and f["assessment"] == "no_anomaly_found"
    assert f["coverage_percent"] < 100 and f["observation_gaps"]["count"] == 1
    event(h, "sensor.ups_status", 800, "unavailable")
    event(h, "sensor.ups_status", 921, "OL")
    assert compute(h, r)["assessment"] == "insufficient_data"


def test_recorder_restart_breaks_held_measurements_and_battery_continuity():
    h, r = fixture()
    a, b = daily_window(DAY, "Europe/Budapest")
    r = [
        {"start": a - 3600, "end": a + 600, "closed_incorrect": False},
        {"start": a + 620, "end": b, "closed_incorrect": False},
    ]
    f = compute(h, r)
    assert f["coverage_seconds"] == 600
    assert f["data_quality"] == "insufficient_data"
    for entity in h:
        old = h[entity][0]
        event(h, entity, 660, old.state, dict(old.attributes))
    f = compute(h, r)
    assert f["data_quality"] == "minor_gaps" and f["unobserved_seconds"] == 60
    assert f["signals"]["ups_status"]["observed_starts"] == 0


def test_missing_sources_and_unrecognized_ups_tokens_remain_unknown():
    h, r = fixture()
    h.clear()
    f = compute(h, r)
    assert f["assessment"] == "not_run" and f["disks"][0]["maximum_celsius"] is None
    h, r = fixture()
    event(h, "sensor.ups_status", 0, "OL UNRECOGNIZED")
    assert compute(h, r)["assessment"] == "insufficient_data"


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_days_use_local_midnights(day, hours):
    f = compute(day=day)
    assert f["window_seconds"] == hours * 3600 and f["coverage_percent"] == 100


async def test_profile_persistence_scope_cache_permissions_and_private_diagnostics(hass):
    await hass.config.async_set_time_zone("Europe/Budapest")
    owner = await running_coordinator(hass)
    try:
        saved = await owner.nas.set_profile("demo", PROFILE)
        with pytest.raises(ValueError, match="changed"):
            await owner.nas.set_profile("demo", PROFILE)
        await owner.nas.set_profile("demo", PROFILE, saved["profile_hash"])
        f = compute()
        bundle = reporting.evidence_bundle(f)
        with (
            patch.object(owner.nas, "facts", AsyncMock(return_value=f)),
            patch.object(
                reporting,
                "summarize",
                AsyncMock(
                    return_value=SimpleNamespace(
                        data={"summary": bundle["summary_options"][0]["text"]}
                    )
                ),
            ) as ai,
        ):
            first = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), nas_profile="demo"
            )
            second = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), nas_profile="demo"
            )
        assert (
            first["attempt"]["status"] == "completed" and second["cached"] and ai.await_count == 1
        )
        assert first["attempt"]["policy"] == "nas:demo"
        with pytest.raises(ValueError):
            await owner.reports.read(None, heating_profile="demo", nas_profile="demo")
        await api.async_setup(hass)
        with (
            patch.object(api, "coordinator", return_value=owner),
            patch.object(owner.nas, "facts", AsyncMock(return_value=f)),
        ):
            value = await hass.services.async_call(
                "home_insights",
                "get_nas_facts",
                {"profile_id": "demo", "date": DAY.isoformat()},
                blocking=True,
                return_response=True,
            )
        assert value["scope"] == "nas_health"
    finally:
        await owner.async_close()
    owner = await running_coordinator(hass, entry=owner.entry)
    try:
        assert owner.nas.get_profile("demo")["profile"] == PROFILE
    finally:
        await owner.async_close()


async def test_native_actions_dispatch_correctly_and_permissions_are_preserved(hass):
    owner = await running_coordinator(hass)
    try:
        await owner.nas.set_profile("demo", PROFILE)
        await api.async_setup(hass)
        with (
            patch.object(api, "coordinator", return_value=owner),
            patch.object(
                owner.reports, "generate", AsyncMock(return_value={"cached": True})
            ) as generate,
        ):
            await hass.services.async_call(
                "home_insights",
                "generate_nas_report",
                {"profile_id": "demo", "ai_task_entity": "ai_task.example"},
                blocking=True,
                return_response=True,
            )
            assert (
                generate.call_args.kwargs["nas_profile"] == "demo"
                and generate.call_args.kwargs["energy_profile"] is None
            )
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        for name, data in (
            ("get_nas_facts", {"profile_id": "demo"}),
            ("set_nas_profile", {"profile_id": "demo", "profile": PROFILE}),
            (
                "generate_nas_report",
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
            api.ws_nas_profile(hass, connection, {"id": 1, "profile_id": "demo"})
        with (
            patch.object(api, "coordinator", return_value=owner),
            pytest.raises(HomeAssistantError),
        ):
            await hass.services.async_call(
                "home_insights",
                "get_nas_facts",
                {"profile_id": "demo", "date": "2099-01-01"},
                blocking=True,
                return_response=True,
            )
    finally:
        await owner.async_close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("warn_celsius", float("nan")),
        ("warn_celsius", float("inf")),
        ("health", "switch.disk"),
        ("install_firmware", True),
    ],
)
def test_profile_rejects_invalid_or_control_fields(field, value):
    p = {**PROFILE, "disks": [{**PROFILE["disks"][0], field: value}]}
    with pytest.raises(ValueError):
        nas.validate_profile(p)
