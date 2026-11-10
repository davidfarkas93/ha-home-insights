"""Audited user metadata. Never clears a source or executes a device action."""

import json
import math
from uuid import uuid4

from .models import canonical, digest

OPERATIONS = {"seen", "claimed_fixed", "snooze", "unsnooze", "note"}


def setup_storage(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS attention_rows(
        policy TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL, episode TEXT,
        base_key TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(policy,id));
      CREATE TABLE IF NOT EXISTS attention_episodes(
        id TEXT PRIMARY KEY, policy TEXT NOT NULL, row_id TEXT NOT NULL, opened REAL NOT NULL,
        closed REAL, close_reason TEXT, initial INTEGER NOT NULL, body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS attention_open ON attention_episodes(policy,closed);
      CREATE TABLE IF NOT EXISTS attention_requests(
        actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        response TEXT NOT NULL, PRIMARY KEY(actor,request_id));
      CREATE TABLE IF NOT EXISTS attention_handling(
        seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, policy TEXT NOT NULL,
        row_id TEXT NOT NULL, episode TEXT NOT NULL, revision INTEGER NOT NULL,
        at REAL NOT NULL, operation TEXT NOT NULL, actor TEXT NOT NULL,
        actor_name TEXT NOT NULL, until REAL, note TEXT NOT NULL,
        UNIQUE(episode,revision));
      CREATE INDEX IF NOT EXISTS handling_episode ON attention_handling(episode,seq DESC);
      CREATE INDEX IF NOT EXISTS handling_policy_day ON attention_handling(policy,at);
      CREATE TABLE IF NOT EXISTS attention_context(
        seq INTEGER PRIMARY KEY, policy TEXT NOT NULL, at REAL NOT NULL,
        hash TEXT NOT NULL, body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS context_policy_time ON attention_context(policy,at);
      CREATE TABLE IF NOT EXISTS attention_quality_tracking(
        policy TEXT PRIMARY KEY, started REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS attention_quality(
        seq INTEGER PRIMARY KEY, policy TEXT NOT NULL, row_id TEXT NOT NULL,
        source TEXT NOT NULL, opened REAL NOT NULL, closed REAL, close_reason TEXT,
        status TEXT NOT NULL, reason TEXT NOT NULL, body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS quality_policy_day ON attention_quality(policy,opened);
      CREATE UNIQUE INDEX IF NOT EXISTS quality_open ON attention_quality(policy,row_id)
        WHERE closed IS NULL;
    """)


def record_quality(db, policy, rows, now, complete):
    db.execute("INSERT OR IGNORE INTO attention_quality_tracking VALUES(?,?)", (policy, now))
    opened = {
        r["row_id"]: r
        for r in db.execute(
            "SELECT * FROM attention_quality WHERE policy=? AND closed IS NULL", (policy,)
        )
    }
    for row in rows:
        old = opened.pop(row["id"], None)
        missing = row["status"] in ("unavailable", "loading")
        if old and missing and old["status"] == row["status"] and old["reason"] == row["reason"]:
            continue
        if old:
            db.execute(
                "UPDATE attention_quality SET closed=?,close_reason=? WHERE seq=?",
                (now, "changed" if missing else "state_observed", old["seq"]),
            )
        if missing:
            db.execute(
                "INSERT INTO attention_quality(policy,row_id,source,opened,status,reason,body) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    policy,
                    row["id"],
                    row["source"],
                    now,
                    row["status"],
                    row["reason"],
                    canonical(row),
                ),
            )
    if complete:
        for old in opened.values():
            db.execute(
                "UPDATE attention_quality SET closed=?,close_reason='no_longer_monitored' "
                "WHERE seq=?",
                (now, old["seq"]),
            )


def context_observed(journal, policy, snapshot, now):
    if snapshot is None:
        return
    fingerprint = digest(snapshot)
    old = journal.db.execute(
        "SELECT hash FROM attention_context WHERE policy=? ORDER BY seq DESC LIMIT 1", (policy,)
    ).fetchone()
    if old and old[0] == fingerprint:
        return
    journal.db.execute(
        "INSERT INTO attention_context(policy,at,hash,body) VALUES(?,?,?,?)",
        (policy, now, fingerprint, canonical(snapshot)),
    )


def handling_state(db, episode, before=None):
    state = {"revision": 0, "snooze_override": False}
    rows = db.execute(
        "SELECT * FROM attention_handling WHERE episode=? AND at<? ORDER BY revision",
        (episode, before if before is not None else 1e20),
    )
    for row in rows:
        state["revision"] = row["revision"]
        if row["operation"] == "seen":
            state["seen_at"] = row["at"]
        elif row["operation"] == "claimed_fixed":
            state["claimed_fixed_at"] = row["at"]
        elif row["operation"] in ("snooze", "unsnooze"):
            state["snooze_override"] = True
            state["snoozed_until"] = row["until"]
    return state


def project_handling(db, rows, now):
    result = []
    for original in rows:
        row = {**original}
        if episode := row.get("episodeId"):
            state = handling_state(db, episode)
            row["handling"] = state
            if state["snooze_override"]:
                row.pop("snoozedUntil", None)
                until = state.get("snoozed_until")
                if until and until > now and row.get("dismissible") and row["tone"] != "error":
                    row["snoozedUntil"] = until
        result.append(row)
    return result


def validate_request(value, now, retry=False):
    if not isinstance(value, dict) or set(value) - {
        "policy_id",
        "request_id",
        "operation",
        "targets",
        "until",
        "note",
    }:
        raise ValueError("Invalid handling request")
    if value.get("operation") not in OPERATIONS:
        raise ValueError("Unknown handling operation")
    if not isinstance(value.get("request_id"), str) or not 1 <= len(value["request_id"]) <= 80:
        raise ValueError("A bounded request ID is required")
    if not isinstance(value.get("policy_id"), str) or not value["policy_id"]:
        raise ValueError("Policy ID required")
    targets = value.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
        raise ValueError("Choose between 1 and 100 episode targets")
    ids = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "row_id",
            "episode_id",
            "expected_revision",
        }:
            raise ValueError("Invalid handling target")
        if any(not isinstance(target[k], str) or not target[k] for k in ("row_id", "episode_id")):
            raise ValueError("Episode and row identity required")
        if type(target["expected_revision"]) is not int or target["expected_revision"] < 0:
            raise ValueError("Invalid handling revision")
        if target["row_id"] in ids:
            raise ValueError("Duplicate handling target")
        ids.add(target["row_id"])
    note = value.get("note", "")
    if not isinstance(note, str) or len(note) > 1000:
        raise ValueError("Notes must be at most 1000 characters")
    if value["operation"] == "note" and not note.strip():
        raise ValueError("Note text required")
    until = value.get("until")
    if value["operation"] == "snooze":
        if (
            type(until) not in (int, float)
            or not math.isfinite(until)
            or until <= 0
            or not retry
            and not now < until <= now + 30 * 86400
        ):
            raise ValueError("Snooze deadline must be in the next 30 days")
    elif until is not None:
        raise ValueError("Only snooze accepts a deadline")
    return {
        **value,
        "note": note.strip(),
        "until": until,
        "targets": sorted(targets, key=lambda t: t["row_id"]),
    }


def record_handling(journal, value, actor, actor_name, now):
    # Validate structural fields even on retry; a previously successful snooze
    # can legitimately have expired before an idempotent retry arrives.
    if not isinstance(value, dict):
        raise ValueError("Invalid handling request")
    original = journal.db.execute(
        "SELECT fingerprint,response FROM attention_requests WHERE actor=? AND request_id=?",
        (actor, value.get("request_id")),
    ).fetchone()
    normalized = validate_request(value, now, retry=original is not None)
    fingerprint = digest(normalized)
    if original:
        if original["fingerprint"] != fingerprint:
            raise ValueError("Request ID was already used for a different operation")
        return json.loads(original["response"])
    response = {"ok": True, "request_id": value["request_id"], "targets": []}
    db = journal.db
    with db:
        for target in normalized["targets"]:
            current = db.execute(
                "SELECT * FROM attention_rows WHERE policy=? AND id=?",
                (normalized["policy_id"], target["row_id"]),
            ).fetchone()
            if current is None or current["episode"] != target["episode_id"]:
                raise ValueError("Episode changed; refresh before handling it")
            state = handling_state(db, current["episode"])
            if state["revision"] != target["expected_revision"]:
                raise ValueError("Handling revision changed; refresh before trying again")
            row = json.loads(current["body"])
            if normalized["operation"] == "snooze" and (
                not row["dismissible"] or row["tone"] == "error"
            ):
                raise ValueError("This source cannot be snoozed")
            revision = state["revision"] + 1
            db.execute(
                "INSERT INTO attention_handling(id,policy,row_id,episode,revision,at,operation,"
                "actor,actor_name,until,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    normalized["policy_id"],
                    target["row_id"],
                    current["episode"],
                    revision,
                    now,
                    normalized["operation"],
                    actor,
                    actor_name,
                    normalized["until"],
                    normalized["note"],
                ),
            )
            journal._event(
                "attention_handling_recorded",
                now,
                {
                    "policy_id": normalized["policy_id"],
                    "row_id": target["row_id"],
                    "revision": revision,
                    "operation": normalized["operation"],
                    "actor": actor,
                    "actor_name": actor_name,
                    "until": normalized["until"],
                    "note": normalized["note"],
                },
                episode=current["episode"],
                policy_hash=journal.db.execute(
                    "SELECT policy_hash FROM events WHERE episode=? ORDER BY seq DESC LIMIT 1",
                    (current["episode"],),
                ).fetchone()[0],
            )
            response["targets"].append({"row_id": target["row_id"], "revision": revision})
        db.execute(
            "INSERT INTO attention_requests VALUES(?,?,?,?)",
            (actor, normalized["request_id"], fingerprint, canonical(response)),
        )
    return response


def episode_detail(db, policy, episode, before=None, limit=20):
    source = db.execute(
        "SELECT * FROM attention_episodes WHERE policy=? AND id=?", (policy, episode)
    ).fetchone()
    if source is None:
        raise ValueError("Unknown episode")
    events = db.execute(
        "SELECT * FROM events WHERE episode=? AND seq<? ORDER BY seq DESC LIMIT ?",
        (episode, before or 9223372036854775807, limit + 1),
    ).fetchall()
    return {
        "schema_version": 1,
        "policy_id": policy,
        "episode_id": episode,
        "row_id": source["row_id"],
        "source": json.loads(source["body"])["source"],
        "opened_at": source["opened"],
        "closed_at": source["closed"],
        "close_reason": source["close_reason"],
        "handling": handling_state(db, episode),
        "events": [
            {
                "seq": e["seq"],
                "observed": e["observed"],
                "type": e["type"],
                **{
                    k: v
                    for k, v in json.loads(e["body"]).items()
                    if k in ("operation", "actor_name", "until", "note", "revision")
                },
                "source_status": json.loads(e["body"]).get("row", {}).get("status"),
            }
            for e in events[:limit]
        ],
        "has_more": len(events) > limit,
        "next_before": events[limit - 1]["seq"] if len(events) > limit else None,
    }
