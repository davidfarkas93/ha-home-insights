"""Pinned native Recorder contract and empty/partial report persistence."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context

from custom_components.home_insights import heating, reporting
from custom_components.home_insights.models import daily_window

from .test_heating import DAY, PROFILE, event, facts, fixture, observed
from .test_runtime import running_coordinator


def test_native_recorder_runs_are_read_with_fixed_read_only_orm_scope(hass):
    from homeassistant.components.recorder import history, util
    from homeassistant.components.recorder.db_schema import RecorderRuns
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    a, b = daily_window(DAY, "Europe/Budapest")
    engine = create_engine("sqlite:///:memory:")
    RecorderRuns.__table__.create(engine)
    with Session(engine) as session:
        session.add_all(
            [
                RecorderRuns(
                    start=datetime.fromtimestamp(a - 90000, UTC),
                    end=datetime.fromtimestamp(a - 86400, UTC),
                    closed_incorrect=False,
                ),
                RecorderRuns(
                    start=datetime.fromtimestamp(a - 100, UTC),
                    end=datetime.fromtimestamp(a + 100, UTC),
                    closed_incorrect=False,
                ),
                RecorderRuns(
                    start=datetime.fromtimestamp(a + 150, UTC),
                    end=datetime.fromtimestamp(a + 200, UTC),
                    closed_incorrect=True,
                ),
                RecorderRuns(
                    start=datetime.fromtimestamp(b + 100, UTC), end=None, closed_incorrect=False
                ),
            ]
        )
        session.commit()

    @contextmanager
    def read_scope(**kwargs):
        assert kwargs == {"hass": hass, "read_only": True}
        with Session(engine) as session:
            yield session

    with (
        patch.object(util, "session_scope", read_scope),
        patch.object(history, "get_significant_states", return_value={}) as native,
    ):
        _, runs = heating.read_recorder(
            hass,
            datetime.fromtimestamp(a - 86400, UTC),
            datetime.fromtimestamp(b, UTC),
            list(PROFILE["sources"].values()),
        )
    assert len(runs) == 2
    assert runs[0]["start"] == a - 100 and runs[1]["closed_incorrect"]
    assert native.call_args.kwargs == {
        "include_start_time_state": True,
        "significant_changes_only": False,
        "no_attributes": False,
    }
    engine.dispose()


async def test_no_observations_skip_ai_and_partial_percent_is_not_rounded_to_clean(hass):
    owner = await running_coordinator(hass)
    try:
        await owner.heating.set_profile("demo", PROFILE)
        h, r = fixture()
        h.pop(PROFILE["sources"]["flame"])
        f = facts(h, r)
        with (
            patch.object(owner.heating, "facts", AsyncMock(return_value=f)),
            patch.object(reporting, "summarize", AsyncMock()) as ai,
        ):
            result = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), heating_profile="demo"
            )
        assert result["attempt"]["ai_status"] == "skipped"
        ai.assert_not_called()
        h, r = observed()
        event(h, "connectivity", 5000, "off")
        event(h, "connectivity", 5000.01, "on")
        f = facts(h, r)
        assert f["coverage_percent"] < 100
        bundle = reporting.evidence_bundle(f)
        with (
            patch.object(owner.heating, "facts", AsyncMock(return_value=f)),
            patch.object(
                reporting,
                "summarize",
                AsyncMock(
                    return_value=SimpleNamespace(
                        data={"summary": bundle["summary_options"][0]["text"]}
                    )
                ),
            ),
        ):
            result = await owner.reports.generate(
                None, "ai_task.example", DAY, Context(), True, heating_profile="demo"
            )
        assert result["attempt"]["facts"]["coverage_percent"] < 100
    finally:
        await owner.async_close()
