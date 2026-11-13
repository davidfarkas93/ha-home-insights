"""Real canonical history, delivery intent and scoped daily conclusions."""

from custom_components.home_insights import notification_memory, reporting
from custom_components.home_insights.daily_attention import daily_attention
from custom_components.home_insights.models import Observation, digest
from custom_components.home_insights.report_focus import group_sources, headline

from .test_daily_attention import DAY, ZONE, journal_at, record, row
from .test_reports import TEXT, facts


def test_historical_dismissal_is_delivery_metadata_not_repair_or_future_knowledge(tmp_path):
    from custom_components.home_insights.models import daily_window

    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    notice = {**row("notice", kind="notification"), "source": "notification:notice"}
    record(journal, start, [notice])
    observation = Observation(
        adapter="notifications",
        source_id="notice",
        status="active",
        fingerprint="test",
        title=notice["name"],
        message=notice["message"],
    )
    notification_memory.acknowledge(journal, [(observation, None)], "user", start + 10)
    record(journal, start + 11, [])
    record(journal, end + 1, [notice])
    notification_memory.acknowledge(journal, [(observation, None)], "user", end + 2)
    journal.checkpoint(end + 3)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "no_action_needed"
    assert result["report_overview"]["notification_sources_handled"] == 1
    evidence = reporting.evidence_bundle(result)
    assert "elvetetted" in evidence["headline"]
    assert evidence["permitted_checks"] == []
    assert result["episodes"][0]["close_reason"] == "notification_removed"
    assert result["episodes"][0]["delivery_handling"]["source_repair_confirmed"] is False
    assert "actor" not in evidence["evidence"][0]["delivery_handling"]
    assert (
        journal.db.execute("SELECT COUNT(*) FROM notification_acknowledgements").fetchone()[0] == 0
    )
    journal.close(end + 3)


def test_future_or_different_content_dismissal_cannot_handle_current_notice(tmp_path):
    from custom_components.home_insights.models import daily_window

    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    notice = {**row("notice", kind="notification"), "source": "notification:notice"}
    record(journal, start, [notice])
    for at, fingerprint in [
        (start + 10, digest(["different", "content"])),
        (end + 1, digest([notice["name"], notice["message"]])),
    ]:
        journal._event(
            "notification_user_dismissed",
            at,
            {"notification_id": "notice", "content_hash": fingerprint},
        )
    journal.checkpoint(end + 2)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "attention_needed"
    assert "delivery_handling" not in result["episodes"][0]
    journal.close(end + 2)


def test_short_unrelated_source_gap_preserves_observed_finding_but_not_complete_quality(tmp_path):
    from custom_components.home_insights.models import daily_window

    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    record(journal, start, [row("fault"), row("telemetry", status="inactive")])
    record(
        journal, start + 100, [row("fault"), row("telemetry", status="unavailable")], complete=False
    )
    record(journal, start + 110, [row("fault"), row("telemetry", status="inactive")])
    journal.checkpoint(end + 1)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "attention_needed"
    assert result["data_quality"] == "insufficient_data"
    assert result["observation_scope"]["observer_unobserved_seconds"] == 0
    assert result["observation_scope"]["source_incomplete_seconds"] == 10
    assert not result["observation_scope"]["complete_day_verdict_possible"]
    bundle = reporting.evidence_bundle(result)
    assert "források" in bundle["observation_note"]
    assert "nem rögzített" not in bundle["observation_note"]
    journal.close(end + 1)


def test_legacy_acknowledgement_is_read_from_history_not_mutable_memory(tmp_path):
    from custom_components.home_insights.models import daily_window

    start, end = daily_window(DAY, ZONE)
    journal = journal_at(tmp_path, start)
    notice = {**row("robot", kind="notification"), "source": "notification:robot"}
    record(journal, start, [notice])
    journal._event("notification_dismissal_remembered", start + 20, {"notification_id": "robot"})
    record(journal, start + 21, [])
    record(journal, start + 30, [notice])
    journal._event("notification_redelivery_suppressed", start + 31, {"notification_id": "robot"})
    record(journal, start + 32, [])
    journal._event("notification_acknowledgement_reset", end + 1, {"notification_id": "robot"})
    journal.checkpoint(end + 2)
    result = daily_attention(journal, "home", DAY, ZONE)
    assert result["assessment"] == "no_action_needed"
    assert "újraküldését" in reporting.evidence_bundle(result)["headline"]
    journal.close(end + 2)


def test_snooze_expiry_unsnooze_and_critical_signal_cannot_be_hidden():
    base = {
        "source": "sensor.fault",
        "name": "Fault",
        "kind": "incident",
        "category": "issues",
        "tone": "warning",
        "period_end": 100,
        "closed_at": None,
    }
    for handling in [
        {"snooze_override": True, "snoozed_until": 99},
        {"snooze_override": True, "snoozed_until": None},
    ]:
        group = group_sources([{**base, "handling_at_period_end": handling}])[0]["group"]
        assert not group["snoozed_at_end"] and group["requires_check"]
    item = {**base, "handling_at_period_end": {"snooze_override": True, "snoozed_until": 101}}
    assert not group_sources([item])[0]["group"]["requires_check"]
    assert group_sources([{**item, "tone": "error"}])[0]["group"]["requires_check"]


def test_unobserved_end_and_claimed_fix_cannot_be_reported_as_clearance():
    base = {
        "source": "sensor.fault",
        "name": "Fault",
        "kind": "incident",
        "category": "issues",
        "closed_at": None,
    }
    assert "nem volt ellenőrizhető" in headline(
        group_sources([{**base, "evidence_status": "unavailable"}])
    )
    assert "javítottnak jelölted" in headline(
        group_sources([{**base, "handling_at_period_end": {"claimed_fixed_at": 10}}])
    )


def test_delivery_removal_and_retired_repair_are_not_unresolved_physical_faults():
    for kind, reason in [
        ("notification", "notification_removed"),
        ("repair", "no_longer_monitored"),
    ]:
        groups = group_sources(
            [
                {
                    "source": "source",
                    "name": "Notice",
                    "kind": kind,
                    "category": "issues",
                    "closed_at": "2026-11-09T20:00:00Z",
                    "close_reason": reason,
                }
            ]
        )
        assert not groups[0]["group"]["requires_check"]
        assert "rendeződése nem igazolt" not in headline(groups)


def test_partial_observation_has_a_detail_note_without_hiding_a_known_finding():
    data = facts("attention_needed")
    data["data_quality"] = "insufficient_data"
    data["observation_scope"] = {"observer_unobserved_seconds": 60, "source_incomplete_seconds": 0}
    bundle = reporting.evidence_bundle(data)
    result = reporting.validate_narrative({**TEXT, "suggested_check": ""}, bundle)
    assert not result["summary"].startswith("Az előző nap csak részben")
    assert "nem rögzített" in result["observation_note"]
    assert result["suggested_check"]


def test_source_selection_cannot_drop_the_leading_finding_or_select_a_gap_as_a_fault():
    import pytest

    data = facts()
    data["episodes"].append(
        {
            **data["episodes"][0],
            "id": "retired",
            "source": "notification:old",
            "kind": "notification",
            "name": "Old notice",
            "closed_at": "2026-11-09T19:00:00Z",
            "close_reason": "notification_removed",
        }
    )
    data["availability"]["details"] = [
        {"name": "Missing", "source": "sensor.missing", "status": "unavailable"}
    ]
    bundle = reporting.evidence_bundle(data)
    assert all("episode_1" in choice.split(",") for choice in reporting.source_selections(bundle))
    assert not any("gap_" in choice for choice in reporting.source_selections(bundle))
    with pytest.raises(ValueError, match="leading observed finding"):
        reporting.validate_narrative(
            {**TEXT, "evidence_ids": "episode_2", "suggested_check": ""}, bundle
        )
