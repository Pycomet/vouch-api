"""Public inventory endpoints — read-only, no authentication required.

These routes expose active creative-gear listings in the launched cities to
unauthenticated callers.  All visibility filtering (listing status, vendor
state, city gate) is enforced inside ``app.services.inventory``.

Restricted fields are excluded at the schema layer (``PublicAsset``); the
serialisation layer never returns serial numbers, vendor identity, condition
notes, or review metadata.

Availability contract (see also ``app.schemas.assets`` module docstring)
------------------------------------------------------------------------
When ``starts_on`` and ``ends_on`` are both supplied they must satisfy:
  * ``starts_on <= ends_on``
  * Both are required together; neither alone triggers date filtering.
If either constraint is violated the endpoint returns 400.

City gate
---------
Launched cities come from ``Settings.launched_cities`` via the
``get_launched_cities`` dependency.  A ``city`` filter outside that set returns
400 ``city_not_launched`` (unknown cities included); without a ``city`` filter,
results span every launched city.  The detail of an asset in a city that is not
launched is a 404.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_launched_cities
from app.db.session import get_session
from app.schemas.assets import DEFAULT_LIMIT, MAX_LIMIT, PublicAsset
from app.schemas.errors import InvalidRequestError
from app.schemas.pagination import Page, PageMeta, decode_cursor
from app.services.inventory import get_public_asset, search_public_assets

router = APIRouter(prefix="/assets", tags=["inventory"])


@router.get("", response_model=Page[PublicAsset], summary="Search public inventory")
async def list_assets(
    category: str | None = Query(None, description="Filter by category slug."),
    starts_on: datetime.date | None = Query(
        None,
        description="Start date for availability check (inclusive, YYYY-MM-DD).  "
        "Required together with ends_on.",
    ),
    ends_on: datetime.date | None = Query(
        None,
        description="End date for availability check (inclusive, YYYY-MM-DD).  "
        "Required together with starts_on.",
    ),
    min_daily_rate: Decimal | None = Query(
        None, description="Minimum daily rate in NGN (inclusive)."
    ),
    max_daily_rate: Decimal | None = Query(
        None, description="Maximum daily rate in NGN (inclusive)."
    ),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page (≤100)."),
    cursor: str | None = Query(None, description="Opaque pagination cursor from a prior response."),
    city: str | None = Query(
        None,
        description="City slug filter.  Must be a launched city; omit it to search them all.",
    ),
    session: AsyncSession = Depends(get_session),
    launched_cities: list[str] = Depends(get_launched_cities),
) -> Page[PublicAsset]:
    """Return a paginated list of publicly visible creative-gear listings.

    Filters are optional and additive.  Date filtering requires both
    ``starts_on`` and ``ends_on``; supplying only one returns 400.
    """
    # --- Validate date pair ---
    if (starts_on is None) != (ends_on is None):
        raise InvalidRequestError(
            message="Both 'starts_on' and 'ends_on' are required together for date filtering.",
            details={"starts_on": str(starts_on), "ends_on": str(ends_on)},
        )
    if starts_on is not None and ends_on is not None and starts_on > ends_on:
        raise InvalidRequestError(
            message="'starts_on' must not be after 'ends_on'.",
            details={"starts_on": str(starts_on), "ends_on": str(ends_on)},
        )

    # --- Validate cursor early so a bad cursor returns 400 before DB access ---
    if cursor is not None:
        decode_cursor(cursor)  # raises CursorDecodeError (→ 400) on failure

    # The service raises CityNotLaunchedError (→ 400) before querying.
    page = await search_public_assets(
        session,
        launched_cities=launched_cities,
        city=city,
        category=category,
        starts_on=starts_on,
        ends_on=ends_on,
        min_daily_rate=min_daily_rate,
        max_daily_rate=max_daily_rate,
        limit=limit,
        cursor=cursor,
    )

    return Page(
        items=[PublicAsset.from_row(row) for row in page.items],
        pagination=PageMeta(limit=limit, next_cursor=page.next_cursor),
    )


@router.get(
    "/{asset_id}",
    response_model=PublicAsset,
    summary="Get a public asset detail",
    responses={404: {"description": "Asset not found or not publicly available."}},
)
async def get_asset(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    launched_cities: list[str] = Depends(get_launched_cities),
) -> PublicAsset:
    """Return the public detail of a single active listing in a launched city.

    Returns 404 for draft, paused, rejected, deleted, not-launched-city, or
    unknown assets — all hidden states are indistinguishable from a genuine 404.
    """
    row = await get_public_asset(session, asset_id, launched_cities=launched_cities)
    return PublicAsset.from_row(row)
