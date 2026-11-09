"""Recovery and loss semantics, restart persistence and honest daily facts."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.home_insights.journal import Journal
from custom_components.home_insights.models import Observation, daily_window

NOW = datetime(2026, 11, 9, 9, tzinfo=ZoneInfo("Europe/Budapest")).timestamp()
POLICY = {"version": 1, "revision": "native-observers.1", "monitored_entities": []}


def observation(status="active", adapter="notifications", fingerprint="first", source_id="test"):
    return Observation(
        adapter=adapter,
        source_id=source_id,
        status=status,
        fingerprint=fingerprint,
        title="Test",
        message="A source statement",
    )


def events(journal):
    return list(reversed(journal.history(limit=100)["events"]))


def test_persistence_recurrence_and_deduplication(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path, NOW, POLICY)
    journal.batch("notifications", [observation()], NOW)
    first = journal.snapshot()["open_episodes"][0]
    assert first["initial"] is True
    for n in range(1, 30):
        journal.batch("notifications", [observation()], NOW + n)
    assert sum(event["type"] == "first_observed" for event in events(journal)) == 1
    journal.close(NOW + 30)
    journal = Journal(path, NOW + 60, POLICY)
    journal.batch("notifications", [observation()], NOW + 60)
    assert journal.snapshot()["open_episodes"][0]["id"] == first["id"]
    journal.batch("notifications", [], NOW + 61)
    journal.batch("notifications", [observation()], NOW + 62)
    second = journal.snapshot()["open_episodes"][0]
    assert second["id"] != first["id"]
    assert second["issue_id"] == first["issue_id"]
    assert second["initial"] is False
    assert "source_cleared" not in [e["type"] for e in events(journal)]
    assert "notification_removed" in [e["type"] for e in events(journal)]
    journal.close(NOW + 63)


def test_lost_telemetry_does_not_resolve_or_restart_existing_fault(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite", NOW, POLICY)
    journal.batch("integrations", [observation(adapter="integrations")], NOW)
    first = journal.snapshot()["open_episodes"][0]["id"]
    journal.batch("integrations", [observation("unknown", "integrations", "not_loaded")], NOW + 1)
    assert journal.snapshot()["open_count"] == 1
    assert journal.snapshot()["status"] == "partial"
    journal.batch("integrations", [observation(adapter="integrations")], NOW + 2)
    assert journal.snapshot()["open_episodes"][0]["id"] == first
    journal.batch("integrations", [observation("clear", "integrations", "loaded")], NOW + 3)
    assert journal.snapshot()["open_count"] == 0
    assert [e["type"] for e in events(journal)].count("source_cleared") == 1
    journal.close(NOW + 4)


def test_failed_adapter_never_removes_active_notices(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite", NOW, POLICY)
    journal.batch("notifications", [observation()], NOW)
    first = journal.snapshot()["open_episodes"][0]["id"]
    journal.source_failure("notifications", "read_failed", NOW + 1)
    assert journal.snapshot()["open_count"] == 1
    journal.batch("notifications", [observation()], NOW + 2)
    assert journal.snapshot()["open_episodes"][0]["id"] == first
    journal.batch("notifications", [observation(fingerprint="new message")], NOW + 3)
    assert journal.snapshot()["open_count"] == 1
    assert journal.snapshot()["open_episodes"][0]["id"] != first
    assert "episode_superseded" in [e["type"] for e in events(journal)]
    journal.close(NOW + 4)


def test_daily_facts_coverage_gaps_and_dst(tmp_path):
    zone = "Europe/Budapest"
    for day, hours in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]:
        a, b = daily_window(day, zone)
        assert b - a == hours * 3600
    day = date(2026, 11, 9)
    start, end = daily_window(day, zone)
    journal = Journal(tmp_path / "journal.sqlite", start, POLICY)
    journal.checkpoint(start)
    journal.batch("notifications", [], start)
    journal.checkpoint(end)
    result = journal.daily_facts(day, zone)
    assert result["assessment"] == "no_anomaly_found"
    assert result["coverage_seconds"] == 86400
    assert journal.daily_facts(date(2026, 11, 8), zone)["assessment"] == "not_run"
    # A full source outage is different from a count of zero faults.
    journal.source_failure("repairs", "read_failed", start + 3600)
    journal.batch("repairs", [], start + 7200)
    assert journal.daily_facts(day, zone)["assessment"] == "insufficient_data"
    journal.gap("observer_lost", start + 3600)
    journal.checkpoint(start + 7200)
    journal.checkpoint(end)
    assert journal.daily_facts(day, zone)["coverage_seconds"] == 82800
    journal.close(end)


def test_ongoing_fault_is_counted_even_without_daily_new_events(tmp_path):
    start, end = daily_window(date(2026, 11, 9), "Europe/Budapest")
    journal = Journal(tmp_path / "journal.sqlite", start - 60, POLICY)
    journal.batch("repairs", [observation(adapter="repairs")], start - 60)
    journal.checkpoint(start - 60)
    journal.checkpoint(end)
    result = journal.daily_facts(date(2026, 11, 9), "Europe/Budapest")
    assert result["assessment"] == "attention_needed"
    assert result["episodes_present"] == 1
    assert result["counts"].get("source_appeared", 0) == 0
    journal.close(end)


def test_atomic_rejection_pagination_and_bounded_projection(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite", NOW, POLICY)
    for n in range(65):
        journal.batch("notifications", [observation(source_id=f"source{n}")], NOW + n)
    first = journal.history(limit=30)
    second = journal.history(before=first["next_before"], limit=30)
    assert first["has_more"] and second["has_more"]
    assert not {e["id"] for e in first["events"]} & {e["id"] for e in second["events"]}
    before = journal.snapshot()["event_count"]
    import pytest

    with pytest.raises(ValueError):
        journal.batch("notifications", [observation(), observation()], NOW + 100)
    assert journal.snapshot()["event_count"] == before
    assert json.dumps(journal.daily_facts(date(2026, 11, 9), "Europe/Budapest"))
    journal.close(NOW + 101)


def test_daily_evidence_does_not_leak_later_updates_or_drown_in_raw_states(tmp_path):
    start, end = daily_window(date(2026, 11, 9), "Europe/Budapest")
    journal = Journal(tmp_path / "journal.sqlite", start - 1, POLICY)
    journal.checkpoint(start)
    journal.batch("notifications", [observation()], start)
    for n in range(150):
        journal.entity("sensor.power", {"available": True, "state": str(n)}, start + n + 1)
    journal.batch("notifications", [observation(fingerprint="tomorrow")], end + 1)
    journal.checkpoint(end + 2)
    result = journal.daily_facts(date(2026, 11, 9), "Europe/Budapest")
    assert result["raw_state_event_count"] == 150
    assert "entity_state_changed" not in [event["type"] for event in result["events"]]
    assert "tomorrow" not in json.dumps(result)
    assert result["episodes"][0]["close_reason"] is None
    journal.close(end + 3)


def test_selected_source_must_have_a_successful_baseline(tmp_path):
    start, end = daily_window(date(2026, 11, 9), "Europe/Budapest")
    journal = Journal(
        tmp_path / "journal.sqlite", start, {**POLICY, "adapters": ["notifications", "repairs"]}
    )
    journal.batch("notifications", [], start)
    journal.checkpoint(start)
    journal.checkpoint(end)
    assert (
        journal.daily_facts(date(2026, 11, 9), "Europe/Budapest")["assessment"]
        == "insufficient_data"
    )
    journal.close(end)


def test_removed_unknown_integration_does_not_leave_an_eternal_gap(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite", NOW, POLICY)
    journal.batch("integrations", [observation("unknown", "integrations")], NOW)
    journal.checkpoint(NOW)
    assert journal.snapshot()["status"] == "partial"
    journal.batch("integrations", [], NOW + 1)
    assert journal.snapshot()["status"] == "recording"
    assert journal.snapshot()["open_count"] == 0
    journal.close(NOW + 2)


def test_ungraceful_restart_preserves_episode_and_exposes_missing_time(tmp_path):
    path = tmp_path / "journal.sqlite"
    start, end = daily_window(date(2026, 11, 9), "Europe/Budapest")
    journal = Journal(path, start, POLICY)
    journal.batch("notifications", [observation()], start)
    journal.checkpoint(start)
    episode = journal.snapshot()["open_episodes"][0]["id"]
    journal.checkpoint(start + 3600)
    journal.db.close()
    journal = Journal(path, start + 7200, POLICY)
    journal.batch("notifications", [observation()], start + 7200)
    journal.checkpoint(start + 7200)
    journal.checkpoint(end)
    result = journal.daily_facts(date(2026, 11, 9), "Europe/Budapest")
    assert result["assessment"] == "insufficient_data"
    assert result["unobserved_intervals"][0]["seconds"] == 3600
    assert journal.snapshot()["open_episodes"][0]["id"] == episode
    journal.close(end)
