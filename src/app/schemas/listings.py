"""Request and response bodies of the vendor's own listings (M3a 4.4, 4.5).

Money in requests is :data:`~app.schemas.bookings.MoneyStr` (a JSON string such
as ``"50000.00"``); money in responses is a two-decimal string. ``category`` is
never accepted: it is copied from the catalogue item. ``OwnAsset`` is the owning
vendor's full view (serial number, condition notes, review reason, ``paused_by``);
the public projection stays :class:`~app.schemas.assets.PublicAsset`, which hides
all of these.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.schemas.bookings import MoneyStr, format_money

ConditionGrade = Literal["excellent", "good", "fair"]
ListingStatusName = Literal["draft", "review", "active", "paused", "rejected"]

MAX_ACCESSORIES = 50
SerialNumber = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
CitySlug = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=64)]
Accessory = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(max_length=5000)]


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("must be greater than zero")
    return value


PositiveMoney = Annotated[MoneyStr, AfterValidator(_positive)]


class FulfilmentIn(BaseModel):
    """How the renter can get the unit. ``delivery_fee`` is charged on delivery only."""

    model_config = ConfigDict(extra="forbid")

    pickup: bool
    delivery: bool
    delivery_fee: MoneyStr | None = None

    def stored(self) -> dict[str, Any]:
        """The ``assets.fulfilment`` JSON (money as a string; no fee key when unset)."""
        data: dict[str, Any] = {"pickup": self.pickup, "delivery": self.delivery}
        if self.delivery_fee is not None:
            data["delivery_fee"] = format_money(self.delivery_fee)
        return data


_CREATE_EXAMPLE = {
    "catalogue_item_id": "7b8c9d0e-1f2a-4b3c-8d4e-5f6a7b8c9d0e",
    "city": "city-slug",
    "serial_number": "SN-0001",
    "condition_grade": "excellent",
    "daily_rate": "50000.00",
    "deposit_amount": "150000.00",
    "weekend_rate": "90000.00",
    "description": "Full-frame cinema body with two batteries.",
    "condition_notes": "Light wear on the grip.",
    "included_accessories": ["2 batteries", "128 GB card"],
    "fulfilment": {"pickup": True, "delivery": True, "delivery_fee": "5000.00"},
}


class OwnAssetCreate(BaseModel):
    """``POST /vendor/assets``: a new listing, created as ``draft``.

    ``minimum_trust_tier`` defaults from the catalogue item's band (A→1, B→2,
    C→3); the vendor may raise it (up to 3) but not lower it below that default.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_CREATE_EXAMPLE]})

    catalogue_item_id: uuid.UUID
    city: CitySlug = Field(description="City slug; must be a launched city.")
    serial_number: SerialNumber | None = None
    condition_grade: ConditionGrade | None = None
    daily_rate: PositiveMoney
    deposit_amount: MoneyStr
    weekend_rate: PositiveMoney | None = None
    weekly_rate: PositiveMoney | None = None
    description: LongText | None = None
    condition_notes: LongText | None = None
    included_accessories: list[Accessory] = Field(default_factory=list, max_length=MAX_ACCESSORIES)
    fulfilment: FulfilmentIn = Field(
        default_factory=lambda: FulfilmentIn(pickup=True, delivery=False)
    )
    minimum_trust_tier: int | None = Field(default=None, ge=0, le=3)


# Fields a PATCH may not set to null: the database requires them.
_NOT_NULLABLE = frozenset(
    {
        "catalogue_item_id",
        "city",
        "daily_rate",
        "deposit_amount",
        "included_accessories",
        "fulfilment",
    }
)


class OwnAssetUpdate(BaseModel):
    """``PATCH /vendor/assets/{id}``: only the fields given change.

    Identity fields (``catalogue_item_id``, ``serial_number``, ``city``) change only
    in ``draft`` or ``rejected``; commercial fields also in ``active`` and
    ``paused``. ``minimum_trust_tier`` is not here: only an admin changes it after
    create, so sending it is 422 (an unknown field).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"daily_rate": "55000.00", "condition_grade": "good"}]},
    )

    catalogue_item_id: uuid.UUID | None = None
    city: CitySlug | None = None
    serial_number: SerialNumber | None = None
    condition_grade: ConditionGrade | None = None
    daily_rate: PositiveMoney | None = None
    deposit_amount: MoneyStr | None = None
    weekend_rate: PositiveMoney | None = None
    weekly_rate: PositiveMoney | None = None
    description: LongText | None = None
    condition_notes: LongText | None = None
    included_accessories: list[Accessory] | None = Field(default=None, max_length=MAX_ACCESSORIES)
    fulfilment: FulfilmentIn | None = None

    @model_validator(mode="after")
    def _required_fields_not_null(self) -> OwnAssetUpdate:
        nulled = sorted(
            name for name in self.model_fields_set & _NOT_NULLABLE if getattr(self, name) is None
        )
        if nulled:
            raise ValueError(f"must not be null: {', '.join(nulled)}")
        return self

    def changes(self) -> dict[str, Any]:
        """The fields the client sent, with their parsed values."""
        return {name: getattr(self, name) for name in self.model_fields_set}


class FulfilmentOut(BaseModel):
    pickup: bool
    delivery: bool
    delivery_fee: str | None = Field(description="Two-decimal string; null when not set.")


class OwnAsset(BaseModel):
    """The owning vendor's view of a listing (never shown to anyone else)."""

    id: uuid.UUID
    catalogue_item_id: uuid.UUID
    make: str
    model: str
    category: str = Field(description="Category slug (the catalogue item's).")
    city: str = Field(description="City slug.")
    serial_number: str | None
    condition_grade: str | None
    condition_notes: str | None
    description: str | None
    daily_rate: str
    weekend_rate: str | None
    weekly_rate: str | None
    deposit_amount: str
    currency: str = "NGN"
    included_accessories: list[str]
    fulfilment: FulfilmentOut
    listing_status: ListingStatusName
    minimum_trust_tier: int = Field(description="Minimum renter trust tier (0-3).")
    paused_by: Literal["vendor", "admin"] | None = Field(
        description="Who paused the listing; set only while it is paused."
    )
    review_reason: str | None = Field(description="Why an admin last rejected it, if they did.")
    reviewed_at: datetime.datetime | None
    is_available: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime
