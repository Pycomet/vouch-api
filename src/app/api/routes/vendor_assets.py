"""The vendor's own listings (M3a 4.4): ``/vendor/assets``.

Every route needs a vendor token (401 without one, 403 for another role). The
rules live in :mod:`app.services.listings`; these routes only translate. Another
vendor's listing, or a deleted one, is 404 everywhere, exactly like an unknown id.
Reads work for a vendor whose profile is not active; every write is then 409
``vendor_not_active``. Each write commits after the service returns.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, require_role
from app.api.examples import (
    ACTIVE_ASSET_EXAMPLE,
    CATALOGUE_ITEM_INACTIVE,
    CITY_NOT_LAUNCHED,
    DUPLICATE_SERIAL,
    HAS_LIVE_BOOKINGS,
    IDENTITY_LOCKED,
    INVALID_CURSOR,
    LISTING_INCOMPLETE,
    LISTING_LOCKED,
    OWN_ASSET_EXAMPLE,
    OWN_ASSET_PAGE_EXAMPLE,
    PAUSED_ASSET_EXAMPLE,
    REVIEW_ASSET_EXAMPLE,
    VENDOR_NOT_ACTIVE,
    documented,
    listing_transition,
)
from app.core.clock import Clock
from app.db.models import ActorRole
from app.db.models.asset import Asset, ListingStatus
from app.db.session import get_session
from app.schemas.assets import DEFAULT_LIMIT, MAX_LIMIT
from app.schemas.bookings import format_money
from app.schemas.listings import (
    FulfilmentOut,
    ListingStatusName,
    OwnAsset,
    OwnAssetCreate,
    OwnAssetUpdate,
)
from app.schemas.pagination import Page, PageMeta, decode_cursor
from app.services import listings
from app.services.audit import Actor

router = APIRouter(prefix="/vendor/assets", tags=["vendor"])

_vendor = require_role(ActorRole.vendor)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _money(value) -> str | None:
    return None if value is None else format_money(value)


def to_own_asset(row: listings.OwnAssetRow) -> OwnAsset:
    asset = row.asset
    fulfilment = asset.fulfilment or {}
    fee = fulfilment.get("delivery_fee")
    return OwnAsset(
        id=asset.id,
        catalogue_item_id=asset.catalogue_item_id,
        make=row.make,
        model=row.model,
        category=row.category_slug,
        city=row.city_slug,
        serial_number=asset.serial_number,
        condition_grade=asset.condition_grade,
        condition_notes=asset.condition_notes,
        description=asset.description,
        daily_rate=format_money(asset.daily_rate),
        weekend_rate=_money(asset.weekend_rate),
        weekly_rate=_money(asset.weekly_rate),
        deposit_amount=format_money(asset.deposit_amount),
        included_accessories=list(asset.included_accessories or []),
        fulfilment=FulfilmentOut(
            pickup=bool(fulfilment.get("pickup")),
            delivery=bool(fulfilment.get("delivery")),
            delivery_fee=None if fee is None else format_money(Decimal(str(fee))),
        ),
        listing_status=asset.listing_status.value,
        minimum_trust_tier=asset.minimum_trust_tier,
        paused_by=asset.paused_by,
        review_reason=asset.review_reason,
        reviewed_at=asset.reviewed_at,
        is_available=asset.is_available,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


async def _committed(session: AsyncSession, actor: Actor, asset: Asset) -> OwnAsset:
    asset_id = asset.id
    await session.commit()
    return to_own_asset(await listings.get_own_listing(session, actor.user_id, asset_id))


_ON_LISTING = (401, 403, 404, VENDOR_NOT_ACTIVE)


@router.post(
    "",
    response_model=OwnAsset,
    status_code=status.HTTP_201_CREATED,
    summary="Create a listing (draft)",
    responses=documented(
        201,
        OWN_ASSET_EXAMPLE,
        CITY_NOT_LAUNCHED,
        401,
        403,
        VENDOR_NOT_ACTIVE,
        CATALOGUE_ITEM_INACTIVE,
        DUPLICATE_SERIAL,
        422,
    ),
)
async def create_asset(
    body: OwnAssetCreate,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> OwnAsset:
    """A new listing as ``draft``. The category comes from the catalogue item.

    ``minimum_trust_tier`` defaults from the item's band and may only be raised. An
    unknown catalogue item, or a tier below the band default, is 422."""
    asset = await listings.create_listing(
        session,
        actor=actor,
        body=body,
        settings=request.app.state.settings,
        request_id=_request_id(request),
    )
    return await _committed(session, actor, asset)


@router.get(
    "",
    response_model=Page[OwnAsset],
    summary="The vendor's own listings",
    responses=documented(200, OWN_ASSET_PAGE_EXAMPLE, INVALID_CURSOR, 401, 403, 422),
)
async def list_assets(
    status_: ListingStatusName | None = Query(None, alias="status", description="Listing status."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page (≤100)."),
    cursor: str | None = Query(None, description="Opaque pagination cursor from a prior response."),
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> Page[OwnAsset]:
    """Not deleted listings, newest first. Works whatever the vendor's status."""
    if cursor is not None:
        decode_cursor(cursor)  # CursorDecodeError (400) before any query
    page = await listings.list_own_listings(
        session,
        actor.user_id,
        status=None if status_ is None else ListingStatus(status_),
        limit=limit,
        cursor=cursor,
    )
    return Page[OwnAsset](
        items=[to_own_asset(row) for row in page.items],
        pagination=PageMeta(limit=limit, next_cursor=page.next_cursor),
    )


@router.get(
    "/{asset_id}",
    response_model=OwnAsset,
    summary="One of the vendor's own listings",
    responses=documented(200, OWN_ASSET_EXAMPLE, 401, 403, 404, 422),
)
async def get_asset(
    asset_id: uuid.UUID,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> OwnAsset:
    """The full listing: serial number, condition notes, review reason, ``paused_by``."""
    return to_own_asset(await listings.get_own_listing(session, actor.user_id, asset_id))


@router.patch(
    "/{asset_id}",
    response_model=OwnAsset,
    summary="Edit a listing",
    responses=documented(
        200,
        OWN_ASSET_EXAMPLE,
        CITY_NOT_LAUNCHED,
        *_ON_LISTING,
        LISTING_LOCKED,
        IDENTITY_LOCKED,
        CATALOGUE_ITEM_INACTIVE,
        DUPLICATE_SERIAL,
        422,
    ),
)
async def update_asset(
    asset_id: uuid.UUID,
    body: OwnAssetUpdate,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> OwnAsset:
    """Only the fields given change. Locked in ``review``; identity fields
    (``catalogue_item_id``, ``serial_number``, ``city``) only in ``draft`` or
    ``rejected``. ``minimum_trust_tier`` cannot be sent (422)."""
    asset = await listings.update_listing(
        session,
        actor=actor,
        asset_id=asset_id,
        body=body,
        settings=request.app.state.settings,
        request_id=_request_id(request),
    )
    return await _committed(session, actor, asset)


@router.post(
    "/{asset_id}/submit",
    response_model=OwnAsset,
    summary="Submit a listing for review",
    responses=documented(
        200,
        REVIEW_ASSET_EXAMPLE,
        *_ON_LISTING,
        listing_transition("active", "submit", "Only a draft or rejected listing is submitted."),
        LISTING_INCOMPLETE,
        CATALOGUE_ITEM_INACTIVE,
        422,
    ),
)
async def submit_asset(
    asset_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> OwnAsset:
    """``draft`` or ``rejected`` → ``review``, once the listing is complete."""
    asset = await listings.submit_listing(
        session, actor=actor, asset_id=asset_id, request_id=_request_id(request)
    )
    return await _committed(session, actor, asset)


@router.post(
    "/{asset_id}/pause",
    response_model=OwnAsset,
    summary="Pause a listing",
    responses=documented(
        200,
        PAUSED_ASSET_EXAMPLE,
        *_ON_LISTING,
        listing_transition("draft", "pause", "Only an active listing is paused."),
        422,
    ),
)
async def pause_asset(
    asset_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> OwnAsset:
    """``active`` → ``paused``. It leaves search; live bookings continue."""
    asset = await listings.pause_listing(
        session, actor=actor, asset_id=asset_id, request_id=_request_id(request)
    )
    return await _committed(session, actor, asset)


@router.post(
    "/{asset_id}/resume",
    response_model=OwnAsset,
    summary="Resume a paused listing",
    responses=documented(
        200,
        ACTIVE_ASSET_EXAMPLE,
        *_ON_LISTING,
        listing_transition(
            "paused", "resume", "Not paused, or paused by an admin (only an admin resumes it)."
        ),
        422,
    ),
)
async def resume_asset(
    asset_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> OwnAsset:
    """``paused`` → ``active``, only if the vendor paused it."""
    asset = await listings.resume_listing(
        session, actor=actor, asset_id=asset_id, request_id=_request_id(request)
    )
    return await _committed(session, actor, asset)


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a listing",
    responses={
        **documented(204, None, *_ON_LISTING, HAS_LIVE_BOOKINGS, 422),
        # A 204 has no body, so no example.
        204: {"description": "Deleted (soft delete); no body."},
    },
)
async def delete_asset(
    asset_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Soft delete. Refused while a booking on it is live or disputed."""
    await listings.delete_listing(
        session,
        actor=actor,
        asset_id=asset_id,
        now=clock.now(),
        request_id=_request_id(request),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
