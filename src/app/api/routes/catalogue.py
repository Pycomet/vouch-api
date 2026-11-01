"""Catalogue search (spec 3.3): ``GET /catalogue/search?q=&city=&from=&to=``.

Open to any caller, no token needed. It is backed by the same inventory service as
``/assets`` (amendment A7), so the visibility rules and the city gate are the same:
a ``city`` outside the launched cities is 400 ``city_not_launched``.

* ``q`` matches catalogue make, model and aliases (full-text, the GIN expression).
* ``from`` and ``to`` are required together (400 ``invalid_request`` otherwise,
  and when ``from`` is after ``to``); with them, only units with no live booking
  and no blocked window overlapping the inclusive range are returned.
* **Equivalents.** When nothing is returned and ``q`` matched catalogue items,
  the search runs again without ``q``, restricted to those items' categories, and
  the response says ``equivalents: true``.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_launched_cities
from app.api.examples import (
    CITY_NOT_LAUNCHED,
    INVALID_CURSOR,
    INVALID_DATES,
    SEARCH_EXAMPLE,
    documented,
)
from app.db.session import get_session
from app.schemas.assets import DEFAULT_LIMIT, MAX_LIMIT, CatalogueSearchPage, PublicAsset
from app.schemas.errors import InvalidRequestError
from app.schemas.pagination import PageMeta, decode_cursor
from app.services.inventory import matching_categories, search_public_assets

router = APIRouter(prefix="/catalogue", tags=["inventory"])


@router.get(
    "/search",
    response_model=CatalogueSearchPage,
    summary="Search the catalogue for units free on given dates",
    responses=documented(
        200, SEARCH_EXAMPLE, CITY_NOT_LAUNCHED, INVALID_DATES, INVALID_CURSOR, 422
    ),
)
async def search_catalogue(
    q: str | None = Query(None, max_length=200, description="Free text: make, model or alias."),
    city: str | None = Query(None, description="City slug; must be a launched city."),
    from_: datetime.date | None = Query(
        None, alias="from", description="First rental day (inclusive). Required with `to`."
    ),
    to: datetime.date | None = Query(
        None, description="Last rental day (inclusive). Required with `from`."
    ),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page (≤100)."),
    cursor: str | None = Query(None, description="Opaque pagination cursor from a prior response."),
    session: AsyncSession = Depends(get_session),
    launched_cities: list[str] = Depends(get_launched_cities),
) -> CatalogueSearchPage:
    """Units matching ``q`` that are free on the dates, or free equivalents."""
    # Details name only the dates that were given (no "None" strings).
    given = {key: day.isoformat() for key, day in (("from", from_), ("to", to)) if day}
    if (from_ is None) != (to is None):
        raise InvalidRequestError(
            message="Both 'from' and 'to' are required together for date filtering.",
            details=given,
        )
    if from_ is not None and to is not None and from_ > to:
        raise InvalidRequestError(message="'from' must not be after 'to'.", details=given)
    if cursor is not None:
        decode_cursor(cursor)  # CursorDecodeError (400) before any query

    filters = {
        "launched_cities": launched_cities,
        "city": city.strip().lower() if city else None,
        "starts_on": from_,
        "ends_on": to,
        "limit": limit,
        "cursor": cursor,
    }
    page = await search_public_assets(session, q=q, **filters)
    equivalents = False
    if not page.items and q and q.strip():
        categories = await matching_categories(session, q)
        if categories:
            page = await search_public_assets(session, categories=categories, **filters)
            equivalents = True

    return CatalogueSearchPage(
        items=[PublicAsset.from_row(row) for row in page.items],
        pagination=PageMeta(limit=limit, next_cursor=page.next_cursor),
        equivalents=equivalents,
    )
