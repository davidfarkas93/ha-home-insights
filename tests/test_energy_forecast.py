"""A forecast reference must predate its fixed cutoff and have known units/day."""

from datetime import UTC, date, datetime

from homeassistant.core import State

from custom_components.home_insights.energy_forecast import snapshot

DAY = date(2026, 11, 11)
CONFIG = {"entity": "sensor.forecast", "snapshot_time": "06:00", "maximum_age_hours": 6}


def row(at, value="3", unit="kWh", **extra):
    return State(
        "sensor.forecast",
        value,
        attributes={"unit_of_measurement": unit, **extra},
        last_updated=datetime.fromisoformat(at).astimezone(UTC),
    )


def test_fixed_local_cutoff_does_not_use_hindsight_or_yesterday():
    rows = [
        row("2026-11-10T23:59:00+01:00", "20"),
        row("2026-11-11T05:30:00+01:00", "3000", "Wh"),
        row("2026-11-11T06:30:00+01:00", "15"),
    ]
    result = snapshot(rows, CONFIG, DAY, "Europe/Budapest")
    assert result["value_kwh"] == 3 and result["status"] == "available"
    assert result["comparison_basis"] == "independent_forecast"
    for invalid in (
        [rows[0]],
        [rows[2]],
        [row("2026-11-11T05:30:00+01:00", "unknown")],
        [row("2026-11-11T05:30:00+01:00", "3", "W")],
        [row("2026-11-11T05:30:00+01:00", restored=True)],
        [row("2026-11-11T05:30:00+01:00", "nan")],
    ):
        assert snapshot(invalid, CONFIG, DAY, "Europe/Budapest")["value_kwh"] is None
    assert (
        snapshot([rows[1], rows[1]], CONFIG, DAY, "Europe/Budapest")["status"]
        == "ambiguous_snapshot"
    )
