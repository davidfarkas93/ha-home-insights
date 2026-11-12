"""Energy totals, missing history, native contracts and report scope isolation."""

import inspect
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.home_insights import api, energy, reporting
from custom_components.home_insights.models import daily_window, digest

from .test_runtime import running_coordinator

DAY = date(2026, 11, 9)
PROFILE = energy.validate_profile(
    {"version": 1, "revision": "test.1", "metrics": {k: "sensor." + k for k in energy.METRICS}}
)


def sample(day=DAY, zone="Europe/Budapest", missing=None):
    a, b = daily_window(day - timedelta(days=14), zone)
    start, end = daily_window(day, zone)
    del b
    rows, metadata, histories = {}, {}, {}
    for key, source in PROFILE["metrics"].items():
        total = 100.0
        points = []
        for at in range(int(a - 3600), int(end), 3600):
            # Lower solar, higher import, no charging on the examined day.
            value = (
                (0.1 if key == "solar" else 1 if key == "grid_import" else 0)
                if at >= start
                else 0.5
            )
            total += value
            points.append({"start": at, "end": at + 3600, "state": total, "sum": total})
        rows[source] = [p for p in points if p["start"] != missing]
        metadata[source] = (1, {"has_sum": True, "unit_of_measurement": "kWh"})
        histories[source] = [State(source, "100", last_updated=datetime.fromtimestamp(start, UTC))]
    return rows, metadata, histories


def facts():
    return energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", *sample())


def narrative():
    return {
        "summary": (
            "A napelemtermelés elmaradt a korábbi napok mediánjától. "
            "A hálózati import meghaladta a korábbi napok mediánját."
        ),
    }


def test_hourly_energy_and_prior_only_median_are_deterministic():
    result = facts()
    assert result["assessment"] == "attention_needed" and result["coverage_percent"] == 100
    metrics = {m["key"]: m for m in result["metrics"]}
    assert metrics["solar"]["value"] == 2.4
    assert metrics["grid_import"]["value"] == 24
    assert metrics["ev"]["value"] == 0
    assert metrics["solar"]["baseline"]["median"] == 12
    assert metrics["solar"]["delta_percent"] == -80
    assert metrics["solar"]["deviation"] == "below_recent_days"
    assert metrics["grid_import"]["deviation"] == "above_recent_days"
    assert all(
        s["day"] < result["day"] for m in result["metrics"] for s in m["baseline"]["samples"]
    )
    assert result["profile_hash"] == digest(PROFILE)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dst_days_use_actual_local_midnights(day, hours):
    result = energy.compute_facts(PROFILE, "demo", day, "Europe/Budapest", *sample(day))
    assert result["window_seconds"] == hours * 3600
    assert result["metrics"][1]["value"] == hours
    assert result["metrics"][1]["statistics"]["hours"] == hours


def test_missing_hour_or_starting_sum_never_becomes_zero_or_clean():
    start, end = daily_window(DAY, "Europe/Budapest")
    for missing in (start - 3600, start, start + 8 * 3600, end - 3600):
        result = energy.compute_facts(
            PROFILE, "demo", DAY, "Europe/Budapest", *sample(missing=missing)
        )
        assert result["assessment"] == "insufficient_data"
        assert all(m["value"] is None for m in result["metrics"])
    result = energy.measured_day([], start + 900, end + 900)
    assert result["reason"] == "unsupported_midnight_alignment"


@pytest.mark.parametrize("change", ["negative", "nan", "duplicate", "unsupported"])
def test_invalid_series_or_metadata_are_not_consumption(change):
    data, metadata, history = sample()
    source = PROFILE["metrics"]["solar"]
    if change == "negative":
        data[source][-1]["sum"] = 0
    elif change == "nan":
        data[source][-1]["sum"] = float("nan")
    elif change == "duplicate":
        data[source].append(data[source][-1])
    else:
        metadata[source][1]["unit_of_measurement"] = "W"
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["metrics"][0]["value"] is None and result["assessment"] == "insufficient_data"


def test_unknown_boundaries_and_recovered_internal_gap_have_distinct_meanings():
    data, metadata, history = sample()
    start, end = daily_window(DAY, "Europe/Budapest")
    source = PROFILE["metrics"]["ev"]
    history[source] += [
        State(source, "unavailable", last_updated=datetime.fromtimestamp(start + 100, UTC)),
        State(source, "101", last_updated=datetime.fromtimestamp(start + 102, UTC)),
    ]
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    metric = result["metrics"][-1]
    assert metric["value"] == 0 and metric["history"]["unavailable_seconds"] == 2
    assert metric["status"] == "complete"
    history[source].append(
        State(source, "unavailable", last_updated=datetime.fromtimestamp(end - 10, UTC))
    )
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["metrics"][-1]["value"] is None
    assert result["metrics"][-1]["status"] == "unknown_history_boundaries"
    history[source] = []
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["metrics"][-1]["value"] is None


def test_insufficient_baseline_keeps_verified_totals_but_cannot_be_clean():
    data, metadata, history = sample()
    start, _ = daily_window(DAY - timedelta(days=2), "Europe/Budapest")
    data = {k: [r for r in rows if r["start"] >= start] for k, rows in data.items()}
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["assessment"] == "insufficient_data"
    assert result["metrics"][0]["value"] == 2.4
    assert result["metrics"][0]["baseline"]["median"] is None
    assert result["metrics"][0]["delta_percent"] is None


def test_counter_resets_use_native_sums_and_future_rows_are_ignored():
    data, metadata, history = sample()
    _, end = daily_window(DAY, "Europe/Budapest")
    for rows in data.values():
        for row in rows:
            row["state"] = row["state"] % 2
        rows.append({"start": end, "end": end + 3600, "sum": 999999, "state": 999999})
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["metrics"][0]["value"] == 2.4


def test_clean_scope_requires_all_data_and_both_thresholds():
    data, metadata, history = sample()
    start, _ = daily_window(DAY, "Europe/Budapest")
    for rows in data.values():
        total = 100.0
        for row in rows:
            total += 0.5
            row["sum"] = total
            row["state"] = total
    result = energy.compute_facts(PROFILE, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["assessment"] == "no_anomaly_found"
    # Large percentage alone is not enough when the absolute difference is small.
    for row in data["sensor.solar"]:
        if row["start"] >= start:
            row["sum"] -= (row["start"] - start + 3600) / 3600 * 0.1
    strict = {**PROFILE, "deviation_percent": 10}
    result = energy.compute_facts(strict, "demo", DAY, "Europe/Budapest", data, metadata, history)
    assert result["metrics"][0]["delta_percent"] == -20
    assert result["metrics"][0]["deviation"] is None


def test_profile_contract_forbids_actions_unknown_fields_and_invalid_bindings():
    for change in (
        {"shell": "anything"},
        {"minimum_baseline_days": 15},
        {"deviation_percent": float("nan")},
        {"metrics": {k: "sensor.same" for k in energy.METRICS}},
        {"metrics": {**PROFILE["metrics"], "ev": "switch.charger"}},
    ):
        with pytest.raises(ValueError):
            energy.validate_profile({**PROFILE, **change})


def test_energy_ai_receives_only_measured_projection_and_source_bound_checks():
    bundle = reporting.evidence_bundle(facts())
    assert bundle["scope"] == "energy_statistics"
    assert "permitted_checks" in reporting.instructions(bundle)
    assert reporting.validate_narrative(narrative(), bundle)["evidence_refs"] == [
        "solar",
        "grid_import",
    ]
    schema = reporting.output_schema(bundle)
    assert schema(narrative()) == narrative()
    for change in (
        {"evidence_ids": "made_up"},
        {"summary": "A Napelemtermelés 50 százalékkal esett."},
        {"hypothesis": "Biztosan hibás az inverter."},
        {"suggested_check": "Kapcsold le az invertert."},
        {"summary": "Minden rendben.", "evidence_ids": "ev"},
    ):
        with pytest.raises(ValueError):
            reporting.validate_narrative({**narrative(), **change}, bundle)


def test_energy_summary_choices_keep_all_findings_and_derive_exact_sources():
    bundle = reporting.evidence_bundle(facts())
    assert len(bundle["summary_options"]) == 2
    for option in bundle["summary_options"]:
        assert set(option["refs"]) == {"solar", "grid_import"}
        result = reporting.validate_narrative({"summary": option["text"]}, bundle)
        assert result["evidence_refs"] == option["refs"]
        assert result["suggested_check"] in [c["text"] for c in bundle["permitted_checks"]]
        assert result["hypothesis"] == ""
    assert all("samples" not in e["baseline"] for e in bundle["evidence"])
    for summary in (
        "A napelemtermelés elmaradt a korábbi napok átlagától.",
        "Biztosan meghibásodott az inverter.",
        "Az autótöltés rögzített energiája nem nőtt.",
    ):
        with pytest.raises(ValueError):
            reporting.validate_narrative({"summary": summary}, bundle)


async def test_profile_persists_hash_guard_and_facts_use_recorder_executor(hass):
    owner = await running_coordinator(hass)
    try:
        await hass.config.async_set_time_zone("Europe/Budapest")
        saved = await owner.energy.set_profile("demo", PROFILE)
        with pytest.raises(ValueError, match="changed"):
            await owner.energy.set_profile("demo", PROFILE)
        await owner.energy.set_profile("demo", PROFILE, saved["profile_hash"])
        from homeassistant.components.recorder import get_instance

        with patch(
            "homeassistant.components.recorder.get_instance",
            return_value=SimpleNamespace(async_add_executor_job=AsyncMock(return_value=sample())),
        ) as native:
            result = await owner.energy.facts("demo", DAY)
        assert result["metrics"][0]["value"] == 2.4
        job = native.return_value.async_add_executor_job.call_args.args[0]
        assert job.func is energy.read_recorder
        inspect.signature(energy.read_recorder).bind(*job.args, **job.keywords)
        assert (
            job.args[1].timestamp() < daily_window(DAY - timedelta(days=14), "Europe/Budapest")[0]
        )
        del get_instance
    finally:
        await owner.async_close()
    reopened = await running_coordinator(hass, entry=owner.entry)
    try:
        assert reopened.energy.get_profile("demo")["profile"] == PROFILE
    finally:
        await reopened.async_close()


def test_native_recorder_adapter_is_read_only_and_uses_energy_conversion(hass):
    from homeassistant.components.recorder import history, statistics

    start = datetime(2026, 11, 9, tzinfo=UTC)
    sources = ["sensor.solar"]
    native_stats = statistics.statistics_during_period
    native_history = history.get_significant_states
    with (
        patch.object(statistics, "get_metadata", return_value={}),
        patch.object(statistics, "statistics_during_period", return_value={}) as stats,
        patch.object(history, "get_significant_states", return_value={}) as hist,
    ):
        energy.read_recorder(
            hass, start - timedelta(days=14), start + timedelta(days=1), start, sources
        )
    inspect.signature(native_stats).bind(*stats.call_args.args, **stats.call_args.kwargs)
    inspect.signature(native_history).bind(*hist.call_args.args, **hist.call_args.kwargs)
    assert stats.call_args.args[5] == {"energy": "kWh"}
    assert hist.call_args.kwargs == {"include_start_time_state": True, "no_attributes": False}


async def test_energy_reports_are_persisted_cached_and_isolated_from_attention(hass):
    owner = await running_coordinator(hass)
    try:
        await hass.config.async_set_time_zone("Europe/Budapest")
        await owner.energy.set_profile("demo", PROFILE)
        await owner.attention.set_policy("demo", {"version": 1, "revision": "test.1", "rules": []})
        with (
            patch.object(owner.energy, "facts", AsyncMock(return_value=facts())),
            patch.object(
                reporting, "summarize", AsyncMock(return_value=SimpleNamespace(data=narrative()))
            ) as ai,
        ):
            first = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), energy_profile="demo"
            )
            second = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), energy_profile="demo"
            )
        assert first["attempt"]["status"] == "completed" and second["cached"]
        assert ai.await_count == 1
        snapshot = await owner.reports.read(None, energy_profile="demo")
        assert snapshot["policy_id"] == "energy:demo" and snapshot["energy_profile"] == "demo"
        assert snapshot["last_success"]["id"] == first["attempt"]["id"]
        assert (await owner.reports.read("demo"))["last_success"] is None
        with patch.object(owner.energy, "facts", AsyncMock(side_effect=RuntimeError("private"))):
            failed = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), True, energy_profile="demo"
            )
        assert failed["attempt"]["error"] == "collection_failed"
        assert (await owner.reports.read(None, energy_profile="demo"))["last_success"][
            "id"
        ] == first["attempt"]["id"]
    finally:
        await owner.async_close()


async def test_missing_energy_data_skips_ai_and_admin_boundaries_are_enforced(hass):
    owner = await running_coordinator(hass)
    try:
        await owner.energy.set_profile("demo", PROFILE)
        data = facts()
        data["assessment"] = "insufficient_data"
        for metric in data["metrics"]:
            metric["value"] = None
        with (
            patch.object(owner.energy, "facts", AsyncMock(return_value=data)),
            patch.object(reporting, "summarize", AsyncMock()) as ai,
        ):
            result = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), energy_profile="demo"
            )
        assert result["attempt"]["ai_status"] == "skipped"
        ai.assert_not_called()
        await api.async_setup(hass)
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        with patch.object(api, "coordinator", return_value=owner):
            for service, payload in (
                ("set_energy_profile", {"profile_id": "demo", "profile": PROFILE}),
                ("get_energy_facts", {"profile_id": "demo"}),
                (
                    "generate_energy_report",
                    {"profile_id": "demo", "ai_task_entity": "ai_task.example"},
                ),
            ):
                with pytest.raises(Unauthorized):
                    await hass.services.async_call(
                        "home_insights",
                        service,
                        payload,
                        blocking=True,
                        return_response=True,
                        context=Context(user_id="normal"),
                    )
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "home_insights",
                    "get_energy_facts",
                    {"profile_id": "demo", "date": "2099-01-01"},
                    blocking=True,
                    return_response=True,
                )
        connection = SimpleNamespace(user=SimpleNamespace(is_admin=False), send_result=Mock())
        for handler, msg in (
            (api.ws_energy_profile, {"profile_id": "demo"}),
            (api.ws_report, {"energy_profile": "demo"}),
        ):
            with pytest.raises(Unauthorized):
                handler(hass, connection, {"id": 1, **msg})
        assert not connection.send_result.called
    finally:
        await owner.async_close()
