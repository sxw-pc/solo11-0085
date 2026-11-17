"""Injectable clocks and deterministic timestamp helpers."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from .errors import ConfigError
class Clock(Protocol):
    def now(self) -> datetime: """Return an aware UTC datetime."""
@dataclass(frozen=True)
class SystemClock:
    def now(self) -> datetime: return datetime.now(timezone.utc)
@dataclass(frozen=True)
class FixedClock:
    instant: datetime
    def __post_init__(self) -> None:
        value = self.instant
        if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "instant", value.astimezone(timezone.utc))
    def now(self) -> datetime: return self.instant
def parse_instant(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        text = value.strip()
        if text.endswith("Z"): text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ConfigError(f"invalid ISO-8601 clock value: {value!r}") from exc
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
def iso_z(value: datetime) -> str:
    """Canonical second-resolution UTC timestamp used in reports."""
    utc = value.astimezone(timezone.utc).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")
def timestamp_slug(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%S%fZ")
def clock_from_config(fixed_at: str | None) -> Clock:
    return FixedClock(parse_instant(fixed_at)) if fixed_at else SystemClock()
