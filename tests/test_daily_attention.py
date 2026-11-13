"""Canonical facts: calendar boundaries, gaps, recurrence and evidence as of midnight."""

import json
from datetime import date

import pytest

from custom_components.home_insights.attention_journal import record_attention
from custom_components.home_insights.daily_attention import daily_attention
from custom_components.home_insights.handling import record_handling
from custom_components.home_insights.journal import Journal
from custom_components.home_insights.models import daily_window

DAY = date(2026, 11, 9)
ZONE = "Europe/Budapest"
POLICY = {"version": 1, "revision": "test.1", "rules": []}


def row(identifier="fault", status="active", kind="incident", key=None, tone="warning"):
    return {
        "id": identifier,
        "key": key or identifier,
        "rule": identifier,
        "source": f"sensor.{identifier}",
        "name": identifier,
        "message": "Measured source",
        "scope": "home",
        "category": "health",
        "kind": kind,
        "status": status,
        "reason": "condition_met",
        "tone": tone,
        "dismissible": True,
    }


def record(journal, at, rows, complete=True, context=None, policy=POLICY, name="home"):
    return record_attention(
        journal, name, policy, rows, at, complete=complete, household_snapshot=context
    )


def journal_at(tmp_path, at):
    journal = Journal(tmp_path / "journal.sqlite", at, {"version": 1})
    journal.checkpoint(at)
    return journal


def request(row, operation="seen", request_id="first", **extra):
    return {
        "policy_id": "home",
        "operation": operation,
        "request_id": request_id,
        "targets": [{"row_id": row["id"], "episode_id": row["episodeId"], "expected_revision": 0}],
        **extra,
    }


@pytest.mark.parametrize(
    "day,hours", [(DAY, 24), (date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_complete_calendar_day_counts_updates_separately(tmp_path, day, hours):
    start, end = daily_window(day, ZONE)
    journal = journal_at(tmp_path, start)
    record(
        journal, start, [row("update", kind="update"), row("info", status="event", kind="event")]
    )
    journal.checkpoint(end + 1)
    result = daily_attention(journal, "home", day, ZONE)
    assert result["assessment"] == "no_anomaly_found"
    assert result["coverage_seconds"] == hours * 3600
    assert result["counts"]["issues"].get("episodes_present", 0) == 0
    assert result["counts"]["updates"]["episodes_present"] == 1
    assert result["counts"]["events"]["episodes_present"] == 1
    journal.close(end + 1)


def test_new_policy_partial_day_and_source_gaps_never_claim_clean(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    assert daily_attention(journal, "home", DAY, ZONE)["assessment"] == "not_run"
    record(journal, start + 3600, [])
    journal.checkpoint(end + 1)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "insufficient_data"
    assert result["coverage_seconds"] == 23 * 3600
    record(journal, start + 7200, [], complete=False)
    record(journal, start + 10800, [])
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["observed_seconds"] == 23 * 3600
    assert result["coverage_seconds"] == 22 * 3600
    assert daily_attention(journal, "home", date(2026, 11, 8), ZONE)["assessment"] == "not_run"
    journal.close(end + 1)


def test_observer_gap_reduces_duration_and_does_not_clear_episode(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    first = record(journal, start, [row()])[0]
    journal.gap("storage_failed", start + 3600)
    journal.checkpoint(start + 7200)
    current = record(journal, start + 7200, [row()], complete=True)[0]
    assert first["episodeId"] == current["episodeId"]
    journal.checkpoint(end + 1)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "attention_needed"
    assert result["data_quality"] == "insufficient_data"
    assert result["episodes"][0]["confirmed_active_seconds"] == 23 * 3600
    assert result["episodes"][0]["episode_span_seconds"] == 24 * 3600
    assert result["episodes"][0]["close_reason"] is None
    journal.close(end + 1)


def test_carry_recurrence_channel_removal_and_no_future_leak(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start - 60)
    record(journal, start - 60, [row("alert", kind="notification")])
    # Native evidence is not a second issue in this canonical scope.
    from .test_journal import observation

    journal.batch("notifications", [observation()], start)
    record(journal, start + 30, [])
    current = record(journal, start + 60, [row("alert", kind="notification")])[0]
    record_handling(journal, request(current), "actor", "User", start + 65)
    record_handling(
        journal,
        request(
            current,
            "claimed_fixed",
            "future",
            note="TOMORROW",
            targets=[
                {
                    "row_id": current["id"],
                    "episode_id": current["episodeId"],
                    "expected_revision": 1,
                }
            ],
        ),
        "actor",
        "User",
        end + 1,
    )
    record(journal, end + 2, [])
    journal.checkpoint(end + 3)
    result = daily_attention(journal, "home", DAY, ZONE)
    counts = result["counts"]["issues"]
    assert counts["sources_present"] == 1
    assert counts["episodes_present"] == 2
    assert counts["carried_over"] == 1 and counts["recurrences"] == 1
    assert counts["notification_removed"] == 1
    assert counts["open_at_period_end"] == 1
    assert result["handling_counts"] == {"seen": 1}
    assert "TOMORROW" not in json.dumps(result)
    ongoing = next(e for e in result["episodes"] if not e["present_at_period_start"])
    assert ongoing["closed_at"] is None
    assert ongoing["handling_at_period_end"]["revision"] == 1
    assert result["assessment"] == "attention_needed"
    journal.close(end + 3)


def test_pending_noise_not_an_episode_and_context_is_historical(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    context = {
        "schema_version": 1,
        "revision": 1,
        "rules": [
            {"id": "approved", "status": "approved", "enabled": True},
            {"id": "draft", "status": "draft", "enabled": True},
        ],
    }
    for n in range(200):
        record(
            journal,
            start + n,
            [row("telemetry", status="pending" if n % 2 else "inactive")],
            context=context,
        )
    record(journal, end + 1, [], context={**context, "revision": 2, "rules": [{"title": "FUTURE"}]})
    journal.checkpoint(end + 2)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["episodes"] == [] and result["assessment"] == "no_anomaly_found"
    assert result["household_context"]["snapshot"]["revision"] == 1
    assert len(result["household_context"]["snapshot"]["rules"]) == 1
    assert "FUTURE" not in json.dumps(result)
    assert journal.db.execute("SELECT COUNT(*) FROM attention_context").fetchone()[0] == 2
    journal.close(end + 2)


def test_policy_revision_reversion_and_two_ids_share_definition(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    record(journal, start, [])
    record(journal, start + 1, [], name="second")
    record(journal, start + 2, [], policy={**POLICY, "revision": "test.2"})
    record(journal, start + 3, [])
    journal.checkpoint(end + 1)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert [r["revision"] for r in result["policy_revisions"]] == ["test.1", "test.2", "test.1"]
    assert daily_attention(journal, "second", DAY, ZONE)["assessment"] == "insufficient_data"
    journal.close(end + 1)


def test_incident_evidence_precedes_update_flood_and_truncation_is_explicit(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    record(
        journal,
        start,
        [row("critical", tone="error"), *[row(f"up{n}", kind="update") for n in range(105)]],
    )
    journal.checkpoint(end + 1)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["episodes_truncated"] and len(result["episodes"]) == 100
    assert result["episodes"][0]["name"] == "critical"
    assert result["counts"]["updates"]["episodes_present"] == 105
    assert result["counts"]["issues"]["episodes_present"] == 1
    journal.close(end + 1)


def test_missing_source_details_do_not_become_faults_or_leak_tomorrow(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    record(journal, start, [row("battery", status="unavailable")], complete=False)
    record(journal, start + 60, [row("battery", status="inactive")])
    record(journal, end + 1, [row("future", status="unavailable")], complete=False)
    journal.checkpoint(end + 2)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "insufficient_data"
    assert result["counts"]["issues"].get("episodes_present", 0) == 0
    assert result["availability"]["sources_with_gaps"] == 1
    assert result["availability"]["details"][0]["source"] == "sensor.battery"
    assert result["availability"]["details"][0]["observed_seconds"] == 60
    assert result["availability"]["whole_day_details"]
    assert "sensor.future" not in json.dumps(result)
    journal.close(end + 3)
