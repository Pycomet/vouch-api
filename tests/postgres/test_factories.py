"""The shared factories: deterministic default booking dates, and vendor profiles."""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VendorBusinessType, VendorProfile, VendorStatus
from app.db.models.user import RenterRole
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


async def test_a_vendor_user_gets_an_active_verified_profile(db_session: AsyncSession) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor)

    profile = await db_session.get(VendorProfile, vendor.id)
    assert profile is not None
    assert profile.status == VendorStatus.active
    assert profile.business_type == VendorBusinessType.individual
    assert profile.verified_at is not None
    assert (profile.bank_name, profile.account_last4, profile.recipient_code) == (None, None, None)


async def test_renters_and_admins_get_no_vendor_profile(db_session: AsyncSession) -> None:
    for role in (RenterRole.renter, RenterRole.admin):
        user = await create_user(db_session, role=role)
        assert await db_session.get(VendorProfile, user.id) is None


async def test_vendor_profile_fields_can_be_overridden(db_session: AsyncSession) -> None:
    pending = await create_user(
        db_session,
        role=RenterRole.vendor,
        vendor_status=VendorStatus.pending,
        vendor_profile={"bank_name": "Test Bank", "account_name": "Ade", "account_last4": "0042"},
    )
    suspended = await create_user(
        db_session, role=RenterRole.vendor, vendor_status=VendorStatus.suspended
    )
    bare = await create_user(db_session, role=RenterRole.vendor, vendor_status=None)

    profile = await db_session.get(VendorProfile, pending.id)
    assert (profile.status, profile.verified_at) == (VendorStatus.pending, None)
    assert (profile.bank_name, profile.account_name, profile.account_last4) == (
        "Test Bank",
        "Ade",
        "0042",
    )
    profile = await db_session.get(VendorProfile, suspended.id)
    assert profile.status == VendorStatus.suspended
    assert profile.suspended_at is not None and profile.suspended_reason
    assert await db_session.get(VendorProfile, bare.id) is None
