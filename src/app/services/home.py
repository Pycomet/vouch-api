"""The renter's and the vendor's home screens (spec 3.3).

Renter (``renter_home``)
    ``active`` is the renter's live booking (``LIVE_BOOKING_STATUSES``) that starts
    soonest; ``past`` holds terminal bookings (``TERMINAL_BOOKING_STATUSES``),
    newest start first, at most :data:`PAST_LIMIT`. A ``disputed`` booking is
    neither live nor terminal and appears in neither list.

Vendor (``vendor_home``), with ``today`` the business date of the caller's clock
    * ``requests``: ``requested`` bookings, ``hold_expires_at`` ascending (spec 5).
    * ``today.handovers``: ``confirmed`` bookings starting today;
      ``today.returns``: ``in_progress`` bookings ending today.
    * ``next_payout``: the credit-normal ``vendor_payable`` balance over the
      vendor's ``completed`` bookings. Payout postings debit ``vendor_payable``,
      so the balance is already net of what has been paid out (none in M2).
    * ``utilisation_7d``: booked asset-days in live and ``completed`` bookings over
      ``[today, today + 6]``, divided by (active listings x 7), rounded half up to
      two places; ``"0.00"`` without active listings. Only bookings of the
      vendor's active listings count, so the ratio stays within 0..1.

Every card carries the booking's money line (``compute_money_line``).
"""

from __future__ import annotations

import datetime
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Asset, Booking, CatalogueItem, LedgerAccount, LedgerEntry
from app.db.models.asset import ListingStatus
from app.db.models.booking import (
    LIVE_BOOKING_STATUSES,
    TERMINAL_BOOKING_STATUSES,
    BookingStatus,
)
from app.db.models.ledger import Direction
from app.schemas.bookings import format_money
from app.schemas.home import BookingCard, RenterHome, VendorHome, VendorToday
from app.services.money_line import compute_money_line

PAST_LIMIT = 20
UTILISATION_DAYS = 7
_RATIO = Decimal("0.01")


def _cards_query() -> Select:
    return (
        select(Booking, Asset.name, CatalogueItem.make, CatalogueItem.model)
        .join(Asset, Asset.id == Booking.asset_id)
        .join(CatalogueItem, CatalogueItem.id == Asset.catalogue_item_id)
    )


async def _cards(session: AsyncSession, query: Select, settings: Settings) -> list[BookingCard]:
    cards = []
    for booking, name, make, model in (await session.execute(query)).all():
        cards.append(
            BookingCard(
                id=booking.id,
                status=booking.status.value,
                asset_id=booking.asset_id,
                asset_name=name or f"{make} {model}",
                starts_on=booking.starts_on,
                ends_on=booking.ends_on,
                hold_expires_at=booking.hold_expires_at,
                money_line=await compute_money_line(session, booking, settings=settings),
            )
        )
    return cards


async def renter_home(
    session: AsyncSession, renter_id: uuid.UUID, *, settings: Settings
) -> RenterHome:
    """The renter's next live booking and their closed bookings."""
    mine = _cards_query().where(Booking.renter_id == renter_id)
    active = await _cards(
        session,
        mine.where(Booking.status.in_(LIVE_BOOKING_STATUSES))
        .order_by(Booking.starts_on.asc(), Booking.created_at.asc(), Booking.id.asc())
        .limit(1),
        settings,
    )
    past = await _cards(
        session,
        mine.where(Booking.status.in_(TERMINAL_BOOKING_STATUSES))
        .order_by(Booking.starts_on.desc(), Booking.created_at.desc(), Booking.id.desc())
        .limit(PAST_LIMIT),
        settings,
    )
    return RenterHome(active=active[0] if active else None, past=past)


async def _next_payout(session: AsyncSession, vendor_id: uuid.UUID) -> Decimal:
    signed = case(
        (LedgerEntry.direction == Direction.cr.value, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    balance = await session.scalar(
        select(func.coalesce(func.sum(signed), 0))
        .join(Booking, Booking.id == LedgerEntry.booking_id)
        .where(
            Booking.vendor_id == vendor_id,
            Booking.status == BookingStatus.completed,
            LedgerEntry.account == LedgerAccount.vendor_payable.value,
        )
    )
    return Decimal(balance)


def _active_listing(vendor_id: uuid.UUID):
    return (
        Asset.vendor_id == vendor_id,
        Asset.listing_status == ListingStatus.active,
        Asset.deleted_at.is_(None),
    )


async def _utilisation(
    session: AsyncSession, vendor_id: uuid.UUID, today: datetime.date
) -> Decimal:
    listings = await session.scalar(
        select(func.count()).select_from(Asset).where(*_active_listing(vendor_id))
    )
    if not listings:
        return Decimal("0.00")
    last = today + datetime.timedelta(days=UTILISATION_DAYS - 1)
    # Inclusive overlap of [starts_on, ends_on] with [today, last]; date - date is days.
    days = func.least(Booking.ends_on, last) - func.greatest(Booking.starts_on, today) + 1
    booked = await session.scalar(
        select(func.coalesce(func.sum(days), 0))
        .join(Asset, Asset.id == Booking.asset_id)
        .where(
            *_active_listing(vendor_id),
            Booking.status.in_(LIVE_BOOKING_STATUSES | {BookingStatus.completed}),
            Booking.starts_on <= last,
            Booking.ends_on >= today,
        )
    )
    ratio = Decimal(booked) / (Decimal(listings) * UTILISATION_DAYS)
    return ratio.quantize(_RATIO, rounding=ROUND_HALF_UP)


async def vendor_home(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    today: datetime.date,
    settings: Settings,
) -> VendorHome:
    """The vendor's open requests, today's handovers and returns, payout and use."""
    mine = _cards_query().where(Booking.vendor_id == vendor_id)
    requests = await _cards(
        session,
        mine.where(Booking.status == BookingStatus.requested).order_by(
            Booking.hold_expires_at.asc(), Booking.id.asc()
        ),
        settings,
    )
    handovers = await _cards(
        session,
        mine.where(Booking.status == BookingStatus.confirmed, Booking.starts_on == today).order_by(
            Booking.created_at.asc(), Booking.id.asc()
        ),
        settings,
    )
    returns = await _cards(
        session,
        mine.where(Booking.status == BookingStatus.in_progress, Booking.ends_on == today).order_by(
            Booking.created_at.asc(), Booking.id.asc()
        ),
        settings,
    )
    return VendorHome(
        requests=requests,
        today=VendorToday(handovers=handovers, returns=returns),
        next_payout=format_money(await _next_payout(session, vendor_id)),
        utilisation_7d=str(await _utilisation(session, vendor_id, today)),
    )
