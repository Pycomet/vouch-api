"""Clock and business-day boundary tests (no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import (
    Clock,
    FrozenClock,
    SystemClock,
    business_date,
    start_of_business_day,
)

# UTC+1 all year, no daylight saving: a fixed, predictable business zone for tests.
WAT = "Africa/Lagos"


def test_system_clock_returns_utc_aware_now() -> None:
    clock: Clock = SystemClock()
    before = datetime.now(UTC)
    now = clock.now()
    after = datetime.now(UTC)

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert before <= now <= after


def test_frozen_clock_holds_and_advances() -> None:
    at = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    clock: Clock = FrozenClock(at)

    assert clock.now() == at
    assert clock.now() == at
    clock.advance(timedelta(hours=49))
    assert clock.now() == datetime(2026, 11, 4, 10, 30, tzinfo=UTC)


def test_frozen_clock_normalises_to_utc() -> None:
    at = datetime(2026, 11, 2, 10, 30, tzinfo=timezone(timedelta(hours=1)))
    clock = FrozenClock(at)

    assert clock.now() == datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    assert clock.now().utcoffset() == timedelta(0)


def test_frozen_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 11, 2, 9, 30))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 22:59 UTC is 23:59 in Lagos: still the same business day.
        (datetime(2026, 11, 2, 22, 59, tzinfo=UTC), date(2026, 11, 2)),
        # 23:00 UTC is midnight in Lagos: the next business day has started.
        (datetime(2026, 11, 2, 23, 0, tzinfo=UTC), date(2026, 11, 3)),
        (datetime(2026, 11, 2, 0, 0, tzinfo=UTC), date(2026, 11, 2)),
    ],
)
def test_business_date_uses_business_timezone(now: datetime, expected: date) -> None:
    assert business_date(now, WAT) == expected
    assert business_date(now, ZoneInfo(WAT)) == expected


def test_business_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        business_date(datetime(2026, 11, 2, 12, 0), WAT)


def test_start_of_business_day_is_local_midnight_in_utc() -> None:
    start = start_of_business_day(date(2026, 11, 3), WAT)

    assert start == datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
    assert start.utcoffset() == timedelta(0)
    assert business_date(start, WAT) == date(2026, 11, 3)
    assert business_date(start - timedelta(microseconds=1), WAT) == date(2026, 11, 2)


def test_start_of_business_day_handles_daylight_saving() -> None:
    # London is UTC+1 in summer and UTC+0 in winter.
    assert start_of_business_day(date(2026, 7, 1), "Europe/London") == datetime(
        2026, 6, 30, 23, 0, tzinfo=UTC
    )
    assert start_of_business_day(date(2026, 12, 1), ZoneInfo("Europe/London")) == datetime(
        2026, 12, 1, 0, 0, tzinfo=UTC
    )
