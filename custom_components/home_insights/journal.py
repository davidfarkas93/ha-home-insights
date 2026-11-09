"""Transactional SQLite journal. All methods run on one dedicated executor thread."""

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import Observation, canonical, daily_window, digest, iso

SOURCE_REMOVALS = {
    "notifications": "notification_removed",
    "repairs": "repair_no_longer_reported",
    "integrations": "integration_removed",
}


class Journal:
    """Source truth, coverage, episodes and metadata stay separate."""

    def __init__(self, path: str | Path, now: float, policy: dict[str, Any]) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        schema = self.db.execute("PRAGMA user_version").fetchone()[0]
        if schema not in (0, 1):
            self.db.close()
            raise ValueError("Unsupported journal schema")
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA busy_timeout=5000;
          CREATE TABLE IF NOT EXISTS policies(
            hash TEXT PRIMARY KEY, observed REAL NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, started REAL NOT NULL, last_seen REAL NOT NULL,
            closed REAL, reason TEXT);
          CREATE TABLE IF NOT EXISTS coverage(
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, started REAL NOT NULL,
            ended REAL, reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observations(
            issue_id TEXT PRIMARY KEY, adapter TEXT NOT NULL, status TEXT NOT NULL,
            fingerprint TEXT NOT NULL, episode TEXT, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS episodes(
            id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, opened REAL NOT NULL,
            last_confirmed REAL NOT NULL, closed REAL, close_reason TEXT,
            initial INTEGER NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, observed REAL NOT NULL,
            source_time TEXT, issue_id TEXT, episode TEXT, type TEXT NOT NULL,
            policy_hash TEXT NOT NULL, body TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS events_observed ON events(observed);
          CREATE TABLE IF NOT EXISTS entity_states(
            entity_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL);
          PRAGMA user_version=1;
        """)
        self.run_id = str(uuid4())
        self.policy_hash = digest(policy)
        self.baselines: set[str] = set()
        with self.db:
            self.db.execute(
                "UPDATE runs SET closed=last_seen, reason='interrupted' WHERE closed IS NULL"
            )
            self.db.execute("INSERT INTO runs VALUES(?,?,?,NULL,NULL)", (self.run_id, now, now))
            old = self.db.execute(
                "SELECT hash FROM policies ORDER BY observed DESC LIMIT 1"
            ).fetchone()
            self.db.execute(
                "INSERT OR IGNORE INTO policies VALUES(?,?,?)",
                (self.policy_hash, now, canonical(policy)),
            )
            if not old or old[0] != self.policy_hash:
                self._event("policy_observed", now, {"policy": policy})
            self._event("observer_started", now, {})
            self._coverage("observer", "starting", now)
            for adapter in policy.get("adapters", []):
                self._coverage(adapter, "starting", now)
            for entity in policy.get("monitored_entities", []):
                self._coverage(f"entity:{entity}", "starting", now)
            selected = set(policy.get("monitored_entities", []))
            for row in self.db.execute(
                "SELECT source FROM coverage WHERE source LIKE 'entity:%' AND ended IS NULL"
            ).fetchall():
                if row["source"].removeprefix("entity:") not in selected:
                    self.db.execute(
                        "UPDATE coverage SET ended=? WHERE source=? AND ended IS NULL",
                        (now, row["source"]),
                    )
                    self._event("monitoring_removed", now, {"source": row["source"]})

    def _event(
        self,
        kind: str,
        now: float,
        body: dict[str, Any],
        observation: Observation | None = None,
        episode: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO events(id,observed,source_time,issue_id,episode,type,policy_hash,body) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                now,
                observation.source_time if observation else None,
                observation.issue_id if observation else None,
                episode,
                kind,
                self.policy_hash,
                canonical(body),
            ),
        )

    def _coverage(self, source: str, reason: str | None, now: float) -> None:
        old = self.db.execute(
            "SELECT * FROM coverage WHERE source=? AND ended IS NULL", (source,)
        ).fetchone()
        if (old and old["reason"] == reason) or (not old and not reason):
            return
        if old:
            self.db.execute("UPDATE coverage SET ended=? WHERE id=?", (now, old["id"]))
        if reason:
            self.db.execute(
                "INSERT INTO coverage(source,started,reason) VALUES(?,?,?)", (source, now, reason)
            )
        self._event(
            "coverage_lost" if reason else "coverage_restored",
            now,
            {"source": source, "reason": reason},
        )

    def gap(self, reason: str, now: float) -> None:
        """Unknown history must never become a healthy-to-fault transition."""
        with self.db:
            self.baselines.clear()
            self._coverage("observer", reason, now)

    def source_failure(self, adapter: str, reason: str, now: float) -> None:
        with self.db:
            self.baselines.discard(adapter)
            self._coverage(adapter, reason, now)

    def batch(self, adapter: str, rows: list[Observation], now: float) -> None:
        """Apply one complete, successful adapter snapshot atomically."""
        if adapter not in SOURCE_REMOVALS:
            raise ValueError("Unknown adapter")
        if any(row.adapter != adapter for row in rows):
            raise ValueError("Mixed adapter snapshot")
        if len({row.issue_id for row in rows}) != len(rows):
            raise ValueError("Duplicate source in snapshot")
        continuous = adapter in self.baselines
        seen = {row.issue_id for row in rows}
        with self.db:
            self._coverage(adapter, None, now)
            for row in rows:
                self._coverage(
                    f"condition:{adapter}:{row.source_id}",
                    "source_state_unknown" if row.status == "unknown" else None,
                    now,
                )
                old = self.db.execute(
                    "SELECT * FROM observations WHERE issue_id=?", (row.issue_id,)
                ).fetchone()
                episode = old["episode"] if old else None
                body = row.as_dict()
                if row.status == "active":
                    active_body = (
                        self.db.execute(
                            "SELECT body FROM episodes WHERE id=?", (episode,)
                        ).fetchone()
                        if episode
                        else None
                    )
                    if active_body and json.loads(active_body[0])["fingerprint"] != row.fingerprint:
                        self._close(episode, "superseded", now)
                        self._event("episode_superseded", now, body, row, episode)
                        episode = None
                    if not episode:
                        episode = str(uuid4())
                        initial = not continuous or old is not None and old["status"] == "unknown"
                        self.db.execute(
                            "INSERT INTO episodes VALUES(?,?,?,?,NULL,NULL,?,?)",
                            (episode, row.issue_id, now, now, int(initial), canonical(body)),
                        )
                        self._event(
                            "first_observed" if initial else "source_appeared",
                            now,
                            body,
                            row,
                            episode,
                        )
                    elif old["status"] == "unknown":
                        self._event("observation_restored", now, body, row, episode)
                    self.db.execute(
                        "UPDATE episodes SET last_confirmed=?,body=? WHERE id=?",
                        (now, canonical(body), episode),
                    )
                elif episode and row.status in ("clear", "excluded"):
                    kind = "source_cleared" if row.status == "clear" else "excluded_from_monitoring"
                    self._close(episode, kind, now)
                    self._event(kind, now, body, row, episode)
                    episode = None
                elif episode and row.status == "unknown" and old["status"] != "unknown":
                    self._event("observation_lost", now, body, row, episode)
                self.db.execute(
                    "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)",
                    (row.issue_id, adapter, row.status, row.fingerprint, episode, canonical(body)),
                )
            for old in self.db.execute(
                "SELECT * FROM observations WHERE adapter=? AND status!='absent'", (adapter,)
            ).fetchall():
                if old["issue_id"] in seen:
                    continue
                observation = Observation(**json.loads(old["body"]))
                kind = SOURCE_REMOVALS[adapter]
                source = f"condition:{adapter}:{observation.source_id}"
                if self.db.execute(
                    "UPDATE coverage SET ended=? WHERE source=? AND ended IS NULL", (now, source)
                ).rowcount:
                    self._event("monitoring_removed", now, {"source": source})
                if old["episode"]:
                    self._close(old["episode"], kind, now)
                self._event(kind, now, observation.as_dict(), observation, old["episode"])
                self.db.execute(
                    "UPDATE observations SET status='absent',episode=NULL WHERE issue_id=?",
                    (old["issue_id"],),
                )
        self.baselines.add(adapter)

    def _close(self, episode: str, reason: str, now: float) -> None:
        self.db.execute(
            "UPDATE episodes SET closed=?,close_reason=? WHERE id=?", (now, reason, episode)
        )

    def entity(self, entity_id: str, body: dict[str, Any], now: float) -> None:
        """Record measured facts only. No thresholds, alarms or household inference."""
        fingerprint = digest(body)
        with self.db:
            self._coverage(
                f"entity:{entity_id}", None if body["available"] else "no_reliable_state", now
            )
            old = self.db.execute(
                "SELECT fingerprint FROM entity_states WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if old and old[0] == fingerprint:
                return
            self.db.execute(
                "INSERT OR REPLACE INTO entity_states VALUES(?,?,?)",
                (entity_id, fingerprint, canonical(body)),
            )
            self._event(
                "entity_state_observed" if not old else "entity_state_changed",
                now,
                {"entity_id": entity_id, **body},
            )

    def checkpoint(self, now: float) -> None:
        with self.db:
            self._coverage("observer", None, now)
            self.db.execute("UPDATE runs SET last_seen=? WHERE id=?", (now, self.run_id))

    def history(self, before: int | None = None, limit: int = 30) -> dict[str, Any]:
        limit = min(max(limit, 1), 100)
        rows = self.db.execute(
            "SELECT * FROM events WHERE seq<? ORDER BY seq DESC LIMIT ?",
            (before if before is not None else 9223372036854775807, limit + 1),
        ).fetchall()
        result = [{**dict(row), "body": json.loads(row["body"])} for row in rows[:limit]]
        return {
            "events": result,
            "has_more": len(rows) > limit,
            "next_before": result[-1]["seq"] if len(rows) > limit else None,
        }

    def snapshot(self) -> dict[str, Any]:
        open_rows = self.db.execute(
            "SELECT * FROM episodes WHERE closed IS NULL ORDER BY opened DESC"
        ).fetchall()
        gaps = [
            dict(row)
            for row in self.db.execute(
                "SELECT source,started,reason FROM coverage WHERE ended IS NULL ORDER BY source"
            )
        ]
        run = self.db.execute("SELECT * FROM runs WHERE id=?", (self.run_id,)).fetchone()
        return {
            "schema_version": 1,
            "status": "stopped" if run["closed"] else "partial" if gaps else "recording",
            "started_at": iso(self.db.execute("SELECT MIN(started) FROM runs").fetchone()[0]),
            "last_observed_at": iso(run["last_seen"]),
            "policy_hash": self.policy_hash,
            "open_count": len(open_rows),
            "open_episodes": [
                {**dict(row), "body": json.loads(row["body"]), "initial": bool(row["initial"])}
                for row in open_rows[:50]
            ],
            "open_episodes_truncated": len(open_rows) > 50,
            "coverage_gaps": gaps,
            "event_count": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    def daily_facts(self, day: date, zone: str) -> dict[str, Any]:
        start, end = daily_window(day, zone)
        spans = self.db.execute(
            "SELECT started,last_seen FROM runs WHERE started<? AND last_seen>? ORDER BY started",
            (end, start),
        ).fetchall()
        intervals: list[tuple[float, float]] = []
        for span in spans:
            a, b = max(start, span["started"]), min(end, span["last_seen"])
            if intervals and a <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(b, intervals[-1][1]))
            else:
                intervals.append((a, b))
        gaps = [
            dict(row)
            for row in self.db.execute(
                "SELECT source,started,ended,reason FROM coverage "
                "WHERE started<? AND (ended IS NULL OR ended>?) ORDER BY started",
                (end, start),
            )
        ]
        for gap in gaps:
            if gap["source"] == "observer":
                kept = []
                for a, b in intervals:
                    left, right = max(a, gap["started"]), min(b, gap["ended"] or end)
                    if left >= right:
                        kept.append((a, b))
                    else:
                        if a < left:
                            kept.append((a, left))
                        if right < b:
                            kept.append((right, b))
                intervals = kept
        coverage = sum(b - a for a, b in intervals)
        unobserved = []
        cursor = start
        for a, b in [*intervals, (end, end)]:
            if a > cursor:
                unobserved.append({"start": iso(cursor), "end": iso(a), "seconds": a - cursor})
            cursor = max(cursor, b)
        quality = (
            "not_run"
            if not coverage
            else ("insufficient_data" if coverage < end - start or gaps else "complete")
        )
        counts = dict(
            self.db.execute(
                "SELECT type,COUNT(*) FROM events WHERE observed>=? AND observed<? GROUP BY type",
                (start, end),
            ).fetchall()
        )
        active = self.db.execute(
            "SELECT COUNT(*) FROM episodes WHERE opened<? AND (closed IS NULL OR closed>?)",
            (end, start),
        ).fetchone()[0]
        assessment = (
            quality
            if quality != "complete"
            else "attention_needed"
            if active
            else "no_anomaly_found"
        )
        events = self.db.execute(
            "SELECT * FROM events WHERE observed>=? AND observed<? "
            "AND type NOT IN ('entity_state_observed','entity_state_changed') "
            "ORDER BY seq DESC LIMIT 100",
            (start, end),
        ).fetchall()
        total = sum(counts.values())
        raw_count = counts.get("entity_state_observed", 0) + counts.get("entity_state_changed", 0)
        episodes = []
        for row in self.db.execute(
            "SELECT * FROM episodes WHERE opened<? AND (closed IS NULL OR closed>?) "
            "ORDER BY opened DESC LIMIT 100",
            (end, start),
        ):
            evidence = self.db.execute(
                "SELECT observed,body FROM events WHERE episode=? AND observed<? "
                "ORDER BY seq DESC LIMIT 1",
                (row["id"], end),
            ).fetchone()
            episodes.append(
                {
                    "id": row["id"],
                    "issue_id": row["issue_id"],
                    "first_observed_at": iso(row["opened"]),
                    "initial": bool(row["initial"]),
                    "present_at_period_start": row["opened"] < start,
                    "closed_at": iso(row["closed"])
                    if row["closed"] and row["closed"] < end
                    else None,
                    "close_reason": row["close_reason"]
                    if row["closed"] and row["closed"] < end
                    else None,
                    "evidence_at": iso(evidence["observed"]) if evidence else None,
                    "evidence": json.loads(evidence["body"]) if evidence else None,
                }
            )
        return {
            "schema_version": 1,
            "day": day.isoformat(),
            "timezone": zone,
            "start": iso(start),
            "end": iso(end),
            "data_quality": quality,
            "assessment": assessment,
            "coverage_seconds": round(coverage, 3),
            "window_seconds": end - start,
            "coverage_gaps": gaps,
            "unobserved_intervals": unobserved,
            "counts": counts,
            "episodes_present": active,
            "episodes": episodes,
            "episodes_truncated": active > len(episodes),
            "events": [{**dict(row), "body": json.loads(row["body"])} for row in reversed(events)],
            "event_count": total,
            "detail_event_count": total - raw_count,
            "raw_state_event_count": raw_count,
            "events_truncated": total - raw_count > len(events),
            "scope": [
                "HA notifications",
                "HA Repairs",
                "integration setup errors",
                "explicitly selected entity state facts",
            ],
            "limitations": [
                "First observation does not establish the real start time.",
                "Removing a notification does not prove resolution or delivery.",
                "Luma custom rules, energy/heating/NAS analysis and AI are not included yet.",
            ],
        }

    def close(self, now: float) -> None:
        with self.db:
            self._coverage("observer", "stopped", now)
            self.db.execute(
                "UPDATE runs SET closed=?,last_seen=?,reason='stopped' WHERE id=?",
                (now, now, self.run_id),
            )
            self._event("observer_stopped", now, {})
        self.db.close()
