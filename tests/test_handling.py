"""Metadata audit, atomic group edits, idempotency and episode-scoped state."""

import pytest

from custom_components.home_insights.handling import (
    episode_detail,
    project_handling,
    record_handling,
)
from custom_components.home_insights.models import daily_window

from .test_daily_attention import DAY, ZONE, journal_at, record, request, row


def test_seen_claimed_fixed_and_snooze_never_clear_source(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    current = record(journal, start, [row()])[0]
    for revision, operation in enumerate(("seen", "claimed_fixed", "snooze", "unsnooze")):
        value = request(
            current,
            operation,
            str(revision),
            targets=[
                {
                    "row_id": current["id"],
                    "episode_id": current["episodeId"],
                    "expected_revision": revision,
                }
            ],
            **({"until": start + 3600} if operation == "snooze" else {}),
        )
        result = record_handling(journal, value, "actual-user", "Actual User", start + revision + 1)
        assert result["ok"]
        projected = project_handling(journal.db, [current], start + 100)[0]
        assert projected["status"] == "active"
        assert projected["episodeId"] == current["episodeId"]
        assert ("snoozedUntil" in projected) == (operation == "snooze")
    detail = episode_detail(journal.db, "home", current["episodeId"], limit=2)
    assert detail["closed_at"] is None and detail["handling"]["claimed_fixed_at"] == start + 2
    assert detail["has_more"] and len(detail["events"]) == 2
    assert detail["events"][0]["actor_name"] == "Actual User"
    assert "body" not in detail["events"][0]
    journal.close(end)


def test_idempotent_retry_survives_expiry_and_group_failure_is_atomic(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    first, second = record(journal, start, [row("first"), row("second")])
    value = request(first, "snooze", until=start + 60)
    result = record_handling(journal, value, "actor", "User", start + 1)
    assert record_handling(journal, value, "actor", "User", start + 100) == result
    assert journal.db.execute("SELECT COUNT(*) FROM attention_handling").fetchone()[0] == 1
    with pytest.raises(ValueError, match="different operation"):
        record_handling(journal, {**value, "note": "different"}, "actor", "User", start + 2)
    group = request(
        first,
        "seen",
        "group",
        targets=[request(second)["targets"][0], request(first)["targets"][0]],
    )
    with pytest.raises(ValueError, match="revision changed"):
        record_handling(journal, group, "actor", "User", start + 2)
    assert journal.db.execute("SELECT COUNT(*) FROM attention_handling").fetchone()[0] == 1
    record(journal, start + 101, [row("first", status="inactive"), row("second")])
    renewed = record(journal, start + 102, [row("first"), row("second")])[0]
    assert renewed["episodeId"] != first["episodeId"]
    assert project_handling(journal.db, [renewed], start + 103)[0]["handling"]["revision"] == 0
    with pytest.raises(ValueError, match="Episode changed"):
        record_handling(journal, request(first, request_id="stale"), "actor", "User", start + 103)
    journal.close(end)


def test_critical_cannot_be_hidden_and_deadlines_notes_validated(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    current = record(journal, start, [row(tone="error")])[0]
    with pytest.raises(ValueError, match="cannot be snoozed"):
        record_handling(
            journal, request(current, "snooze", until=start + 60), "actor", "User", start + 1
        )
    for extra in ({"until": float("nan")}, {"until": start}, {"until": end + 30 * 86400}):
        with pytest.raises(ValueError):
            record_handling(
                journal, request(current, "snooze", **extra), "actor", "User", start + 1
            )
    with pytest.raises(ValueError):
        record_handling(
            journal, request(current, "note", note="x" * 1001), "actor", "User", start + 1
        )
    with pytest.raises(ValueError):
        record_handling(
            journal, {**request(current), "actor": "spoofed"}, "actor", "User", start + 1
        )
    assert record_handling(journal, request(current), "actor", "User", start + 1)["ok"]
    journal.close(end)


def test_metadata_and_history_persist_after_reopening(tmp_path):
    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    current = record(journal, start, [row()])[0]
    value = request(current, "claimed_fixed", note="Checked the cable")
    response = record_handling(journal, value, "actor", "User", start + 1)
    journal.close(start + 2)
    journal = journal_at(tmp_path, start + 3)
    assert record_handling(journal, value, "actor", "User", start + 4) == response
    detail = episode_detail(journal.db, "home", current["episodeId"])
    assert detail["handling"]["claimed_fixed_at"] == start + 1
    assert detail["events"][0]["note"] == "Checked the cable"
    journal.close(end)
