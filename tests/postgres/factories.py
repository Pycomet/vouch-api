"""Row factories for service tests on the ``db_session`` fixture.

Each factory adds ORM rows to the given session and flushes, so ids and server
defaults are populated, and returns the row. Nothing commits: the fixture rolls
everything back. Unique columns (phones, catalogue make/model) get random suffixes,
so factories can be called any number of times in one test.

``create_booking`` fills the columns the bookings CHECK constraints require for the
chosen status (``hold_expires_at`` for requested, ``deposit_due_at`` for accepted,
``claim_window_ends_at`` for returned and disputed) and, by default, gives each
booking of the session its own dates (FIRST_START, then a week apart), so live
bookings of one asset never overlap and every test sees the same defaults. Given
only one of ``starts_on``/``ends_on``, the other is two days away.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    Asset,
    Booking,
    CatalogueItem,
    Category,
    City,
    KYCProfile,
    User,
    VendorReference,
)
from app.db.models.asset import ListingStatus
from app.db.models.booking import BookingStatus
from app.db.models.user import KYCStatus, RenterRole, TrustTier

DEFAULT_NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
FIRST_START = date(2027, 1, 4)

_SLOT_KEY = "factories.booking_slot"


def _phone() -> str:
    return "+234" + str(uuid.uuid4().int)[:10]


async def create_user(
    session: AsyncSession,
    *,
    role: RenterRole = RenterRole.renter,
    kyc_verified: bool = False,
    full_name: str | None = None,
    phone: str | None = None,
) -> User:
    """A user; with ``kyc_verified`` also a verified tier-1 KYC profile."""
    user = User(
        phone=phone or _phone(),
        full_name=full_name or f"Test {role.value}",
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    if kyc_verified:
        await create_kyc(session, user, verified=True)
    return user


async def create_kyc(session: AsyncSession, user: User, *, verified: bool = True) -> KYCProfile:
    """A KYC profile: verified (tier 1) or submitted and not yet verified (tier 0)."""
    profile = KYCProfile(
        user_id=user.id,
        provider="fake",
        status=KYCStatus.verified if verified else KYCStatus.submitted,
        tier=TrustTier.identity_verified if verified else TrustTier.phone_verified,
        verified_at=DEFAULT_NOW if verified else None,
    )
    session.add(profile)
    await session.flush()
    return profile


async def launched_city(session: AsyncSession) -> City:
    """The first city in ``Settings.launched_cities``."""
    slug = get_settings().launched_cities[0]
    return (await session.execute(select(City).where(City.slug == slug))).scalar_one()


async def create_asset(
    session: AsyncSession,
    *,
    vendor: User | None = None,
    minimum_trust_tier: int = 0,
    category_slug: str = "camera",
    band: str = "B",
    daily_rate: Decimal = Decimal("50000.00"),
    deposit_amount: Decimal = Decimal("150000.00"),
    weekend_rate: Decimal | None = None,
    weekly_rate: Decimal | None = None,
    fulfilment: dict | None = None,
    listing_status: ListingStatus = ListingStatus.active,
) -> Asset:
    """An active asset in a launched city, with its own catalogue item.

    Creates a verified vendor when ``vendor`` is not given (``asset.vendor_id``).
    """
    if vendor is None:
        vendor = await create_user(session, role=RenterRole.vendor, kyc_verified=True)
    category = (
        await session.execute(select(Category).where(Category.slug == category_slug))
    ).scalar_one()
    city = await launched_city(session)
    item = CatalogueItem(
        category_id=category.id,
        make="TestMake",
        model=f"Model {uuid.uuid4().hex[:12]}",
        replacement_value_band=band,
    )
    session.add(item)
    await session.flush()
    asset = Asset(
        vendor_id=vendor.id,
        catalogue_item_id=item.id,
        category_id=category.id,
        city_id=city.id,
        daily_rate=daily_rate,
        deposit_amount=deposit_amount,
        weekend_rate=weekend_rate,
        weekly_rate=weekly_rate,
        fulfilment=fulfilment or {"pickup": True, "delivery": False},
        listing_status=listing_status,
        minimum_trust_tier=minimum_trust_tier,
        condition_grade="good",
    )
    session.add(asset)
    await session.flush()
    return asset


def _next_dates(session: AsyncSession) -> tuple[date, date]:
    """The next default slot for this session: FIRST_START, then a week apart.

    The counter lives in ``session.info``, so it restarts with every test's session
    and default dates never depend on test order or selection.
    """
    slot = session.info.get(_SLOT_KEY, 0)
    session.info[_SLOT_KEY] = slot + 1
    start = FIRST_START + timedelta(days=7 * slot)
    return start, start + timedelta(days=2)


async def create_booking(
    session: AsyncSession,
    *,
    asset: Asset,
    renter: User,
    status: BookingStatus = BookingStatus.requested,
    starts_on: date | None = None,
    ends_on: date | None = None,
    total_rental_fee: Decimal = Decimal("150000.00"),
    deposit_amount: Decimal | None = None,
    price_snapshot: dict | None = None,
    fulfilment_choice: str = "pickup",
    now: datetime = DEFAULT_NOW,
    claimed: bool = False,
    vendor_approved: bool = False,
) -> Booking:
    """A booking of ``asset`` by ``renter`` in any status.

    ``claimed`` sets the claim columns (amount, description, ``claimed_at``).
    """
    if starts_on is None and ends_on is None:
        starts_on, ends_on = _next_dates(session)
    elif starts_on is None:
        starts_on = ends_on - timedelta(days=2)
    elif ends_on is None:
        ends_on = starts_on + timedelta(days=2)
    deposit = asset.deposit_amount if deposit_amount is None else deposit_amount
    booking = Booking(
        asset_id=asset.id,
        renter_id=renter.id,
        vendor_id=asset.vendor_id,
        starts_on=starts_on,
        ends_on=ends_on,
        status=status,
        total_rental_fee=total_rental_fee,
        deposit_amount=deposit,
        price_snapshot=price_snapshot
        or {
            "rental": str(total_rental_fee),
            "deposit": str(deposit),
            "currency": "NGN",
        },
        fulfilment_choice=fulfilment_choice,
        hold_expires_at=now + timedelta(hours=24) if status == BookingStatus.requested else None,
        deposit_due_at=now + timedelta(hours=24) if status == BookingStatus.accepted else None,
        claim_window_ends_at=(
            now + timedelta(hours=48)
            if status in {BookingStatus.returned, BookingStatus.disputed}
            else None
        ),
        vendor_approved_at=now if vendor_approved else None,
    )
    if claimed:
        booking.claim_amount = Decimal("20000.00")
        booking.claim_description = "Scratched lens element"
        booking.claimed_at = now
    session.add(booking)
    await session.flush()
    return booking


async def create_vendor_reference(session: AsyncSession, booking: Booking) -> VendorReference:
    """The booking's vendor vouches for its renter."""
    reference = VendorReference(
        renter_id=booking.renter_id,
        vendor_id=booking.vendor_id,
        booking_id=booking.id,
    )
    session.add(reference)
    await session.flush()
    return reference
