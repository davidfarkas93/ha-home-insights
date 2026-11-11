"""Report attempts and successful results, persisted independently of source state."""

import json
from uuid import uuid4

from .models import canonical


def setup_reports(db, now):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS reports(
        id TEXT PRIMARY KEY, policy TEXT NOT NULL, day TEXT NOT NULL,
        started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
        ai_status TEXT NOT NULL, actor TEXT NOT NULL, facts TEXT,
        narrative TEXT, error TEXT);
      CREATE INDEX IF NOT EXISTS report_policy_day ON reports(policy,day,started DESC);
      CREATE UNIQUE INDEX IF NOT EXISTS report_running ON reports(policy) WHERE status='running';
    """)
    if "diagnostics" not in {r["name"] for r in db.execute("PRAGMA table_info(reports)")}:
        with db:
            db.execute("ALTER TABLE reports ADD COLUMN diagnostics TEXT")
    with db:
        db.execute(
            "UPDATE reports SET status='failed',ai_status='failed',error='interrupted',"
            "finished=? WHERE status='running'",
            (now,),
        )


def decode(row):
    if row is None:
        return None
    return {
        **dict(row),
        "facts": json.loads(row["facts"]) if row["facts"] else None,
        "narrative": json.loads(row["narrative"]) if row["narrative"] else None,
        "diagnostics": json.loads(row["diagnostics"]) if row["diagnostics"] else None,
    }


def begin_report(journal, policy, day, actor, now, force=False):
    db = journal.db
    existing = db.execute(
        "SELECT * FROM reports WHERE policy=? AND day=? AND status='completed' "
        "ORDER BY started DESC LIMIT 1",
        (policy, day),
    ).fetchone()
    if existing and not force:
        return {"cached": True, "attempt": decode(existing)}
    if db.execute(
        "SELECT 1 FROM reports WHERE policy=? AND status='running'", (policy,)
    ).fetchone():
        raise ValueError("A report is already running")
    identifier = str(uuid4())
    with db:
        db.execute(
            "INSERT INTO reports(id,policy,day,started,status,ai_status,actor) "
            "VALUES(?,?,?,?,'running','pending',?)",
            (identifier, policy, day, now, actor),
        )
        journal._event(
            "report_started", now, {"policy_id": policy, "day": day, "report_id": identifier}
        )
    return {
        "cached": False,
        "attempt": decode(db.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone()),
    }


def save_report_facts(journal, identifier, facts):
    with journal.db:
        journal.db.execute(
            "UPDATE reports SET facts=? WHERE id=? AND status='running'",
            (canonical(facts), identifier),
        )


def finish_report(
    journal, identifier, now, narrative=None, error=None, skipped=False, diagnostics=None
):
    db = journal.db
    with db:
        changed = db.execute(
            "UPDATE reports SET finished=?,status=?,ai_status=?,narrative=?,error=?,diagnostics=? "
            "WHERE id=? AND status='running'",
            (
                now,
                "failed" if error else "completed",
                "failed" if error else "skipped" if skipped else "success",
                canonical(narrative) if narrative is not None else None,
                error,
                canonical(diagnostics) if diagnostics else None,
                identifier,
            ),
        ).rowcount
        row = db.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone()
        if changed:
            journal._event(
                "report_failed" if error else "report_completed",
                now,
                {
                    "policy_id": row["policy"],
                    "day": row["day"],
                    "report_id": identifier,
                    "error": error,
                },
            )
    return decode(row)


def read_reports(journal, policy, day):
    db = journal.db
    current = db.execute(
        "SELECT * FROM reports WHERE policy=? AND day=? ORDER BY started DESC LIMIT 1",
        (policy, day),
    ).fetchone()
    success = db.execute(
        "SELECT * FROM reports WHERE policy=? AND status='completed' "
        "AND ai_status='success' ORDER BY day DESC,started DESC LIMIT 1",
        (policy,),
    ).fetchone()
    latest = db.execute(
        "SELECT id,day,started,status,finished FROM reports WHERE policy=? "
        "ORDER BY started DESC LIMIT 1",
        (policy,),
    ).fetchone()
    return {
        "attempt": decode(current),
        "last_success": decode(success),
        "last_attempt": dict(latest) if latest else None,
    }
