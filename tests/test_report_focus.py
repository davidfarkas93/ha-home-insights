"""Group before applying the budget; a resolved transient cannot bury a lasting alert."""

from custom_components.home_insights.report_focus import group_sources, headline


def test_repeated_brief_sources_do_not_fill_all_evidence_slots():
    transient = {
        "source": "sensor.ha",
        "name": "HA",
        "category": "issues",
        "tone": "error",
        "initial": True,
        "confirmed_active_seconds": 1,
        "closed_at": "2026-11-11T20:00:00Z",
        "close_reason": "condition_cleared",
    }
    episodes = [{**transient, "id": str(i)} for i in range(20)]
    episodes.append(
        {
            **transient,
            "id": "disk",
            "source": "sensor.disk",
            "name": "Disk",
            "confirmed_active_seconds": 5000,
        }
    )
    groups = group_sources(episodes)
    assert len(groups) == 2 and groups[0]["source"] == "sensor.disk"
    assert groups[1]["group"]["observations"] == 20
    assert "rendeződött" in headline(groups)
    assert "5000" not in headline(groups), "grouping cannot invent a downtime sum"


def test_snooze_is_not_source_repair_and_updates_do_not_displace_issues():
    rows = [
        {
            "source": "sensor.issue",
            "name": "Issue",
            "category": "issues",
            "tone": "warning",
            "closed_at": None,
            "handling_at_period_end": {"snooze_override": True},
        },
        {"source": "update.x", "category": "updates", "tone": "error", "closed_at": None},
    ]
    groups = group_sources(rows)
    assert groups[0]["source"] == "sensor.issue"
    assert "fennállt" in headline(groups) and "szüneteltetted" in headline(groups)
