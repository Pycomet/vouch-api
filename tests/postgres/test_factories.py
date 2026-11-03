"""The shared factories give deterministic default booking dates within each test."""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.postgres.factories import FIRST_START, create_asset, create_booking, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


# Runs twice in one process: the second run follows a test that already created
# bookings, and its first default booking must still start on FIRST_START.
@pytest.mark.parametrize("run", ["first", "second"])
async def test_default_booking_dates_restart_each_test(db_session: AsyncSession, run: str) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    bookings = [await create_booking(db_session, asset=asset, renter=renter) for _ in range(3)]

    assert [b.starts_on for b in bookings] == [
        FIRST_START,
        FIRST_START + timedelta(days=7),
        FIRST_START + timedelta(days=14),
    ]
    assert [b.ends_on - b.starts_on for b in bookings] == [timedelta(days=2)] * 3


async def test_explicit_dates_never_invert(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    only_end = await create_booking(
        db_session, asset=asset, renter=renter, ends_on=date(2027, 6, 3)
    )
    only_start = await create_booking(
        db_session, asset=asset, renter=renter, starts_on=date(2027, 7, 1)
    )

    assert (only_end.starts_on, only_end.ends_on) == (date(2027, 6, 1), date(2027, 6, 3))
    assert (only_start.starts_on, only_start.ends_on) == (date(2027, 7, 1), date(2027, 7, 3))
