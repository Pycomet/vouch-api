"""The one home of the listing rules (M3a 4.4, 4.5; ruling S3-R3).

State machine (``ListingEvent`` → :data:`TRANSITIONS`)::

    draft ──submit──► review ──approve (admin)──► active ◄──resume──┐
      ▲                 │                          │  ▲             │
      │                 └──reject (admin)──► rejected │  └──pause──► paused
      └─────────────────── (edit, then submit) ─────┘

:func:`next_status` is the only place the table is read; an event the table has
no row for is 409 ``invalid_transition`` with details ``{from, event}``. The
vendor's own actions are here (create, update, submit, pause, resume, delete);
Step 4's admin approve, reject, pause and resume reuse :func:`next_status`,
:func:`load_for_update` and :func:`record_listing_audit`.

Edit rules (:func:`update_listing`):

* ``review`` is locked: any vendor edit is 409 ``listing_locked``.
* Identity fields (:data:`IDENTITY_FIELDS`) change only in ``draft`` or
  ``rejected`` (else 409 ``listing_locked``, ``details.fields`` naming them).
* Commercial fields (:data:`COMMERCIAL_FIELDS`) change in ``draft``, ``rejected``,
  ``active`` and ``paused``. A booking froze its ``price_snapshot`` when it was
  requested, so a change never touches an existing booking.
* ``minimum_trust_tier`` is not vendor-editable after create (the update schema
  has no such field, so FastAPI answers 422).
* A new catalogue item re-derives ``category_id`` and re-applies the tier floor:
  a tier below the new item's band default is raised to it.
* An ``active`` or ``paused`` listing must stay complete (:func:`missing_for_review`):
  an edit that would leave something missing is 409 ``listing_incomplete`` and
  writes nothing. A ``draft`` or ``rejected`` listing may stay incomplete.
* One ``asset.updated`` audit row with the before and after of the changed fields
  only; no row when nothing changed.

Every vendor write needs an ``active`` vendor profile (409 ``vendor_not_active``);
reads do not. Another vendor's listing, and a soft-deleted one, are 404, exactly
like an unknown id. Every write records one audit row (actor and request id).

Body problems the schema cannot see (an unknown catalogue item, a trust tier
below the band default) raise :class:`~app.schemas.errors.BodyFieldError`, which
the app answers with FastAPI's own 422 body, so every 422 has the same shape.

Services flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Select, and_, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Asset, Booking, CatalogueItem, Category, City
from app.db.models.asset import ListingStatus, PausedBy
from app.db.models.booking import LIVE_BOOKING_STATUSES, BookingStatus
from app.db.models.catalogue import BAND_DEFAULT_TIER, ValueBand
from app.schemas.errors import (
    LISTING_TRANSITION_MESSAGE,
    BodyFieldError,
    CatalogueItemInactiveError,
    CityNotLaunchedError,
    DuplicateSerialError,
    HasLiveBookingsError,
    InvalidTransition,
    ListingIncompleteError,
    ListingLockedError,
    ResourceNotFoundError,
)
from app.schemas.listings import FulfilmentIn, OwnAssetCreate, OwnAssetUpdate
from app.schemas.pagination import MAX_PAGE_LIMIT, decode_cursor, encode_cursor
from app.services.audit import Actor, record_audit
from app.services.inventory import ensure_city_launched
from app.services.vendors import ensure_vendor_active

AUDIT_ENTITY = "asset"
SERIAL_INDEX = "uq_assets_vendor_item_serial"

IDENTITY_FIELDS = frozenset({"catalogue_item_id", "serial_number", "city"})
COMMERCIAL_FIELDS = frozenset(
    {
        "daily_rate",
        "weekend_rate",
        "weekly_rate",
        "deposit_amount",
        "included_accessories",
        "fulfilment",
        "description",
        "condition_notes",
        "condition_grade",
    }
)
IDENTITY_EDITABLE = frozenset({ListingStatus.draft, ListingStatus.rejected})
COMMERCIAL_EDITABLE = frozenset(
    {ListingStatus.draft, ListingStatus.rejected, ListingStatus.active, ListingStatus.paused}
)
# A listing in one of these is public (or may be again) and must stay complete.
LIVE_LISTING_STATUSES = frozenset({ListingStatus.active, ListingStatus.paused})
# A listing with a booking in one of these cannot be deleted.
UNDELETABLE_BOOKING_STATUSES = frozenset(LIVE_BOOKING_STATUSES | {BookingStatus.disputed})


class ListingEvent(StrEnum):
    submit = "submit"
    approve = "approve"
    reject = "reject"
    pause = "pause"
    resume = "resume"


TRANSITIONS: Mapping[tuple[ListingStatus, ListingEvent], ListingStatus] = {
    (ListingStatus.draft, ListingEvent.submit): ListingStatus.review,
    (ListingStatus.rejected, ListingEvent.submit): ListingStatus.review,
    (ListingStatus.review, ListingEvent.approve): ListingStatus.active,
    (ListingStatus.review, ListingEvent.reject): ListingStatus.rejected,
    (ListingStatus.active, ListingEvent.pause): ListingStatus.paused,
    (ListingStatus.paused, ListingEvent.resume): ListingStatus.active,
}


def next_status(status: ListingStatus, event: ListingEvent) -> ListingStatus:
    """The status ``event`` leads to from ``status``; 409 ``invalid_transition`` if none."""
    target = TRANSITIONS.get((status, event))
    if target is None:
        raise listing_transition_error(status, event)
    return target


def listing_transition_error(status: ListingStatus, event: ListingEvent) -> InvalidTransition:
    return InvalidTransition(status.value, event.value, message=LISTING_TRANSITION_MESSAGE)


def band_default_tier(item: CatalogueItem) -> int:
    """The default ``minimum_trust_tier`` for a listing of ``item`` (amendment A3)."""
    return BAND_DEFAULT_TIER[ValueBand(item.replacement_value_band)]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnAssetRow:
    """One of the vendor's listings with its slugs and catalogue names."""

    asset: Asset
    city_slug: str
    category_slug: str
    make: str
    model: str


@dataclass
class OwnAssetPage:
    items: list[OwnAssetRow]
    next_cursor: str | None


def _own_query(vendor_id: uuid.UUID) -> Select:
    return (
        select(Asset, City.slug, Category.slug, CatalogueItem.make, CatalogueItem.model)
        .join(City, City.id == Asset.city_id)
        .join(Category, Category.id == Asset.category_id)
        .join(CatalogueItem, CatalogueItem.id == Asset.catalogue_item_id)
        .where(Asset.vendor_id == vendor_id, Asset.deleted_at.is_(None))
        .execution_options(populate_existing=True)
    )


def _row(result_row) -> OwnAssetRow:
    asset, city_slug, category_slug, make, model = result_row
    return OwnAssetRow(asset, city_slug, category_slug, make, model)


async def get_own_listing(
    session: AsyncSession, vendor_id: uuid.UUID, asset_id: uuid.UUID
) -> OwnAssetRow:
    """The vendor's own, not deleted listing; 404 otherwise (another vendor's included)."""
    row = (await session.execute(_own_query(vendor_id).where(Asset.id == asset_id))).one_or_none()
    if row is None:
        raise ResourceNotFoundError()
    return _row(row)


async def list_own_listings(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    *,
    status: ListingStatus | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> OwnAssetPage:
    """The vendor's listings (not deleted), newest first: ``(created_at, id)`` descending."""
    limit = min(limit, MAX_PAGE_LIMIT)
    stmt = _own_query(vendor_id)
    if status is not None:
        stmt = stmt.where(Asset.listing_status == status)
    if cursor is not None:
        cursor_dt, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            Asset.created_at <= cursor_dt,
            ~and_(Asset.created_at == cursor_dt, Asset.id >= cursor_id),
        )
    stmt = stmt.order_by(Asset.created_at.desc(), Asset.id.desc()).limit(limit + 1)
    rows = [_row(row) for row in (await session.execute(stmt)).all()]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1].asset
        next_cursor = encode_cursor(last.created_at.isoformat(), str(last.id))
    return OwnAssetPage(items=rows, next_cursor=next_cursor)


async def load_for_update(
    session: AsyncSession, asset_id: uuid.UUID, *, vendor_id: uuid.UUID | None = None
) -> Asset:
    """Lock the (not deleted) asset row; 404 if absent or, with ``vendor_id``, not theirs.

    The asset row is the first lock every listing write takes (S3-R6 order).
    """
    stmt = (
        select(Asset)
        .where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if vendor_id is not None:
        stmt = stmt.where(Asset.vendor_id == vendor_id)
    asset = await session.scalar(stmt)
    if asset is None:
        raise ResourceNotFoundError()
    return asset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _body_error(field: str, error_type: str, message: str, value: Any) -> BodyFieldError:
    """A 422 for one body field (the app renders it in FastAPI's validation shape)."""
    return BodyFieldError(field, error_type, message, value)


async def _catalogue_item(session: AsyncSession, item_id: uuid.UUID) -> CatalogueItem:
    """The active catalogue item: 422 when unknown, 409 when inactive."""
    item = await session.get(CatalogueItem, item_id, populate_existing=True)
    if item is None:
        raise _body_error(
            "catalogue_item_id", "unknown_catalogue_item", "No such catalogue item.", str(item_id)
        )
    if not item.is_active:
        raise CatalogueItemInactiveError(details={"catalogue_item_id": str(item_id)})
    return item


async def _launched_city(session: AsyncSession, slug: str, settings: Settings) -> City:
    """The launched, active city with ``slug``; 400 ``city_not_launched`` otherwise."""
    ensure_city_launched(slug, settings.launched_cities)
    city = await session.scalar(select(City).where(City.slug == slug, City.is_active.is_(True)))
    if city is None:
        raise CityNotLaunchedError(slug)
    return city


@asynccontextmanager
async def _listing_savepoint(session: AsyncSession, asset: Asset) -> AsyncIterator[None]:
    """Change ``asset`` inside this block; it is flushed only inside a savepoint.

    ``begin_nested()`` flushes whatever is already dirty *before* it emits the
    SAVEPOINT, so the listing must not be changed before the block opens: run
    every lookup first. Inside, autoflush is off, the block's changes are flushed
    at its end within the savepoint, and the serial index becomes 409
    ``duplicate_serial``. A failure (or an error the block raises) rolls back to
    the savepoint only, so the caller's transaction stays usable.
    """
    try:
        async with session.begin_nested():
            with session.no_autoflush:
                yield
            session.add(asset)
            await session.flush()
    except IntegrityError as exc:
        if SERIAL_INDEX in str(exc.orig):
            raise DuplicateSerialError() from exc
        raise
    await session.refresh(asset)


async def record_listing_audit(
    session: AsyncSession,
    asset: Asset,
    *,
    actor: Actor,
    action: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
    """One ``asset.*`` audit row for ``asset``."""
    await record_audit(
        session,
        actor=actor,
        entity=AUDIT_ENTITY,
        entity_id=asset.id,
        action=action,
        before=before,
        after=after,
        reason=reason,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def create_listing(
    session: AsyncSession,
    *,
    actor: Actor,
    body: OwnAssetCreate,
    settings: Settings,
    request_id: str | None = None,
) -> Asset:
    """Create the vendor's listing as ``draft`` and audit ``asset.created``."""
    vendor_id = actor.user_id
    await ensure_vendor_active(session, vendor_id)
    item = await _catalogue_item(session, body.catalogue_item_id)
    city = await _launched_city(session, body.city, settings)
    floor = band_default_tier(item)
    tier = floor if body.minimum_trust_tier is None else body.minimum_trust_tier
    if tier < floor:
        raise _body_error(
            "minimum_trust_tier",
            "below_band_default",
            f"must be at least {floor}, the default for this item's band",
            tier,
        )

    asset = Asset(
        vendor_id=vendor_id,
        catalogue_item_id=item.id,
        category_id=item.category_id,
        city_id=city.id,
        serial_number=body.serial_number,
        condition_grade=body.condition_grade,
        daily_rate=body.daily_rate,
        deposit_amount=body.deposit_amount,
        weekend_rate=body.weekend_rate,
        weekly_rate=body.weekly_rate,
        description=body.description,
        condition_notes=body.condition_notes,
        included_accessories=list(body.included_accessories),
        fulfilment=body.fulfilment.stored(),
        listing_status=ListingStatus.draft,
        minimum_trust_tier=tier,
    )
    # Not yet in the session, so nothing flushes before the savepoint.
    async with _listing_savepoint(session, asset):
        pass
    await record_listing_audit(
        session,
        asset,
        actor=actor,
        action="asset.created",
        before=None,
        after={
            "listing_status": asset.listing_status,
            "catalogue_item_id": asset.catalogue_item_id,
            "category_id": asset.category_id,
            "city": city.slug,
            "serial_number": asset.serial_number,
            "condition_grade": asset.condition_grade,
            "daily_rate": asset.daily_rate,
            "weekend_rate": asset.weekend_rate,
            "weekly_rate": asset.weekly_rate,
            "deposit_amount": asset.deposit_amount,
            "fulfilment": asset.fulfilment,
            "minimum_trust_tier": asset.minimum_trust_tier,
        },
        request_id=request_id,
    )
    return asset


def _current(asset: Asset, field: str, city_slug: str) -> Any:
    if field == "city":
        return city_slug
    return getattr(asset, field)


def _incoming(field: str, value: Any) -> Any:
    """The stored form of a PATCH value (fulfilment as its JSON, lists copied)."""
    if isinstance(value, FulfilmentIn):
        return value.stored()
    if isinstance(value, list):
        return list(value)
    return value


def _same(field: str, current: Any, new: Any) -> bool:
    if field == "fulfilment":
        return _normalise_fulfilment(current) == _normalise_fulfilment(new)
    return current == new


def _normalise_fulfilment(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    fee = data.get("delivery_fee")
    if fee is not None:
        data["delivery_fee"] = Decimal(str(fee))
    return data


async def update_listing(
    session: AsyncSession,
    *,
    actor: Actor,
    asset_id: uuid.UUID,
    body: OwnAssetUpdate,
    settings: Settings,
    request_id: str | None = None,
) -> Asset:
    """Apply a partial update under the edit rules; audit ``asset.updated`` if anything changed."""
    asset = await load_for_update(session, asset_id, vendor_id=actor.user_id)
    await ensure_vendor_active(session, actor.user_id)
    status = asset.listing_status
    if status == ListingStatus.review:
        raise ListingLockedError(details={"status": status.value})

    city_slug = await session.scalar(select(City.slug).where(City.id == asset.city_id))
    changes = {
        field: _incoming(field, value)
        for field, value in body.changes().items()
        if not _same(field, _current(asset, field, city_slug), _incoming(field, value))
    }
    locked = sorted(
        field
        for field in changes
        if (field in IDENTITY_FIELDS and status not in IDENTITY_EDITABLE)
        or (field in COMMERCIAL_FIELDS and status not in COMMERCIAL_EDITABLE)
    )
    if locked:
        raise ListingLockedError(details={"status": status.value, "fields": locked})
    if not changes:
        return asset

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    def track(field: str, old: Any, new: Any) -> None:
        before[field] = old
        after[field] = new

    # Every lookup runs before any attribute changes: a query after a change would
    # autoflush it outside the savepoint, where a duplicate serial escapes as a 500.
    item = None
    if "catalogue_item_id" in changes:
        item = await _catalogue_item(session, changes["catalogue_item_id"])
    city = None
    if "city" in changes:
        city = await _launched_city(session, changes["city"], settings)

    # Every change happens inside the savepoint (no query runs in it, and
    # autoflush is off there anyway); an error rolls back to the savepoint only.
    async with _listing_savepoint(session, asset):
        if item is not None:
            track("catalogue_item_id", asset.catalogue_item_id, item.id)
            asset.catalogue_item_id = item.id
            if asset.category_id != item.category_id:
                track("category_id", asset.category_id, item.category_id)
                asset.category_id = item.category_id
            floor = band_default_tier(item)
            if asset.minimum_trust_tier < floor:
                track("minimum_trust_tier", asset.minimum_trust_tier, floor)
                asset.minimum_trust_tier = floor
        if city is not None:
            track("city", city_slug, city.slug)
            asset.city_id = city.id
        for field, value in changes.items():
            if field in ("catalogue_item_id", "city"):
                continue
            track(field, getattr(asset, field), value)
            setattr(asset, field, value)

        # A live listing stays complete: an edit may not, say, offer delivery
        # without a fee (new bookings would price it at zero). Drafts may stay
        # incomplete.
        if status in LIVE_LISTING_STATUSES:
            missing = missing_for_review(asset)
            if missing:
                raise ListingIncompleteError(missing)

    await record_listing_audit(
        session,
        asset,
        actor=actor,
        action="asset.updated",
        before=before,
        after=after,
        request_id=request_id,
    )
    return asset


def missing_for_review(asset: Asset) -> list[str]:
    """What a listing still needs before review (empty when complete)."""
    fulfilment = asset.fulfilment or {}
    missing = []
    if not asset.serial_number:
        missing.append("serial_number")
    if asset.condition_grade is None:
        missing.append("condition_grade")
    if asset.daily_rate is None or asset.daily_rate <= 0:
        missing.append("daily_rate")
    if asset.deposit_amount is None or asset.deposit_amount < 0:
        missing.append("deposit_amount")
    if not (fulfilment.get("pickup") or fulfilment.get("delivery")):
        missing.append("fulfilment")
    if fulfilment.get("delivery") and fulfilment.get("delivery_fee") is None:
        missing.append("delivery_fee")
    return missing


async def _vendor_transition(
    session: AsyncSession,
    *,
    actor: Actor,
    asset_id: uuid.UUID,
    event: ListingEvent,
    request_id: str | None,
) -> Asset:
    asset = await load_for_update(session, asset_id, vendor_id=actor.user_id)
    await ensure_vendor_active(session, actor.user_id)
    from_status = asset.listing_status
    from_paused_by = asset.paused_by
    target = next_status(from_status, event)

    # Guards and lookups first; the listing changes only inside the savepoint.
    paused_by = from_paused_by
    if event == ListingEvent.submit:
        missing = missing_for_review(asset)
        if missing:
            raise ListingIncompleteError(missing)
        item = await session.get(CatalogueItem, asset.catalogue_item_id, populate_existing=True)
        if not item.is_active:
            raise CatalogueItemInactiveError(
                details={"catalogue_item_id": str(asset.catalogue_item_id)}
            )
    elif event == ListingEvent.pause:
        paused_by = PausedBy.vendor.value
    elif event == ListingEvent.resume:
        # A vendor resumes only their own pause; an admin pause is the admin's to lift.
        if from_paused_by != PausedBy.vendor.value:
            raise listing_transition_error(from_status, event)
        paused_by = None

    async with _listing_savepoint(session, asset):
        asset.paused_by = paused_by
        asset.listing_status = target
    action = {
        ListingEvent.submit: "asset.submitted",
        ListingEvent.pause: "asset.paused",
        ListingEvent.resume: "asset.resumed",
    }[event]
    await record_listing_audit(
        session,
        asset,
        actor=actor,
        action=action,
        before={"listing_status": from_status, "paused_by": from_paused_by},
        after={"listing_status": asset.listing_status, "paused_by": asset.paused_by},
        request_id=request_id,
    )
    return asset


async def submit_listing(
    session: AsyncSession, *, actor: Actor, asset_id: uuid.UUID, request_id: str | None = None
) -> Asset:
    """``draft``/``rejected`` → ``review``; 409 ``listing_incomplete`` if anything is missing."""
    return await _vendor_transition(
        session, actor=actor, asset_id=asset_id, event=ListingEvent.submit, request_id=request_id
    )


async def pause_listing(
    session: AsyncSession, *, actor: Actor, asset_id: uuid.UUID, request_id: str | None = None
) -> Asset:
    """``active`` → ``paused`` with ``paused_by='vendor'``. Live bookings continue."""
    return await _vendor_transition(
        session, actor=actor, asset_id=asset_id, event=ListingEvent.pause, request_id=request_id
    )


async def resume_listing(
    session: AsyncSession, *, actor: Actor, asset_id: uuid.UUID, request_id: str | None = None
) -> Asset:
    """``paused`` → ``active``, only when the vendor paused it (else ``invalid_transition``)."""
    return await _vendor_transition(
        session, actor=actor, asset_id=asset_id, event=ListingEvent.resume, request_id=request_id
    )


async def delete_listing(
    session: AsyncSession,
    *,
    actor: Actor,
    asset_id: uuid.UUID,
    now: datetime.datetime,
    request_id: str | None = None,
) -> None:
    """Soft-delete the listing; 409 ``has_live_bookings`` while a booking holds it.

    The asset row is locked first, so a booking request for it waits (S3-R6).
    """
    asset = await load_for_update(session, asset_id, vendor_id=actor.user_id)
    await ensure_vendor_active(session, actor.user_id)
    busy = await session.scalar(
        select(
            exists().where(
                Booking.asset_id == asset.id,
                Booking.status.in_(UNDELETABLE_BOOKING_STATUSES),
            )
        )
    )
    if busy:
        raise HasLiveBookingsError()
    async with _listing_savepoint(session, asset):
        asset.deleted_at = now
    await record_listing_audit(
        session,
        asset,
        actor=actor,
        action="asset.deleted",
        before={"deleted_at": None, "listing_status": asset.listing_status},
        after={"deleted_at": now, "listing_status": asset.listing_status},
        request_id=request_id,
    )
