"""Keep precise rejection evidence privately, with a bounded sanitized candidate."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context

from custom_components.home_insights import report_diagnostics, report_store, reporting

from .test_reports import DAY, NOW, TEXT, facts, report_coordinator
from .test_runtime import running_coordinator


async def test_rejection_survives_restart_without_publishing_invalid_text(hass):
    owner = await report_coordinator(hass)
    bad = {**TEXT, "evidence_ids": "invented_reference"}
    try:
        with (
            patch.object(owner, "async_daily_facts", AsyncMock(return_value=facts())),
            patch.object(reporting, "summarize", AsyncMock(return_value=SimpleNamespace(data=bad))),
        ):
            result = await owner.reports.generate("demo", "ai_task.example", DAY, Context())
        attempt = result["attempt"]
        assert attempt["status"] == "failed" and attempt["error"] == "invalid_ai_output"
        assert attempt["narrative"] is None
        diagnostic = attempt["diagnostics"]
        assert diagnostic["reason"] == "unknown_evidence"
        assert json.loads(diagnostic["candidate_json"]) == bad
        assert diagnostic["allowed_evidence_refs"] == ["episode_1"]
    finally:
        await owner.async_close()
    owner = await running_coordinator(hass, entry=owner.entry)
    try:
        with patch.object(reporting.time, "time", return_value=NOW):
            snapshot = await owner.reports.read("demo")
        assert snapshot["state"] == "failed"
        assert snapshot["attempt"]["diagnostics"] == diagnostic
        assert snapshot["last_success"] is None
    finally:
        await owner.async_close()


def test_candidate_and_error_details_are_bounded_and_secret_safe():
    raw = {
        "summary": "x" * 20000,
        "password": "private-password",
        "api_key": "private-api-key",
        "text": "Bearer private-token https://private.example/?token=x",
    }
    d = report_diagnostics.rejection(
        ValueError("Unknown evidence reference"), raw, {"evidence": []}
    )
    assert d["candidate_truncated"] and len(d["candidate_json"]) == 12000
    small = report_diagnostics.rejection(
        ValueError("Unknown evidence reference"),
        {k: v for k, v in raw.items() if k != "summary"},
        {"evidence": []},
    )
    assert all(
        v not in small["candidate_json"]
        for v in ("private-password", "private-api-key", "private-token", "private.example")
    )
    assert raw["password"] == "private-password", "diagnostics must not mutate the response"
    unknown = report_diagnostics.rejection(
        RuntimeError("private exception detail"), {}, {"evidence": []}
    )
    assert "private exception detail" not in unknown["detail"]


def test_old_schema_is_migrated_without_inventing_a_rejection_reason():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE reports(id TEXT PRIMARY KEY,policy TEXT,day TEXT,started REAL,finished REAL,status TEXT,ai_status TEXT,actor TEXT,facts TEXT,narrative TEXT,error TEXT)"
    )
    db.execute(
        "INSERT INTO reports VALUES('old','demo','2026-11-09',1,2,'failed','failed','native','{}',NULL,'invalid_ai_output')"
    )
    report_store.setup_reports(db, 3)
    report_store.setup_reports(db, 4)
    row = report_store.decode(db.execute("SELECT * FROM reports WHERE id='old'").fetchone())
    assert row["error"] == "invalid_ai_output" and row["diagnostics"] is None
    db.close()
