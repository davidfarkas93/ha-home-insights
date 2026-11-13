"""Deterministic completed-day policy facts, without native-source double counts."""

import json
from collections import Counter

from . import attention_report, report_focus
from .handling import handling_state
from .models import daily_window, iso

ISSUES = {"incident", "notification", "repair", "integration"}


def merge(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result


def subtract(intervals, gaps):
    for left, right in gaps:
        kept = []
        for a, b in intervals:
            if right <= a or left >= b:
                kept.append((a, b))
                continue
            if a < left:
                kept.append((a, left))
            if right < b:
                kept.append((right, b))
        intervals = kept
    return intervals


def measure(intervals):
    return round(sum(b - a for a, b in intervals), 3)


def daily_attention(journal, policy, day, zone):
    db = journal.db
    start, end = daily_window(day, zone)
    versions = db.execute(
        "SELECT observed,policy_hash,body FROM events WHERE type='attention_policy_observed' "
        "AND json_extract(body,'$.policy_id')=? AND observed<? ORDER BY observed,seq",
        (policy, end),
    ).fetchall()
    first = versions[0]["observed"] if versions else end
    spans = merge(
        [
            (max(start, first, r["started"]), min(end, r["last_seen"]))
            for r in db.execute(
                "SELECT started,last_seen FROM runs WHERE started<? AND last_seen>?", (end, start)
            )
        ]
    )
    gaps = [
        dict(r)
        for r in db.execute(
            "SELECT source,started,ended,reason FROM coverage WHERE source IN (?,?) "
            "AND started<? AND (ended IS NULL OR ended>?) ORDER BY started",
            ("observer", f"attention:{policy}", end, max(start, first)),
        )
    ]
    observed = subtract(
        spans, [(g["started"], g["ended"] or end) for g in gaps if g["source"] == "observer"]
    )
    covered = subtract(observed, [(g["started"], g["ended"] or end) for g in gaps])
    observed_seconds, covered_seconds = measure(observed), measure(covered)
    quality = (
        "not_run"
        if not observed
        else "complete"
        if covered == [(start, end)]
        else "insufficient_data"
    )
    unobserved = subtract([(start, end)], covered)
    episodes = db.execute(
        "SELECT * FROM attention_episodes WHERE policy=? AND opened<? "
        "AND (closed IS NULL OR closed>?) ORDER BY opened,id",
        (policy, end, start),
    ).fetchall()
    counts = {name: Counter() for name in ("issues", "updates", "events")}
    sources = {name: set() for name in counts}
    enriched = []
    for episode in episodes:
        row = json.loads(episode["body"])
        category = (
            "issues"
            if row["kind"] in ISSUES
            else ("updates" if row["kind"] == "update" else "events")
        )
        count = counts[category]
        count["episodes_present"] += 1
        sources[category].add(row["source"])
        carried = episode["opened"] < start
        count["carried_over"] += carried
        if not carried:
            count["first_observed" if episode["initial"] else "newly_detected"] += 1
            if (
                not episode["initial"]
                and db.execute(
                    "SELECT 1 FROM attention_episodes WHERE policy=? AND row_id=? AND opened<? "
                    "AND closed IS NOT NULL LIMIT 1",
                    (policy, episode["row_id"], episode["opened"]),
                ).fetchone()
            ):
                count["recurrences"] += 1
        closed = (
            episode["closed"] if episode["closed"] is not None and episode["closed"] < end else None
        )
        if closed is not None:
            count[episode["close_reason"]] += 1
        else:
            count["open_at_period_end"] += 1
        enriched.append((episode, row, category, closed))
    for name in counts:
        counts[name] = {**counts[name], "sources_present": len(sources[name])}
    # High-priority incidents precede updates/events, with ongoing issues first.
    enriched.sort(
        key=lambda item: (
            item[2] != "issues",
            item[1]["tone"] != "error",
            item[3] is not None,
            -item[0]["opened"],
        )
    )
    details = []
    for episode, initial_row, category, closed in enriched[:100]:
        evidence = db.execute(
            "SELECT observed,type,body FROM events WHERE episode=? AND observed<? "
            "AND type!='attention_handling_recorded' ORDER BY observed DESC,seq DESC LIMIT 1",
            (episode["id"], end),
        ).fetchone()
        body = json.loads(evidence["body"]) if evidence else {}
        row = body.get("row", initial_row)
        a, b = max(start, episode["opened"]), min(end, closed if closed is not None else end)
        # Entire-policy coverage is deliberately conservative; elapsed episode
        # span is not a measured physical failure duration.
        confirmed = measure([(max(a, x), min(b, y)) for x, y in covered if x < b and y > a])
        details.append(
            {
                "id": episode["id"],
                "row_id": episode["row_id"],
                "category": category,
                "source": initial_row["source"],
                "kind": initial_row["kind"],
                "name": row.get("name", initial_row["name"]),
                "message": row.get("message", ""),
                "tone": row.get("tone", initial_row["tone"]),
                "scope": initial_row["scope"],
                "first_observed_at": iso(episode["opened"]),
                "initial": bool(episode["initial"]),
                "present_at_period_start": episode["opened"] < start,
                "closed_at": iso(closed) if closed is not None else None,
                "close_reason": episode["close_reason"] if closed is not None else None,
                "episode_span_seconds": round(b - a, 3),
                "confirmed_active_seconds": confirmed,
                "evidence_at": iso(evidence["observed"]) if evidence else None,
                "evidence_status": row.get("status"),
                "household_revision": body.get("household_revision"),
                "handling_at_period_end": handling_state(db, episode["id"], end),
                "period_end": end,
                "source_snoozed_until": row.get("snoozedUntil"),
            }
        )
    handling_counts = dict(
        db.execute(
            "SELECT operation,COUNT(*) FROM attention_handling WHERE policy=? AND at>=? AND at<? "
            "GROUP BY operation",
            (policy, start, end),
        ).fetchall()
    )
    handling_rows = db.execute(
        "SELECT * FROM attention_handling WHERE policy=? AND at>=? AND at<? "
        "ORDER BY seq DESC LIMIT 100",
        (policy, start, end),
    ).fetchall()
    context = db.execute(
        "SELECT at,body FROM attention_context WHERE policy=? AND at<? ORDER BY seq DESC LIMIT 1",
        (policy, end),
    ).fetchone()
    knowledge = json.loads(context["body"]) if context else None
    if knowledge:
        knowledge["rules"] = [
            r
            for r in knowledge.get("rules", [])
            if r.get("status") == "approved" and r.get("enabled")
        ]
    quality_tracking = db.execute(
        "SELECT started FROM attention_quality_tracking WHERE policy=?", (policy,)
    ).fetchone()
    whole_day_details = bool(quality_tracking and quality_tracking[0] <= start)
    gap_rows = db.execute(
        "SELECT * FROM attention_quality WHERE policy=? AND opened<? "
        "AND (closed IS NULL OR closed>?) ORDER BY opened DESC",
        (policy, end, start),
    ).fetchall()
    gap_details = []
    for gap in gap_rows[:100]:
        a, b = (
            max(start, gap["opened"]),
            min(end, gap["closed"] if gap["closed"] is not None else end),
        )
        closed = gap["closed"] if gap["closed"] is not None and gap["closed"] < end else None
        gap_details.append(
            {
                "source": gap["source"],
                "row_id": gap["row_id"],
                "name": json.loads(gap["body"])["name"],
                "status": gap["status"],
                "reason": gap["reason"],
                "first_observed_at": iso(gap["opened"]),
                "closed_at": iso(closed) if closed is not None else None,
                "close_reason": gap["close_reason"] if closed is not None else None,
                "observed_seconds": measure(
                    [(max(a, x), min(b, y)) for x, y in observed if x < b and y > a]
                ),
            }
        )
    # Include the version active at midnight, followed by observed changes.
    relevant = [r for r in versions if r["observed"] >= start]
    prior = [r for r in versions if r["observed"] < start]
    if prior:
        relevant.insert(0, prior[-1])
    attention_report.enrich_context(db, policy, details, start, end)
    report_overview = attention_report.overview(report_focus.group_sources(details))
    assessment = (
        "not_run"
        if quality == "not_run"
        else "attention_needed"
        if report_overview["sources_requiring_review"]
        else "insufficient_data"
        if quality != "complete" or len(episodes) > len(details)
        else "no_action_needed"
        if counts["issues"].get("episodes_present")
        else "no_anomaly_found"
    )
    return {
        "schema_version": 2,
        "scope": "canonical_attention_policy",
        "policy_id": policy,
        "day": day.isoformat(),
        "timezone": zone,
        "start": iso(start),
        "end": iso(end),
        "data_quality": quality,
        "assessment": assessment,
        "report_overview": report_overview,
        "observation_scope": attention_report.observation_scope(
            end - start, observed_seconds, covered_seconds, quality
        ),
        "coverage_seconds": covered_seconds,
        "observed_seconds": observed_seconds,
        "window_seconds": end - start,
        "coverage_gaps": gaps,
        "unobserved_intervals": [
            {"start": iso(a), "end": iso(b), "seconds": round(b - a, 3)} for a, b in unobserved
        ],
        "counts": counts,
        "availability": {
            "sources_with_gaps": len({g["source"] for g in gap_rows})
            if whole_day_details
            else None,
            "observed_sources_with_gaps": len({g["source"] for g in gap_rows}),
            "intervals_present": len(gap_rows),
            "details": gap_details,
            "details_truncated": len(gap_rows) > len(gap_details),
            "details_available_since": iso(quality_tracking[0]) if quality_tracking else None,
            "whole_day_details": whole_day_details,
        },
        "episodes": details,
        "episodes_truncated": len(episodes) > len(details),
        "handling_counts": handling_counts,
        "handling_events": [dict(r) for r in handling_rows],
        "handling_events_truncated": sum(handling_counts.values()) > len(handling_rows),
        "policy_revisions": [
            {
                "observed_at": iso(r["observed"]),
                "hash": r["policy_hash"],
                "revision": json.loads(r["body"])["revision"],
            }
            for r in relevant
        ],
        "household_context": {
            "status": "observed" if context else "not_observed",
            "observed_at": iso(context["at"]) if context else None,
            "snapshot": knowledge,
        },
        "limitations": [
            "Counts use canonical policy episodes; native source evidence is not added again.",
            "First observation is not the physical fault start; channel removal is not a repair.",
            "User-claimed fixes and seen flags do not establish observed source clearance.",
            "Confirmed active seconds include only covered intervals, not unobserved time.",
            "Brief pending/inactive transitions are not incident episodes.",
            "Updates and informational events are separate from attention-needing issues.",
            "Energy, heating and NAS numeric analyses are not included in this scope.",
        ],
    }
