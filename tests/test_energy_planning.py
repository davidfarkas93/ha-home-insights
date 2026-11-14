"""A fresh forecast can suggest timing without certifying surplus or charging need."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from homeassistant.core import State

from custom_components.home_insights.energy import validate_profile
from custom_components.home_insights.energy_planning import collect, narrative

NOW = datetime(2026, 11, 14, 7, 0, tzinfo=UTC).timestamp()
CONFIG = {
    "forecast_entity": "sensor.forecast",
    "remaining_entity": "sensor.remaining",
    "peak_power_entity": "sensor.peak",
    "peak_time_entity": "sensor.peak_at",
    "updated_entity": "sensor.polled",
    "minimum_charging_power_entity": "sensor.minimum",
    "allow_grid_assist": True,
    "minimum_remaining_kwh": 5,
    "minimum_peak_kw": 2,
    "maximum_age_hours": 6,
}


def example():
    data = {}

    def state(entity, value, unit=None, **attributes):
        data[entity] = State(
            entity,
            str(value),
            {**({"unit_of_measurement": unit} if unit else {}), **attributes},
            last_updated=datetime.fromtimestamp(NOW, UTC),
        )

    state("sensor.forecast", 22, "kWh")
    state("sensor.remaining", 21, "kWh")
    state("sensor.peak", 3600, "W")
    state("sensor.peak_at", "2026-11-14T11:00:00+00:00")
    state("sensor.polled", "2026-11-14T06:55:00+00:00")
    state("sensor.minimum", 4.1, "kW")
    return SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Budapest"), states=SimpleNamespace(get=data.get)
    ), data


def test_partial_pv_opportunity_is_not_a_guaranteed_solar_only_charge():
    hass, _ = example()
    result = collect(hass, CONFIG, date(2026, 11, 13), NOW)
    assert result["status"] == "opportunity"
    assert result["day"] == "2026-11-14" and result["peak_kw"] == 3.6
    assert result["peak_kw"] < result["minimum_charging_kw"]
    assert not result["surplus_known"] and not result["charging_need_known"]
    text = narrative(result)
    assert "ha töltenéd" in text["headline"] and "rásegítésre" in text["summary"]
    assert text["valid_until"] <= result["peak_at"] + 2 * 3600
    assert collect(hass, CONFIG, date(2026, 11, 12), NOW) is None


@pytest.mark.parametrize(
    "source,value,unit,extra",
    [
        ("sensor.forecast", "unavailable", "kWh", {}),
        ("sensor.remaining", "nan", "kWh", {}),
        ("sensor.peak", 3.6, "kWh", {}),
        ("sensor.peak", 3600, "W", {"restored": True}),
        ("sensor.peak_at", "2026-11-15T11:00:00+00:00", None, {}),
        ("sensor.polled", "2026-11-13T23:00:00+00:00", None, {}),
        ("sensor.polled", "2026-11-14T07:01:00+00:00", None, {}),
        ("sensor.remaining", 23, "kWh", {}),
    ],
)
def test_unknown_stale_future_wrong_day_or_inconsistent_data_never_becomes_opportunity(
    source, value, unit, extra
):
    hass, data = example()
    data[source] = State(
        source,
        str(value),
        {**({"unit_of_measurement": unit} if unit else {}), **extra},
        last_updated=datetime.fromtimestamp(NOW, UTC),
    )
    assert collect(hass, CONFIG, date(2026, 11, 13), NOW)["status"] == "insufficient_data"


def test_low_forecast_elapsed_window_and_no_grid_permission_are_distinct():
    hass, data = example()
    data["sensor.remaining"] = State(
        "sensor.remaining",
        "1",
        {"unit_of_measurement": "kWh"},
        last_updated=datetime.fromtimestamp(NOW, UTC),
    )
    assert collect(hass, CONFIG, date(2026, 11, 13), NOW)["status"] == "no_opportunity"
    assert (
        collect(hass, {**CONFIG, "allow_grid_assist": False}, date(2026, 11, 13), NOW)["reason"]
        == "verified_surplus_required"
    )
    hass, data = example()
    later = NOW + 7 * 3600
    for key, item in list(data.items()):
        data[key] = State(
            key, item.state, item.attributes, last_updated=datetime.fromtimestamp(later, UTC)
        )
    data["sensor.polled"] = State(
        "sensor.polled",
        datetime.fromtimestamp(later, UTC).isoformat(),
        last_updated=datetime.fromtimestamp(later, UTC),
    )
    assert collect(hass, CONFIG, date(2026, 11, 13), later)["status"] == "elapsed"


def test_profile_validates_finite_explicit_advice_thresholds_and_preserves_mappings():
    profile = {
        "version": 1,
        "revision": "test",
        "metrics": {k: "sensor." + k for k in ["solar", "grid_import", "grid_export", "ev"]},
        "charging_planning": CONFIG,
    }
    assert validate_profile(profile)["charging_planning"] == CONFIG
    for key in ["minimum_remaining_kwh", "minimum_peak_kw"]:
        with pytest.raises(ValueError):
            validate_profile({**profile, "charging_planning": {**CONFIG, key: float("nan")}})


async def test_report_persists_planning_separately_from_historical_measurements(hass):
    from unittest.mock import AsyncMock, patch

    from homeassistant.core import Context

    from custom_components.home_insights import reporting

    from .test_energy import PROFILE, facts
    from .test_energy import narrative as yesterday_narrative
    from .test_runtime import running_coordinator

    owner = await running_coordinator(hass)
    try:
        await owner.energy.set_profile("demo", PROFILE)
        measured = facts()
        report_day = date.fromisoformat(measured["day"])
        example_hass, _ = example()
        planning = collect(example_hass, CONFIG, date(2026, 11, 13), NOW)
        with (
            patch.object(owner.energy, "facts", AsyncMock(return_value=measured)),
            patch.object(owner.energy, "planning", return_value=planning),
            patch.object(
                reporting,
                "summarize",
                AsyncMock(return_value=SimpleNamespace(data=yesterday_narrative())),
            ),
        ):
            result = await owner.reports.generate(
                None, "ai_task.example", report_day, Context(), energy_profile="demo"
            )
        attempt = result["attempt"]
        assert attempt["status"] == "completed"
        assert attempt["facts"]["metrics"] == measured["metrics"]
        assert attempt["facts"]["day"] == measured["day"]
        assert attempt["facts"]["today_planning"] == planning
        assert "napközbenre" in attempt["narrative"]["today"]["headline"]
        assert "napközbenre" not in attempt["narrative"]["summary"]
    finally:
        await owner.async_close()
