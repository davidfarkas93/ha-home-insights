"""Resolved/removed alerts survive; overnight events cannot rewrite yesterday."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.core import State

from custom_components.home_insights import nas, nas_events, nas_reporting
from custom_components.home_insights.models import daily_window

from .test_nas import DAY, PROFILE, fixture

SOURCE = "event.nas_notifications"


def notification(at, subject, identity="notice", observed=None, importance="INFO"):
    return State(
        SOURCE,
        datetime.fromtimestamp(at, UTC).isoformat(),
        attributes={
            "event_type": "notification_created",
            "notification_id": identity,
            "timestamp": datetime.fromtimestamp(at, UTC).isoformat(),
            "importance": importance,
            "subject": subject,
        },
        last_updated=datetime.fromtimestamp(observed or at, UTC),
    )


def test_overnight_error_recovery_and_sync_are_separate_from_previous_day():
    start, end = daily_window(DAY, "Europe/Budapest")
    h, runs = fixture()
    h[SOURCE] = [
        notification(
            end + 3600, "Parity disk in error state (disk dsbl)", "error", importance="ALERT"
        ),
        notification(end + 36000, "array turned good", "recovered"),
        notification(end + 40000, "Parity-Sync started", "sync"),
    ]
    p = nas.validate_profile({**PROFILE, "unraid_notifications": [SOURCE]})
    with patch.object(nas, "read_recorder", return_value=(h, runs)):
        f = nas.collect_facts(None, p, "demo", DAY, "Europe/Budapest", start, end, end + 43200)
    assert f["assessment"] == "no_anomaly_found"
    assert not f["notifications"]["events"]
    follow = f["since_midnight"]
    assert follow["attention"] and [e["code"] for e in follow["events"]] == [
        "parity_disk_error",
        "array_recovered",
        "parity_sync",
    ]
    bundle = nas_reporting.evidence_bundle(f)
    response = {
        "summary": bundle["summary_options"][0]["text"],
        "summary_since_midnight": bundle["since_midnight"]["summary"],
    }
    saved = nas_reporting.validate_narrative(response, bundle)
    assert "hibajelzése" not in saved["summary"]
    assert (
        "hibajelzése" in saved["summary_since_midnight"]
        and "még nem igazolt" in saved["summary_since_midnight"]
    )
    with pytest.raises(ValueError):
        nas_reporting.validate_narrative(
            {**response, "summary": response["summary_since_midnight"]}, bundle
        )


def test_daily_error_is_retained_after_recovery_and_cannot_be_overlooked_by_ai():
    start, end = daily_window(DAY, "Europe/Budapest")
    h, runs = fixture()
    h[SOURCE] = [
        notification(
            start + 3600, "Parity disk in error state (disk dsbl)", "error", importance="ALERT"
        ),
        notification(start + 7200, "array turned good", "recovered"),
        notification(start + 8000, "Parity-Sync started", "sync"),
    ]
    p = nas.validate_profile({**PROFILE, "unraid_notifications": [SOURCE]})
    with patch.object(nas, "read_recorder", return_value=(h, runs)):
        f = nas.collect_facts(None, p, "demo", DAY, "Europe/Budapest", start, end, end)
    assert f["assessment"] == "attention_needed"
    assert "since_midnight" not in f
    bundle = nas_reporting.evidence_bundle(f)
    assert all("notifications" in o["refs"] for o in bundle["summary_options"])
    assert all("hibajelzése" in o["text"] for o in bundle["summary_options"])


def test_notification_replays_use_creation_time_and_keep_one_original_event():
    start, end = daily_window(DAY, "Europe/Budapest")
    row = notification(start + 3600, "Parity disk in error state (disk dsbl)", importance="ALERT")
    replay = notification(start + 3600, row.attributes["subject"], observed=end + 100)
    h = {SOURCE: [row, replay]}
    timeline = nas_events.collect_events(h, [SOURCE], start, end)
    assert len(timeline["events"]) == 1
    assert not nas_events.collect_events(h, [SOURCE], end, end + 3600)["events"]
    invalid = notification(start + 200, "Warning", "invalid")
    invalid.attributes = {**invalid.attributes, "timestamp": "not-a-date"}
    assert (
        nas_events.collect_events({SOURCE: [invalid]}, [SOURCE], start, end)["invalid_events"] == 1
    )


def test_generic_alert_is_not_clean_and_sync_is_not_called_check():
    start, end = daily_window(DAY, "Europe/Budapest")
    events = nas_events.collect_events(
        {SOURCE: [notification(start + 100, "Backup Error!", importance="ALERT")]},
        [SOURCE],
        start,
        end,
    )
    assert events["attention"]
    assert "figyelmet kérő" in nas_events.chronology_statement(events)
    sync = nas_events.collect_events(
        {SOURCE: [notification(start + 100, "Parity-Sync started")]}, [SOURCE], start, end
    )
    assert not sync["attention"] and "különbözik" in nas_events.chronology_statement(sync)


def test_completion_zero_errors_and_backup_recurrence_survive_projection():
    start, end = daily_window(DAY, "Europe/Budapest")
    rows = [
        notification(start + 100, "Parity disk in error state", "error", importance="ALERT"),
        notification(start + 200, "Parity-Sync started", "start"),
        notification(start + 300, "Parity-Sync finished (0 errors)", "finish"),
    ]
    timeline = nas_events.collect_events({SOURCE: rows}, [SOURCE], start, end)
    assert timeline["events"][-1]["code"] == "parity_sync_completed"
    assert timeline["events"][-1]["reported_errors"] == 0
    assert "nulla jelzett hibával befejeződött" in nas_events.chronology_statement(timeline)
    assert "helyreállt" not in nas_events.chronology_statement(timeline)
    rows[-1] = notification(start + 300, "Parity-Sync finished (4 errors)", "finish")
    bad = nas_events.collect_events({SOURCE: rows}, [SOURCE], start, end)
    assert bad["events"][-1]["attention"]
    assert "nem nulla" in nas_events.chronology_statement(bad)
    backup = nas_events.collect_events(
        {SOURCE: [notification(start + 100, "[AppdataBackup] Error!", importance="ALERT")]},
        [SOURCE],
        start,
        end,
    )
    assert "Appdata Backup" in nas_events.chronology_statement(backup)
    assert "ismét" in nas_events.chronology_statement(backup, backup)


def test_completion_from_another_nas_does_not_close_this_disk_alert():
    start, end = daily_window(DAY, "Europe/Budapest")
    timeline = nas_events.collect_events(
        {
            SOURCE: [
                notification(start + 100, "Parity disk in error state", importance="ALERT"),
                notification(start + 200, "Parity-Sync started", "start"),
            ]
        },
        [SOURCE],
        start,
        end,
    )
    timeline["events"].append(
        {
            **timeline["events"][-1],
            "source": "event.other",
            "code": "parity_sync_completed",
            "at": timeline["end"],
            "reported_errors": 0,
        }
    )
    assert "még nem igazolt" in nas_events.chronology_statement(timeline)
