"""Availability blocks on the vendor's own listings (M3a 4.6): ``/vendor/assets/{id}/blocks``.

Every route needs a vendor token (401 without one, 403 for another role). The
rules live in :mod:`app.services.blocks`; these routes only translate. Another
vendor's listing, a deleted one, and a block that is not on the listing are 404,
exactly like an unknown id. Adding and removing blocks need an ``active`` vendor
profile (409 ``vendor_not_active``); reading does not. Each write commits after
the service returns.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, require_role
from app.api.examples import (
    BLOCK_DATES_INVALID,
    BLOCK_EXAMPLE,
    BLOCK_LIST_EXAMPLE,
    BLOCK_OVERLAPS_BLOCK,
    BLOCK_OVERLAPS_BOOKING,
    VENDOR_NOT_ACTIVE,
    documented,
)
from app.core.clock import Clock, business_date
from app.db.models import ActorRole
from app.db.models.asset import AvailabilityWindow
from app.db.session import get_session
from app.schemas.blocks import Block, BlockCreate, BlockList
from app.services import blocks
from app.services.audit import Actor

router = APIRouter(prefix="/vendor/assets", tags=["vendor"])

_vendor = require_role(ActorRole.vendor)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _today(request: Request, clock: Clock) -> datetime.date:
    return business_date(clock.now(), request.app.state.settings.business_zone)


def to_block(window: AvailabilityWindow) -> Block:
    return Block(
        id=window.id,
        asset_id=window.asset_id,
        starts_on=window.starts_at,
        ends_on=window.ends_at,
        created_at=window.created_at,
    )


@router.post(
    "/{asset_id}/blocks",
    response_model=Block,
    status_code=status.HTTP_201_CREATED,
    summary="Block dates on a listing",
    responses=documented(
        201,
        BLOCK_EXAMPLE,
        BLOCK_DATES_INVALID,
        401,
        403,
        404,
        VENDOR_NOT_ACTIVE,
        BLOCK_OVERLAPS_BLOCK,
        BLOCK_OVERLAPS_BOOKING,
        422,
    ),
)
async def create_block(
    asset_id: uuid.UUID,
    body: BlockCreate,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Block:
    """Block ``starts_on``..``ends_on`` (inclusive) in any listing status.

    The start may not be after the end or before today in the business zone (400).
    Overlapping another block is 409 ``dates_blocked``; overlapping a live booking
    is 409 ``dates_unavailable``."""
    window = await blocks.create_block(
        session,
        actor=actor,
        asset_id=asset_id,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        today=_today(request, clock),
        request_id=_request_id(request),
    )
    out = to_block(window)
    await session.commit()
    return out


@router.get(
    "/{asset_id}/blocks",
    response_model=BlockList,
    summary="A listing's blocks",
    responses=documented(200, BLOCK_LIST_EXAMPLE, 401, 403, 404, 422),
)
async def list_blocks(
    asset_id: uuid.UUID,
    request: Request,
    from_: datetime.date | None = Query(
        None,
        alias="from",
        description="Only blocks ending on or after this date (default: today, business zone).",
    ),
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> BlockList:
    """Blocks ordered by start date. Works whatever the vendor's status."""
    rows = await blocks.list_blocks(
        session,
        vendor_id=actor.user_id,
        asset_id=asset_id,
        from_date=from_ if from_ is not None else _today(request, clock),
    )
    return BlockList(items=[to_block(row) for row in rows])


@router.delete(
    "/{asset_id}/blocks/{block_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a block",
    responses={
        **documented(204, None, 401, 403, 404, VENDOR_NOT_ACTIVE, 422),
        # A 204 has no body, so no example.
        204: {"description": "Removed; no body."},
    },
)
async def delete_block(
    asset_id: uuid.UUID,
    block_id: uuid.UUID,
    request: Request,
    actor: Actor = Depends(_vendor),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Remove the block; the dates can be booked again."""
    await blocks.delete_block(
        session,
        actor=actor,
        asset_id=asset_id,
        block_id=block_id,
        request_id=_request_id(request),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
