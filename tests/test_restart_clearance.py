"""Cached startup values cannot manufacture repair and erase an existing snooze."""

from custom_components.home_insights.attention_journal import record_attention
from custom_components.home_insights.handling import project_handling, record_handling

from .test_daily_attention import POLICY, journal_at, record, request, row


def test_cached_clear_then_real_active_preserves_episode_and_snooze(tmp_path):
    journal = journal_at(tmp_path, 1000)
    original = record(journal, 1001, [row()])[0]
    record_handling(journal, request(original, "snooze", until=2000), "user", "User", 1002)
    journal.close(1003)
    journal = journal_at(tmp_path, 1100)
    try:
        pending = record_attention(
            journal, "home", POLICY, [row(status="inactive")], 1101, resumed=True
        )[0]
        assert pending["status"] == "loading" and pending["reason"] == "startup_clearance_pending"
        assert pending["episodeId"] == original["episodeId"]
        restored = record(journal, 1108, [row()])[0]
        projected = project_handling(journal.db, [restored], 1109)[0]
        assert projected["episodeId"] == original["episodeId"]
        assert projected["snoozedUntil"] == 2000
        assert (
            journal.db.execute(
                "SELECT closed FROM attention_episodes WHERE id=?", (original["episodeId"],)
            ).fetchone()[0]
            is None
        )
    finally:
        journal.close(1200)


def test_restored_value_is_uncertain_but_sustained_clearance_still_closes(tmp_path):
    journal = journal_at(tmp_path, 1000)
    original = record(journal, 1001, [row()])[0]
    journal.close(1002)
    journal = journal_at(tmp_path, 1100)
    try:
        restored = {**row(status="excluded"), "reason": "restored"}
        result = record_attention(journal, "home", POLICY, [restored], 1101, resumed=True)[0]
        assert result["status"] == "loading" and result["episodeId"] == original["episodeId"]
        result = record(journal, 1102, [row(status="inactive")])[0]
        assert result["status"] == "loading"
        result = record(journal, 1132, [row(status="inactive")])[0]
        assert result["status"] == "inactive" and "episodeId" not in result
        renewed = record(journal, 1133, [row()])[0]
        assert renewed["episodeId"] != original["episodeId"]
        assert project_handling(journal.db, [renewed], 1134)[0]["handling"]["revision"] == 0
    finally:
        journal.close(1200)
