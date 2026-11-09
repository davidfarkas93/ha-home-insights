"""Immutable journal inputs, independent of Home Assistant and presentation."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

type Status = Literal["active", "clear", "unknown", "excluded"]


def canonical(value: Any) -> str:
    """Serialize reproducibly for stored policy and identity hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Observation:
    """A source statement, not an AI diagnosis or a delivery receipt."""

    adapter: str
    source_id: str
    status: Status
    fingerprint: str
    title: str
    message: str = ""
    severity: str = "warning"
    source_time: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def issue_id(self) -> str:
        """Stable across restarts and source episodes; preserve the full source identity."""
        return digest([self.adapter, self.source_id])

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def daily_window(day: date, zone: str) -> tuple[float, float]:
    """Use local calendar midnights: DST days are not necessarily 86400 seconds."""
    tz = ZoneInfo(zone)
    return (
        datetime.combine(day, time.min, tz).timestamp(),
        datetime.combine(day + timedelta(days=1), time.min, tz).timestamp(),
    )


def previous_day(now: float, zone: str) -> date:
    return datetime.fromtimestamp(now, ZoneInfo(zone)).date() - timedelta(days=1)


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()
