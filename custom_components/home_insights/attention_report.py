"""Historical report context. Handling never changes the measured source truth."""

import json
from collections import defaultdict
from datetime import datetime

from .models import digest, iso


def enrich_context(db, policy, episodes, start, end):
    """Use only evidence before the day's boundary, never today's mutable tables."""
    deliveries = defaultdict(list)
    for event in db.execute(
        "SELECT observed,type,body FROM events WHERE observed<? AND type IN "
        "('notification_user_dismissed','notification_dismissal_remembered',"
        "'notification_redelivery_suppressed','notification_acknowledgement_reset') "
        "ORDER BY observed,seq",
        (end,),
    ):
        body = json.loads(event["body"])
        deliveries["notification:" + body["notification_id"]].append(
            {"at": event["observed"], "operation": event["type"], **body}
        )
    for item in episodes:
        opened = datetime.fromisoformat(item["first_observed_at"]).timestamp()
        prior = db.execute(
            "SELECT COUNT(*) AS observations,MIN(opened) AS first_observed "
            "FROM attention_episodes WHERE policy=? AND row_id=? AND opened<?",
            (policy, item["row_id"], min(start, opened)),
        ).fetchone()
        item["history"] = {
            "previously_observed": opened < start or bool(prior["observations"]),
            "first_observed_at": iso(prior["first_observed"])
            if prior["first_observed"] is not None
            else item["first_observed_at"],
            "actual_fault_start_known": False,
        }
        if item["kind"] != "notification":
            continue
        closed = datetime.fromisoformat(item["closed_at"]).timestamp() if item["closed_at"] else end
        fingerprint = digest([item["name"], item["message"]])
        matching = []
        for event in deliveries[item["source"]]:
            # Confirmed removal can reach the worker just after the source callback.
            # A later unrelated dismissal cannot acknowledge an earlier occurrence.
            if not opened <= event["at"] <= closed + 5:
                continue
            if event.get("content_hash") and event["content_hash"] != fingerprint:
                continue
            if event["operation"] == "notification_acknowledgement_reset":
                continue
            matching.append(event)
        if matching:
            latest = matching[-1]
            item["delivery_handling"] = {
                "status": "redelivery_suppressed"
                if latest["operation"] == "notification_redelivery_suppressed"
                else "dismissed_by_user",
                "at": iso(latest["at"]),
                "source_repair_confirmed": False,
            }


def observation_scope(window, observed, covered, quality):
    return {
        "observer_unobserved_seconds": round(window - observed, 3),
        "source_incomplete_seconds": round(observed - covered, 3),
        "complete_day_verdict_possible": quality == "complete",
        "findings_are_observed_only": True,
        "automation_trace_audit": "not_collected",
    }


def overview(groups):
    """Source groups, not episode/delivery counts or physical failure counts."""
    issues = [g for g in groups if g.get("category") == "issues"]
    return {
        "sources_requiring_review": sum(g["group"]["requires_check"] for g in issues),
        "sources_observed_cleared": sum(g["group"]["latest_observed_clearance"] for g in issues),
        "sources_snoozed_at_end": sum(g["group"]["snoozed_at_end"] for g in issues),
        "notification_sources_handled": sum(
            g["group"]["delivery_status"] in ("dismissed_by_user", "redelivery_suppressed")
            for g in issues
        ),
        "previously_observed_sources": sum(g["group"]["previously_observed"] for g in issues),
    }


def observation_note(facts):
    if facts["data_quality"] == "complete":
        return ""
    scope = facts.get("observation_scope", {})
    missing = []
    if scope.get("observer_unobserved_seconds", 0) > 0:
        missing.append("a jelzésfigyelő egyes időszakokban nem rögzített")
    if scope.get("source_incomplete_seconds", 0) > 0:
        missing.append("egyes források állapota átmenetileg nem volt ellenőrizhető")
    reason = "; ".join(missing) or "a megfigyelés nem fedi le a teljes napot"
    return (
        reason[0].upper() + reason[1:] + ". A rögzített események értékelhetők, "
        "de a kimaradt szakaszokról és a teljes nap hibamentességéről nincs elég adat."
    )
