"""Morning reports: truth, provider isolation, failures, persistence and scheduling."""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized

from custom_components.home_insights import api, report_store, reporting
from custom_components.home_insights.models import daily_window

from .test_runtime import running_coordinator

DAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 10, 8, 12, tzinfo=ZoneInfo("Europe/Budapest")).timestamp()
POLICY = {"version": 1, "revision": "reports.1", "rules": []}
TEXT = {
    "summary": "A kazán jelzése az előző napról húzódik át.",
    "hypothesis": "",
    "suggested_check": "Kazán: nézd meg az előző napi állapotelőzményeket és a jelzés részleteit.",
    "evidence_ids": "episode_1",
}


def facts(assessment="attention_needed"):
    a, b = daily_window(DAY, "Europe/Budapest")
    return {
        "schema_version": 2,
        "scope": "canonical_attention_policy",
        "day": DAY.isoformat(),
        "timezone": "Europe/Budapest",
        "assessment": assessment,
        "data_quality": "complete",
        "start": datetime.fromtimestamp(a, ZoneInfo("UTC")).isoformat(),
        "end": datetime.fromtimestamp(b, ZoneInfo("UTC")).isoformat(),
        "coverage_seconds": b - a,
        "window_seconds": b - a,
        "counts": {"issues": {"sources_present": 1}, "updates": {}, "events": {}},
        "episodes": [
            {
                "id": "episode",
                "name": "Kazán",
                "source": "binary_sensor.boiler_fault",
                "category": "issues",
                "kind": "incident",
                "present_at_period_start": True,
            }
        ],
        "availability": {"whole_day_details": True, "sources_with_gaps": 0, "details": []},
        "household_context": {"status": "observed", "snapshot": {"rules": []}},
        "handling_events": [],
        "limitations": [],
    }


async def report_coordinator(hass):
    await hass.config.async_set_time_zone("Europe/Budapest")
    owner = await running_coordinator(hass)
    await owner.attention.set_policy("demo", POLICY)
    return owner


async def test_generate_is_persisted_cached_and_only_interprets_prepared_facts(hass):
    owner = await report_coordinator(hass)
    try:
        with (
            patch.object(owner, "async_daily_facts", AsyncMock(return_value=facts())),
            patch.object(
                reporting, "summarize", AsyncMock(return_value=SimpleNamespace(data=TEXT))
            ) as ai,
        ):
            first = await owner.reports.generate("demo", "ai_task.example", DAY, Context())
            second = await owner.reports.generate("demo", "ai_task.example", DAY, Context())
        assert first["attempt"]["status"] == "completed" and second["cached"]
        assert ai.await_count == 1
        assert ai.call_args.args[2]["evidence"][0]["present_at_period_start"] is True
        assert first["attempt"]["facts"]["coverage_percent"] == 100
        with patch.object(reporting.time, "time", return_value=NOW):
            snapshot = await owner.reports.read("demo")
        assert snapshot["state"] == "attention_needed"
        assert snapshot["attempt"]["id"] == snapshot["last_success"]["id"]
    finally:
        await owner.async_close()
    reopened = await running_coordinator(hass, entry=owner.entry)
    try:
        with patch.object(reporting.time, "time", return_value=NOW):
            snapshot = await reopened.reports.read("demo")
        assert snapshot["attempt"]["id"] == first["attempt"]["id"]
    finally:
        await reopened.async_close()


@pytest.mark.parametrize(
    "error,code",
    [
        (RuntimeError("private detail"), "ai_failed"),
        (PermissionError(), "unsafe_provider"),
        (TimeoutError(), "timeout"),
    ],
)
async def test_failed_attempt_preserves_earlier_success_and_collected_facts(hass, error, code):
    owner = await report_coordinator(hass)
    try:
        with (
            patch.object(owner, "async_daily_facts", AsyncMock(return_value=facts())),
            patch.object(
                reporting, "summarize", AsyncMock(return_value=SimpleNamespace(data=TEXT))
            ),
        ):
            success = await owner.reports.generate("demo", "ai_task.example", DAY, Context())
        with (
            patch.object(owner, "async_daily_facts", AsyncMock(return_value=facts())),
            patch.object(reporting, "summarize", AsyncMock(side_effect=error)),
        ):
            failed = await owner.reports.generate("demo", "ai_task.example", DAY, Context(), True)
        with patch.object(reporting.time, "time", return_value=NOW):
            snapshot = await owner.reports.read("demo")
        assert snapshot["state"] == "failed"
        assert failed["attempt"]["error"] == code
        assert failed["attempt"]["facts"]["counts"] == facts()["counts"]
        assert snapshot["last_success"]["id"] == success["attempt"]["id"]
        assert "private detail" not in str(snapshot)
    finally:
        await owner.async_close()


async def test_collection_failure_and_no_observation_are_distinct(hass):
    owner = await report_coordinator(hass)
    try:
        with patch.object(owner, "async_daily_facts", AsyncMock(side_effect=RuntimeError())):
            failed = await owner.reports.generate("demo", "ai_task.example", DAY, Context())
        assert failed["attempt"]["status"] == "failed" and failed["attempt"]["facts"] is None
        with (
            patch.object(owner, "async_daily_facts", AsyncMock(return_value=facts("not_run"))),
            patch.object(reporting, "summarize", AsyncMock()) as ai,
        ):
            empty = await owner.reports.generate("demo", "ai_task.example", DAY, Context(), True)
        assert (
            empty["attempt"]["status"] == "completed" and empty["attempt"]["ai_status"] == "skipped"
        )
        ai.assert_not_called()
    finally:
        await owner.async_close()


async def test_running_cancel_and_restart_are_never_success(hass):
    owner = await report_coordinator(hass)
    started = asyncio.Event()

    async def wait(*_):
        started.set()
        await asyncio.Event().wait()

    try:
        with (
            patch.object(owner, "async_daily_facts", AsyncMock(return_value=facts())),
            patch.object(reporting, "summarize", wait),
        ):
            task = asyncio.create_task(
                owner.reports.generate("demo", "ai_task.example", DAY, Context())
            )
            await started.wait()
            with pytest.raises(ValueError, match="already running"):
                await owner.reports.generate("demo", "ai_task.example", DAY, Context())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        value = await owner.execute(
            report_store.read_reports, owner.journal, "demo", DAY.isoformat()
        )
        assert value["attempt"]["status"] == "failed" and value["attempt"]["error"] == "interrupted"
    finally:
        await owner.async_close()


async def test_schedule_before_due_missing_and_stalled_old_success_cannot_hide_failure(hass):
    owner = await report_coordinator(hass)
    try:
        with patch.object(reporting.time, "time", return_value=NOW):
            assert (await owner.reports.read("demo"))["state"] == "missed"
        with patch.object(reporting.time, "time", return_value=NOW - 3600):
            assert (await owner.reports.read("demo"))["state"] == "waiting"
        await owner.execute(
            report_store.begin_report, owner.journal, "demo", DAY.isoformat(), "test", NOW - 180
        )
        with patch.object(reporting.time, "time", return_value=NOW):
            assert (await owner.reports.read("demo"))["state"] == "stalled"
    finally:
        await owner.async_close()
    reopened = await running_coordinator(hass, entry=owner.entry)
    try:
        result = await reopened.execute(
            report_store.read_reports, reopened.journal, "demo", DAY.isoformat()
        )
        assert result["attempt"]["error"] == "interrupted"
    finally:
        await reopened.async_close()


def test_output_references_and_numbers_are_validated():
    bundle = reporting.evidence_bundle(facts())
    assert reporting.validate_narrative(TEXT, bundle)["evidence_refs"] == ["episode_1"]
    for changes in (
        {"evidence_ids": "made_up"},
        {"summary": "Két hiba, 50 százalék."},
        {"summary": ""},
        {"evidence_ids": ""},
    ):
        with pytest.raises(ValueError):
            reporting.validate_narrative({**TEXT, **changes}, bundle)
    data = facts()
    data["episodes"][0]["name"] = "Router 2"
    bundle = reporting.evidence_bundle(data)
    assert reporting.validate_narrative(
        {
            **TEXT,
            "summary": "Router 2 jelzését érdemes megnézni.",
            "suggested_check": bundle["permitted_checks"][0]["text"],
        },
        bundle,
    )
    data = facts()
    data["episodes"][0]["message"] = "See https://example.test/?token=SECRET"
    assert "SECRET" not in reporting.instructions(reporting.evidence_bundle(data))


def test_observation_attribution_partial_prefix_and_informal_voice():
    data = facts("insufficient_data")
    data["data_quality"] = "insufficient_data"
    bundle = reporting.evidence_bundle(data)
    result = reporting.validate_narrative(TEXT, bundle)
    assert result["summary"].startswith("Az előző nap csak részben volt megfigyelve.")
    assert not result["hypothesis"]
    for changes in (
        {"summary": "A termelés elmaradása hatással lehet az energiaellátásra."},
        {"suggested_check": "Ellenőrizze a rendszer beállításait."},
        {"suggested_check": "Ellenőrizd a beérkező Sunlight adatokat."},
        {"evidence_ids": "", "suggested_check": ""},
    ):
        with pytest.raises(ValueError):
            reporting.validate_narrative({**TEXT, **changes}, bundle)


async def test_actual_native_ai_task_api_has_no_llm_api_attachments_or_provider_tools(hass):
    from homeassistant.components import ai_task
    from homeassistant.components.ai_task.const import DATA_COMPONENT

    native = type(
        "OpenAITaskEntity",
        (),
        {"__module__": "homeassistant.components.openai_conversation.ai_task"},
    )()
    native.subentry = SimpleNamespace(data={})
    hass.data[DATA_COMPONENT] = SimpleNamespace(get_entity=lambda _: native)
    context = Context()
    with patch.object(
        ai_task, "async_generate_data", AsyncMock(return_value=SimpleNamespace(data=TEXT))
    ) as call:
        await reporting.summarize(
            hass, "ai_task.example", reporting.evidence_bundle(facts()), context
        )
        assert call.call_args.kwargs["llm_api"] is None
        assert call.call_args.kwargs["attachments"] is None
        assert call.call_args.kwargs["context"] is context
        for option in ("web_search", "code_interpreter", "llm_hass_api"):
            native.subentry.data = {option: True}
            with pytest.raises(PermissionError):
                await reporting.summarize(
                    hass, "ai_task.example", reporting.evidence_bundle(facts()), context
                )
        assert call.await_count == 1
    assert not reporting.readonly_provider(SimpleNamespace(subentry=SimpleNamespace(data={})))


def test_output_schema_checks_are_bound_and_repeated_source_names_are_valid():
    import probatio
    from homeassistant.helpers import llm

    data = facts()
    data["episodes"].append({**data["episodes"][0], "id": "second"})
    bundle = reporting.evidence_bundle(data)
    schema = probatio.to_openapi(
        reporting.output_schema(bundle),
        custom_serializer=llm.selector_serializer,
        openapi_version="3.1.0",
    )
    assert schema["properties"]["hypothesis"]["enum"] == [""]
    assert schema["properties"]["suggested_check"]["enum"] == [""]
    assert len(bundle["evidence"]) == 1
    assert reporting.validate_narrative({**TEXT, "evidence_ids": "episode_1"}, bundle)[
        "evidence_refs"
    ] == ["episode_1"]


async def test_service_and_private_report_ws_require_admin(hass):
    owner = await report_coordinator(hass)
    try:
        await api.async_setup(hass)
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        with patch.object(api, "coordinator", return_value=owner), pytest.raises(Unauthorized):
            await hass.services.async_call(
                "home_insights",
                "generate_report",
                {"policy_id": "demo", "ai_task_entity": "ai_task.example"},
                blocking=True,
                context=Context(user_id="reader"),
            )
        with pytest.raises(Unauthorized):
            api.ws_report(
                hass,
                SimpleNamespace(user=SimpleNamespace(is_admin=False)),
                {"id": 1, "type": "home_insights/report", "policy_id": "demo"},
            )
    finally:
        await owner.async_close()


def test_monitoring_problem_is_explicit_observation_language_not_physical_diagnosis():
    bundle = reporting.evidence_bundle(facts())
    assert reporting.validate_narrative(
        {**TEXT, "summary": "A Kazán felügyeleti probléma a megadott időszakban felmerült."}, bundle
    )["evidence_refs"] == ["episode_1"]
    with pytest.raises(ValueError):
        reporting.validate_narrative(
            {**TEXT, "summary": "A Kazán biztosan fizikailag meghibásodott."}, bundle
        )


def test_ai_cannot_change_selected_source_lifecycle_in_persisted_summary():
    bundle = reporting.evidence_bundle(facts())
    result = reporting.validate_narrative(
        {**TEXT, "summary": "A Kazán jelzése rendeződött."}, bundle
    )
    assert "nap végén is fennállt" in result["summary"]
    assert "rendeződött" not in result["summary"]


def test_native_selection_derives_checks_only_from_selected_unresolved_sources():
    bundle = reporting.evidence_bundle(facts())
    result = reporting.validate_narrative({**TEXT, "suggested_check": ""}, bundle)
    assert result["suggested_check"].startswith("Kazán:")
    data = facts()
    data["episodes"][0].update(closed_at="2026-11-09T20:00:00Z", close_reason="condition_cleared")
    bundle = reporting.evidence_bundle(data)
    result = reporting.validate_narrative({**TEXT, "suggested_check": ""}, bundle)
    assert result["suggested_check"] == ""
