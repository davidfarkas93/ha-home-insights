"""Fixed native Recorder history/run reads, always on its own executor."""

from datetime import UTC


def stamp(value):
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp()


def read_recorder(hass, start, end, sources):
    """Fixed version-pinned native Recorder reads on its executor; never raw SQL."""
    from homeassistant.components.recorder.db_schema import RecorderRuns
    from homeassistant.components.recorder.history import get_significant_states
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import or_, select

    history = get_significant_states(
        hass,
        start,
        end,
        sources,
        include_start_time_state=True,
        significant_changes_only=False,
        no_attributes=False,
    )
    with session_scope(hass=hass, read_only=True) as session:
        rows = (
            session.execute(
                select(RecorderRuns)
                .where(
                    RecorderRuns.start < end,
                    or_(RecorderRuns.end.is_(None), RecorderRuns.end > start),
                )
                .order_by(RecorderRuns.start)
                .limit(1001)
            )
            .scalars()
            .all()
        )
        if len(rows) > 1000:
            raise ValueError("Recorder run query exceeded its bounded budget")
        runs = [
            {
                "start": stamp(r.start),
                "end": stamp(r.end) if r.end else None,
                "closed_incorrect": bool(r.closed_incorrect),
            }
            for r in rows
        ]
    return history, runs
