"""Availability blocks on the vendor's own listings (M3a 4.6; rulings S3-R4, S3-R6, S3-R7).

A block is an ``availability_windows`` row with ``is_blocked = true``; its dates are
inclusive. Rules:

* The asset must be the caller's own and not deleted (404 otherwise, exactly like
  an unknown id). A block id that is not a blocked window of that asset is 404.
* Blocks may be added in any listing status.
* Adding and removing a block are listing writes: a vendor whose profile is not
  ``active`` gets 409 ``vendor_not_active`` (amendment A23). Reading works.
* ``starts_on <= ends_on`` and ``starts_on`` no earlier than today on the business
  calendar (400 ``invalid_request``).
* A block may not overlap another block (409 ``dates_blocked``; the exclusion
  constraint ``ex_availability_windows_no_blocked_overlap`` is the backstop and is
  translated the same way) nor a live booking (409 ``dates_unavailable``).
* One audit row per write: ``availability.blocked`` / ``availability.unblocked``
  with the asset and the dates. A refusal writes nothing.

**Lock order (S3-R6).** :func:`create_block` locks the asset row
(``SELECT … FOR UPDATE``) first, then reads the vendor profile ``FOR SHARE``, then
checks blocks, then live bookings, then inserts. ``request_booking`` takes the
same asset lock before its blocks check and its insert, so a block and a request
for the same dates are serialised: whichever locks first wins, and the other sees
its committed row. No booking row is locked on this path.

Services flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AvailabilityWindow, Booking
from app.db.models.booking import LIVE_BOOKING_STATUSES
from app.schemas.errors import (
    DatesBlockedError,
    DatesUnavailableError,
    InvalidRequestError,
    ResourceNotFoundError,
)
from app.services.audit import Actor, record_audit
from app.services.listings import get_own_listing, load_for_update
from app.services.vendors import ensure_vendor_active

AUDIT_ENTITY = "availability_window"
BLOCK_OVERLAP_CONSTRAINT = "ex_availability_windows_no_blocked_overlap"


def _audit_fields(window: AvailabilityWindow) -> dict[str, object]:
    return {
        "asset_id": window.asset_id,
        "starts_on": window.starts_at,
        "ends_on": window.ends_at,
    }


async def _overlaps_block(
    session: AsyncSession, asset_id: uuid.UUID, starts_on: datetime.date, ends_on: datetime.date
) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    AvailabilityWindow.asset_id == asset_id,
                    AvailabilityWindow.is_blocked.is_(True),
                    AvailabilityWindow.starts_at <= ends_on,
                    AvailabilityWindow.ends_at >= starts_on,
                )
            )
        )
    )


async def _overlaps_live_booking(
    session: AsyncSession, asset_id: uuid.UUID, starts_on: datetime.date, ends_on: datetime.date
) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    Booking.asset_id == asset_id,
                    Booking.status.in_(LIVE_BOOKING_STATUSES),
                    Booking.starts_on <= ends_on,
                    Booking.ends_on >= starts_on,
                )
            )
        )
    )


async def list_blocks(
    session: AsyncSession, *, vendor_id: uuid.UUID, asset_id: uuid.UUID, from_date: datetime.date
) -> list[AvailabilityWindow]:
    """The listing's blocks ending on or after ``from_date``, by start date."""
    await get_own_listing(session, vendor_id, asset_id)
    rows = await session.scalars(
        select(AvailabilityWindow)
        .where(
            AvailabilityWindow.asset_id == asset_id,
            AvailabilityWindow.is_blocked.is_(True),
            AvailabilityWindow.ends_at >= from_date,
        )
        .order_by(AvailabilityWindow.starts_at, AvailabilityWindow.id)
    )
    return list(rows)


async def create_block(
    session: AsyncSession,
    *,
    actor: Actor,
    asset_id: uuid.UUID,
    starts_on: datetime.date,
    ends_on: datetime.date,
    today: datetime.date,
    request_id: str | None = None,
) -> AvailabilityWindow:
    """Block ``starts_on``..``ends_on`` (inclusive) and audit ``availability.blocked``.

    ``today`` is the business-calendar date the block may not start before.
    """
    asset = await load_for_update(session, asset_id, vendor_id=actor.user_id)
    await ensure_vendor_active(session, actor.user_id)
    if starts_on > ends_on:
        raise InvalidRequestError("The start date must not be after the end date.")
    if starts_on < today:
        raise InvalidRequestError("The start date must not be in the past.")
    if await _overlaps_block(session, asset.id, starts_on, ends_on):
        raise DatesBlockedError()
    if await _overlaps_live_booking(session, asset.id, starts_on, ends_on):
        raise DatesUnavailableError()

    window = AvailabilityWindow(
        asset_id=asset.id, starts_at=starts_on, ends_at=ends_on, is_blocked=True
    )
    try:
        async with session.begin_nested():
            session.add(window)
            await session.flush()
    except IntegrityError as exc:
        if BLOCK_OVERLAP_CONSTRAINT in str(exc.orig):
            raise DatesBlockedError() from exc
        raise
    await session.refresh(window)

    await record_audit(
        session,
        actor=actor,
        entity=AUDIT_ENTITY,
        entity_id=window.id,
        action="availability.blocked",
        before=None,
        after=_audit_fields(window),
        request_id=request_id,
    )
    return window


async def delete_block(
    session: AsyncSession,
    *,
    actor: Actor,
    asset_id: uuid.UUID,
    block_id: uuid.UUID,
    request_id: str | None = None,
) -> None:
    """Remove the block and audit ``availability.unblocked``.

    The asset row is locked first, the same order as :func:`create_block`.
    """
    asset = await load_for_update(session, asset_id, vendor_id=actor.user_id)
    await ensure_vendor_active(session, actor.user_id)
    window = await session.scalar(
        select(AvailabilityWindow).where(
            AvailabilityWindow.id == block_id,
            AvailabilityWindow.asset_id == asset.id,
            AvailabilityWindow.is_blocked.is_(True),
        )
    )
    if window is None:
        raise ResourceNotFoundError()
    before = _audit_fields(window)
    await session.delete(window)
    await session.flush()
    await record_audit(
        session,
        actor=actor,
        entity=AUDIT_ENTITY,
        entity_id=block_id,
        action="availability.unblocked",
        before=before,
        after=None,
        request_id=request_id,
    )
