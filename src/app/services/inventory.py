"""Public inventory query service.

Encapsulates the SQL logic for the public asset search and detail endpoints.
All visibility rules are applied in one place so there is no risk of leaking
non-public data through partial filtering.

Visibility rules applied here
------------------------------
1. ``listing_status = 'active'``
2. ``assets.is_available = TRUE``
3. ``assets.deleted_at IS NULL``
4. The asset's city is launched: its ``cities.slug`` is in ``launched_cities``
   (from ``Settings.launched_cities``) and ``cities.is_active = TRUE``.
5. Owning vendor: ``users.is_active = TRUE AND users.deleted_at IS NULL``

A ``city`` filter that is not in ``launched_cities`` raises
:class:`~app.schemas.errors.CityNotLaunchedError` (400) before any query runs.
Unknown and not-yet-launched cities get the same error.

Date-range filter (when both ``starts_on`` and ``ends_on`` are provided)
--------------------------------------------------------------------------
* Exclude assets with a blocked availability window overlapping the range.
* Exclude assets with a booking in one of the live booking statuses
  (``app.db.models.booking.LIVE_BOOKING_STATUSES``, the same set the exclusion
  constraint ``ex_bookings_no_live_overlap`` uses) overlapping the range.
  Overlap check: ``starts_at/starts_on <= ends_on AND ends_at/ends_on >= starts_on``
  (both ends inclusive, matching the GiST exclusion semantics).

Text query
----------
``q`` matches the catalogue item's make, model and aliases with
``plainto_tsquery('simple', q)`` against the same expression that backs the GIN
index ``ix_catalogue_items_search``.

Cursor pagination
-----------------
Pages are ordered by ``(created_at ASC, id ASC)``.  The opaque cursor encodes
the ``(created_at, id)`` of the last returned item and is decoded by
:mod:`app.schemas.pagination`.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, and_, exists, func, literal_column, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.asset import Asset, AvailabilityWindow, ListingStatus
from app.db.models.booking import LIVE_BOOKING_STATUSES, Booking
from app.db.models.catalogue import CATALOGUE_SEARCH_FUNCTION, CatalogueItem
from app.db.models.reference import Category, City
from app.db.models.user import User
from app.schemas.errors import CityNotLaunchedError, ResourceNotFoundError
from app.schemas.pagination import MAX_PAGE_LIMIT, decode_cursor, encode_cursor


@dataclass(frozen=True)
class PublicAssetRow:
    """One visible asset with its reference slugs and catalogue names."""

    asset: Asset
    city_slug: str
    category_slug: str
    make: str
    model: str


@dataclass
class AssetPage:
    """Result of a single inventory page query."""

    items: list[PublicAssetRow]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def ensure_city_launched(city: str | None, launched_cities: Sequence[str]) -> None:
    """Raise :class:`CityNotLaunchedError` unless ``city`` is None or launched."""
    if city is not None and city not in launched_cities:
        raise CityNotLaunchedError(city)


def _public_query(launched_cities: Sequence[str]) -> Select:
    """Select visible assets with their slugs and catalogue names."""
    return (
        select(Asset, City.slug, Category.slug, CatalogueItem.make, CatalogueItem.model)
        .join(City, City.id == Asset.city_id)
        .join(Category, Category.id == Asset.category_id)
        .join(CatalogueItem, CatalogueItem.id == Asset.catalogue_item_id)
        .join(User, User.id == Asset.vendor_id)
        .where(
            Asset.listing_status == ListingStatus.active,
            Asset.is_available.is_(True),
            Asset.deleted_at.is_(None),
            City.slug.in_(list(launched_cities)),
            City.is_active.is_(True),
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )


def _date_overlap_clauses(asset_id_col, starts_on: datetime.date, ends_on: datetime.date):
    """Return NOT EXISTS predicates that drop assets with date conflicts."""
    # Blocked availability window overlapping [starts_on, ends_on] inclusive.
    blocked_window = exists(
        select(AvailabilityWindow.id).where(
            AvailabilityWindow.asset_id == asset_id_col,
            AvailabilityWindow.is_blocked.is_(True),
            AvailabilityWindow.starts_at <= ends_on,
            AvailabilityWindow.ends_at >= starts_on,
        )
    )

    # Booking in a live booking status overlapping [starts_on, ends_on] inclusive.
    live_booking = exists(
        select(Booking.id).where(
            Booking.asset_id == asset_id_col,
            Booking.status.in_(LIVE_BOOKING_STATUSES),
            Booking.starts_on <= ends_on,
            Booking.ends_on >= starts_on,
        )
    )

    return not_(blocked_window), not_(live_booking)


def _text_match(q: str):
    """Full-text predicate on the catalogue item (same expression as the GIN index)."""
    document = getattr(func, CATALOGUE_SEARCH_FUNCTION)(
        CatalogueItem.make, CatalogueItem.model, CatalogueItem.aliases
    )
    return document.op("@@")(func.plainto_tsquery(literal_column("'simple'::regconfig"), q))


def _to_row(result_row) -> PublicAssetRow:
    asset, city_slug, category_slug, make, model = result_row
    return PublicAssetRow(
        asset=asset, city_slug=city_slug, category_slug=category_slug, make=make, model=model
    )


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------


async def search_public_assets(
    session: AsyncSession,
    *,
    launched_cities: Sequence[str],
    city: str | None = None,
    category: str | None = None,
    starts_on: datetime.date | None = None,
    ends_on: datetime.date | None = None,
    min_daily_rate: Decimal | None = None,
    max_daily_rate: Decimal | None = None,
    q: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> AssetPage:
    """Return a page of publicly visible assets in the launched cities.

    Parameters
    ----------
    session:
        An active async SQLAlchemy session.
    launched_cities:
        City slugs open to the public (``Settings.launched_cities``).
    city:
        Optional city slug filter; must be one of ``launched_cities``.
    category:
        Optional category slug filter.
    starts_on / ends_on:
        Inclusive date range for availability filtering.  Both must be provided
        together; if only one is given, the date filter is skipped (callers
        should validate before calling).
    min_daily_rate / max_daily_rate:
        Inclusive rate bounds (NGN, compared to ``assets.daily_rate``).
    q:
        Optional free-text query over catalogue make, model and aliases.
    limit:
        Number of items per page (1–100).
    cursor:
        Opaque pagination cursor from a previous response.

    Raises
    ------
    CityNotLaunchedError
        ``city`` is given and is not in ``launched_cities``.
    """
    ensure_city_launched(city, launched_cities)
    limit = min(limit, MAX_PAGE_LIMIT)

    stmt = _public_query(launched_cities)

    if city is not None:
        stmt = stmt.where(City.slug == city)

    if category is not None:
        stmt = stmt.where(Category.slug == category)

    if min_daily_rate is not None:
        stmt = stmt.where(Asset.daily_rate >= min_daily_rate)

    if max_daily_rate is not None:
        stmt = stmt.where(Asset.daily_rate <= max_daily_rate)

    if q is not None and q.strip():
        stmt = stmt.where(_text_match(q))

    if starts_on is not None and ends_on is not None:
        no_block, no_booking = _date_overlap_clauses(Asset.id, starts_on, ends_on)
        stmt = stmt.where(no_block, no_booking)

    # Cursor: items after the encoded (created_at, id) position.
    if cursor is not None:
        created_at_iso, id_str = decode_cursor(cursor)
        cursor_dt = datetime.datetime.fromisoformat(created_at_iso)
        cursor_id = uuid.UUID(id_str)
        stmt = stmt.where(
            and_(
                Asset.created_at >= cursor_dt,
                # When created_at is identical, use id as tiebreaker.
                ~and_(Asset.created_at == cursor_dt, Asset.id <= cursor_id),
            )
        )

    # Deterministic ordering: (created_at ASC, id ASC).
    stmt = stmt.order_by(Asset.created_at.asc(), Asset.id.asc())

    # Fetch one extra to detect whether a next page exists.
    stmt = stmt.limit(limit + 1)

    result = await session.execute(stmt)
    rows = [_to_row(row) for row in result.all()]

    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1].asset
        next_cursor = encode_cursor(last.created_at.isoformat(), str(last.id))
    else:
        next_cursor = None

    return AssetPage(items=rows, next_cursor=next_cursor)


async def get_public_asset(
    session: AsyncSession,
    asset_id: uuid.UUID,
    *,
    launched_cities: Sequence[str],
) -> PublicAssetRow:
    """Return a single publicly visible asset or raise ``ResourceNotFoundError``.

    Raises :class:`app.schemas.errors.ResourceNotFoundError` when the asset is
    absent, soft-deleted, non-active, in a city that is not launched, or owned
    by an inactive vendor, making all hidden states indistinguishable from a
    genuine 404.
    """
    stmt = _public_query(launched_cities).where(Asset.id == asset_id)
    result = await session.execute(stmt)
    row = result.one_or_none()

    if row is None:
        raise ResourceNotFoundError()

    return _to_row(row)
