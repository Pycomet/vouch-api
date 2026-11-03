"""Live PostgreSQL tests for the trust-tier calculator (spec 2.3, amendment A3)."""

from __future__ import annotations

import itertools
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.booking import BookingStatus
from app.db.models.user import User
from app.services.trust import required_tier_met, tier_effective
from tests.postgres.factories import (
    create_asset,
    create_booking,
    create_kyc,
    create_user,
    create_vendor_reference,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


async def _completions(
    session: AsyncSession, renter: User, count: int, *, claimed: int = 0
) -> None:
    asset = await create_asset(session)
    for index in range(count):
        await create_booking(
            session,
            asset=asset,
            renter=renter,
            status=BookingStatus.completed,
            claimed=index < claimed,
        )


async def test_verified_with_three_clean_completions_is_tier_2(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    await _completions(db_session, renter, 3)

    assert await tier_effective(db_session, renter.id) == 2


async def test_verified_with_vendor_reference_is_tier_2(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await create_booking(
        db_session, asset=asset, renter=renter, status=BookingStatus.completed
    )
    await create_vendor_reference(db_session, booking)

    assert await tier_effective(db_session, renter.id) == 2


async def test_completion_with_claim_does_not_count(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    await _completions(db_session, renter, 3, claimed=1)

    assert await tier_effective(db_session, renter.id) == 1


async def test_verified_with_no_history_is_tier_1(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)

    assert await tier_effective(db_session, renter.id) == 1


async def test_only_completed_bookings_count(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    await _completions(db_session, renter, 2)
    asset = await create_asset(db_session)
    for status in (BookingStatus.returned, BookingStatus.cancelled_by_vendor, BookingStatus.lost):
        await create_booking(db_session, asset=asset, renter=renter, status=status)

    assert await tier_effective(db_session, renter.id) == 1


async def test_other_renters_history_does_not_count(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    other = await create_user(db_session, kyc_verified=True)
    await _completions(db_session, other, 3)

    assert await tier_effective(db_session, renter.id) == 1
    assert await tier_effective(db_session, other.id) == 2


async def test_unverified_is_tier_0_even_with_history(db_session: AsyncSession) -> None:
    renter = await create_user(db_session)
    await _completions(db_session, renter, 3)
    asset = await create_asset(db_session)
    booking = await create_booking(
        db_session, asset=asset, renter=renter, status=BookingStatus.completed
    )
    await create_vendor_reference(db_session, booking)

    assert await tier_effective(db_session, renter.id) == 0


async def test_submitted_but_unverified_kyc_is_tier_0(db_session: AsyncSession) -> None:
    renter = await create_user(db_session)
    await create_kyc(db_session, renter, verified=False)

    assert await tier_effective(db_session, renter.id) == 0


# ---------------------------------------------------------------------------
# required_tier_met (no database)
# ---------------------------------------------------------------------------


def _expected(effective: int, minimum: int, approved: bool) -> bool:
    if minimum == 3:
        return effective >= 2 and approved
    return effective >= minimum


@pytest.mark.parametrize(
    ("effective", "minimum", "approved"),
    list(itertools.product(range(3), range(4), (False, True))),
)
def test_required_tier_met(effective: int, minimum: int, approved: bool) -> None:
    assert required_tier_met(effective, minimum, approved) is _expected(
        effective, minimum, approved
    )


def test_required_tier_met_spot_checks() -> None:
    assert required_tier_met(2, 3, True) is True
    assert required_tier_met(2, 3, False) is False
    assert required_tier_met(1, 3, True) is False
    assert required_tier_met(0, 0, False) is True
    assert required_tier_met(1, 2, True) is False


@pytest.mark.parametrize(("effective", "minimum"), [(-1, 0), (3, 0), (0, -1), (0, 4)])
def test_required_tier_met_rejects_out_of_range(effective: int, minimum: int) -> None:
    with pytest.raises(ValueError):
        required_tier_met(effective, minimum, False)
