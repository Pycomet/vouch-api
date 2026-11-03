"""Public-facing schemas for the asset inventory endpoints.

Visibility contract (public search and detail)
----------------------------------------------
An asset is publicly visible when ALL of the following hold:

1. ``listing_status = 'active'``
2. ``is_available = True``
3. ``deleted_at IS NULL``
4. Its city is launched: the slug is in ``Settings.launched_cities`` and the
   ``cities`` row is active.
5. The owning vendor has ``is_active = True`` and ``deleted_at IS NULL``.

A ``city`` filter outside the launched cities returns 400 ``city_not_launched``;
the detail of an asset in a city that is not launched returns 404.

Date availability (when ``starts_on`` + ``ends_on`` filters are supplied)
--------------------------------------------------------------------------
* An asset is excluded from results for the requested date range when any
  ``AvailabilityWindow`` with ``is_blocked = True`` overlaps the range
  (inclusive on both ends: ``starts_at <= ends_on AND ends_at >= starts_on``).
* An asset is also excluded when any ``Booking`` in a live status
  (``LIVE_BOOKING_STATUSES``) overlaps the range inclusively.
* Positive ``AvailabilityWindow`` rows (``is_blocked = False``) do NOT create
  an allowlist — the absence of blocks is sufficient.

Restricted fields (never serialised in public responses)
--------------------------------------------------------
``serial_number``, ``vendor_id``, ``condition_notes``, ``review_actor_id``,
``reviewed_at``, ``review_reason``, ``listing_status``, ``deleted_at``.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_serializer

if TYPE_CHECKING:
    from app.services.inventory import PublicAssetRow

# Page-size bounds used by routes and the filter model.
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class PublicAsset(BaseModel):
    """Safe public projection of a creative-gear listing."""

    id: uuid.UUID
    name: str | None = Field(description="Vendor display override; usually null.")
    display_name: str = Field(description="``name`` when set, otherwise make and model.")
    make: str
    model: str
    description: str | None
    category: str = Field(description="Category slug.")
    condition_grade: str | None
    daily_rate: Decimal
    weekend_rate: Decimal | None
    weekly_rate: Decimal | None
    deposit_amount: Decimal
    currency: str = "NGN"
    city: str = Field(description="City slug.")
    is_available: bool
    minimum_trust_tier: int = Field(
        description="Minimum renter trust tier required to transact on this asset (0-3)."
    )
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @field_serializer("daily_rate", "deposit_amount", "weekend_rate", "weekly_rate")
    def _serialize_decimal(self, v: Decimal | None) -> str | None:
        """Amounts are serialised as decimal strings per the API contract."""
        return None if v is None else f"{v:.2f}"

    @classmethod
    def from_row(cls, row: PublicAssetRow) -> PublicAsset:
        """Build the public projection from an inventory row."""
        asset = row.asset
        return cls(
            id=asset.id,
            name=asset.name,
            display_name=asset.name or f"{row.make} {row.model}",
            make=row.make,
            model=row.model,
            description=asset.description,
            category=row.category_slug,
            condition_grade=asset.condition_grade,
            daily_rate=asset.daily_rate,
            weekend_rate=asset.weekend_rate,
            weekly_rate=asset.weekly_rate,
            deposit_amount=asset.deposit_amount,
            city=row.city_slug,
            is_available=asset.is_available,
            minimum_trust_tier=asset.minimum_trust_tier,
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )


class AssetSearchFilters(BaseModel):
    """Validated query parameters for ``GET /api/v1/assets``."""

    category: str | None = None
    starts_on: datetime.date | None = None
    ends_on: datetime.date | None = None
    min_daily_rate: Decimal | None = None
    max_daily_rate: Decimal | None = None
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = None
