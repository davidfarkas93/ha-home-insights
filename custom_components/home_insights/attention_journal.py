"""Persistent policy episodes, separate from native source-evidence episodes."""

import json
from uuid import uuid4

from .attention import attention_hash
from .models import Observation, canonical, digest


def record_attention(
    journal,
    policy_id,
    policy,
    rows,
    now,
    resumed=False,
    complete=True,
    source_times=None,
    household_revision=None,
):
    db = journal.db
    db.executescript("""
      CREATE TABLE IF NOT EXISTS attention_rows(
        policy TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL, episode TEXT,
        base_key TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(policy,id));
      CREATE TABLE IF NOT EXISTS attention_episodes(
        id TEXT PRIMARY KEY, policy TEXT NOT NULL, row_id TEXT NOT NULL, opened REAL NOT NULL,
        closed REAL, close_reason TEXT, initial INTEGER NOT NULL, body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS attention_open ON attention_episodes(policy,closed);
    """)
    policy_hash = digest(policy)
    with db:
        if db.execute(
            "INSERT OR IGNORE INTO policies VALUES(?,?,?)", (policy_hash, now, canonical(policy))
        ).rowcount:
            journal._event(
                "attention_policy_observed",
                now,
                {
                    "policy_id": policy_id,
                    "policy_hash": policy_hash,
                    "revision": policy["revision"],
                },
                policy_hash=policy_hash,
            )
        journal._coverage(f"attention:{policy_id}", None if complete else "incomplete_sources", now)
        seen = set()
        for row in rows:
            identifier = row["id"]
            seen.add(identifier)
            old = db.execute(
                "SELECT * FROM attention_rows WHERE policy=? AND id=?", (policy_id, identifier)
            ).fetchone()
            active = row["status"] in ("active", "installing", "event")
            episode = old["episode"] if old else None
            key = old["key"] if old and episode and old["base_key"] == row["key"] else row["key"]
            prior_body = json.loads(old["body"]) if old else None
            observation = Observation(
                adapter=f"attention:{policy_id}",
                source_id=identifier,
                status="active" if active else "unknown",
                fingerprint=row["key"],
                title=row["name"],
                message=row["message"],
                source_time=(source_times or {}).get(row["source"]),
                facts={"row": row, "policy_hash": policy_hash, "revision": policy["revision"]},
            )

            def emit(kind, row=row, episode_value=None, observation=observation):
                journal._event(
                    "attention_" + kind,
                    now,
                    {
                        "policy_id": policy_id,
                        "policy_hash": policy_hash,
                        "row": row,
                        "household_revision": household_revision,
                    },
                    observation,
                    episode_value,
                    policy_hash=policy_hash,
                )

            if episode and active and old["base_key"] != row["key"]:
                db.execute(
                    "UPDATE attention_episodes SET closed=?,close_reason='superseded' WHERE id=?",
                    (now, episode),
                )
                emit("superseded", episode_value=episode)
                episode = None
            if active and not episode:
                episode = str(uuid4())
                initial = resumed or old is None or old["status"] in ("unavailable", "loading")
                db.execute(
                    "INSERT INTO attention_episodes VALUES(?,?,?,?,NULL,NULL,?,?)",
                    (episode, policy_id, identifier, now, int(initial), canonical(row)),
                )
                rule = next((r for r in policy["rules"] if r["id"] == row["rule"]), {})
                if ("above" in rule or "below" in rule) and rule.get(
                    "identity", "condition"
                ) == "condition":
                    # Preserve initial legacy threshold keys; a later episode gets
                    # a durable nonce rather than a browser-local recurrence map.
                    nonce = 0 if old is None else episode
                    key = "a" + attention_hash(f"{row['key']}|{nonce}")
                emit("first_observed" if initial else "started", episode_value=episode)
            elif episode and row["status"] in ("inactive", "excluded", "pending"):
                reason = {
                    "inactive": "condition_cleared",
                    "excluded": "excluded",
                    "pending": "pending",
                }[row["status"]]
                db.execute(
                    "UPDATE attention_episodes SET closed=?,close_reason=? WHERE id=?",
                    (now, reason, episode),
                )
                emit(reason, episode_value=episode)
                episode = None
            elif (
                episode
                and row["status"] in ("unavailable", "loading")
                and old["status"] not in ("unavailable", "loading")
            ):
                emit("observation_lost", episode_value=episode)
            elif episode and active and old["status"] in ("unavailable", "loading"):
                emit("observation_restored", episode_value=episode)
            elif old and old["status"] != row["status"]:
                emit("changed", episode_value=episode)
            if prior_body and prior_body.get("snoozedUntil") != row.get("snoozedUntil"):
                emit("snooze_observed", episode_value=episode)
            stored = {**row, "key": key, **({"episodeId": episode} if episode else {})}
            db.execute(
                "INSERT OR REPLACE INTO attention_rows VALUES(?,?,?,?,?,?,?)",
                (policy_id, identifier, row["status"], episode, row["key"], key, canonical(stored)),
            )
        if complete:
            for old in db.execute(
                "SELECT * FROM attention_rows WHERE policy=? AND episode IS NOT NULL", (policy_id,)
            ).fetchall():
                if old["id"] in seen:
                    continue
                row = json.loads(old["body"])
                kind = (
                    "notification_removed"
                    if row["kind"] == "notification"
                    else "no_longer_monitored"
                )
                db.execute(
                    "UPDATE attention_episodes SET closed=?,close_reason=? WHERE id=?",
                    (now, kind, old["episode"]),
                )
                db.execute(
                    "UPDATE attention_rows SET status='absent',episode=NULL "
                    "WHERE policy=? AND id=?",
                    (policy_id, old["id"]),
                )
                journal._event(
                    "attention_" + kind,
                    now,
                    {"policy_id": policy_id, "policy_hash": policy_hash, "row": row},
                    episode=old["episode"],
                    policy_hash=policy_hash,
                )
    return [
        json.loads(
            db.execute(
                "SELECT body FROM attention_rows WHERE policy=? AND id=?", (policy_id, r["id"])
            ).fetchone()[0]
        )
        for r in rows
    ]
