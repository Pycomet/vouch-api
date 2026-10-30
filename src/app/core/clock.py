"""Time source and business-day boundaries.

Every timestamp the services store is UTC. Services take a :class:`Clock` so tests
can freeze and move time (:class:`FrozenClock`). Day-based rules (rental start and
end dates, cancel windows) are judged on the business calendar, whose zone comes
from ``Settings.business_timezone``; :func:`business_date` and
:func:`start_of_business_day` convert between the two.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware in UTC."""
        ...


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _zone(tz: ZoneInfo | str) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


class SystemClock:
    """The real wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A clock that stays at one instant until :meth:`advance` moves it."""

    def __init__(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at = self._at + delta


def business_date(now: datetime, tz: ZoneInfo | str) -> date:
    """The calendar date of ``now`` in the business timezone."""
    return _require_aware(now).astimezone(_zone(tz)).date()


def start_of_business_day(d: date, tz: ZoneInfo | str) -> datetime:
    """Local midnight at the start of business date ``d``, as a UTC instant."""
    return datetime.combine(d, time.min, tzinfo=_zone(tz)).astimezone(UTC)
