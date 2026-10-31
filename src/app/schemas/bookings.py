"""Request and response bodies of the booking endpoints (spec 3.3 and 3.4).

Money in request bodies is :data:`MoneyStr` (R20): a JSON *string* such as
``"150000.00"`` with at most ten digits before the point and two after. JSON
numbers, exponents, signs and a third decimal place are refused with 422, so no
amount ever passes through a binary float. Money in responses is always a string
with exactly two decimal places.
"""

from __future__ import annotations

import datetime
import re
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    WithJsonSchema,
)

from app.services.pricing import money

MONEY_PATTERN = r"^[0-9]{1,10}(\.[0-9]{1,2})?$"
_MONEY = re.compile(MONEY_PATTERN)


def format_money(value: Decimal) -> str:
    """``value`` as a two-decimal string (``"150000.00"``), rounded half up."""
    return str(money(value))


def _parse_money(value: object) -> Decimal:
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise ValueError(
            'must be a string amount such as "150000.00" '
            "(up to 10 digits, at most 2 decimal places)"
        )
    return Decimal(value)


MoneyStr = Annotated[
    Decimal,
    BeforeValidator(_parse_money),
    PlainSerializer(format_money, return_type=str),
    WithJsonSchema({"type": "string", "pattern": MONEY_PATTERN, "examples": ["150000.00"]}),
]

EvidenceUrls = Annotated[list[HttpUrl], Field(min_length=1, max_length=20)]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class BookingCreate(BaseModel):
    """``POST /bookings``: the renter asks for an item on inclusive dates."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "asset_id": "4f0c2d8e-5b1a-4c3e-9d7f-2a6b8c0e1f23",
                    "starts_on": "2026-11-13",
                    "ends_on": "2026-11-15",
                    "fulfilment": "pickup",
                    "damage_contribution": False,
                }
            ]
        },
    )

    asset_id: uuid.UUID
    starts_on: datetime.date
    ends_on: datetime.date
    fulfilment: Literal["pickup", "delivery"]
    damage_contribution: bool = False


class ReasonBody(BaseModel):
    """An optional free-text reason (``/cancel``, ``/decline``)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"reason": "Plans changed"}]}
    )

    reason: str | None = Field(default=None, max_length=2000)


class HandoverSubmit(BaseModel):
    """``/handover`` and ``/return``: the caller's side of the handover record."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "checklist": {"body": "no scratches", "battery": "2 of 2"},
                    "evidence_urls": ["https://cdn.example/handover/1.jpg"],
                    "sign": True,
                }
            ]
        },
    )

    checklist: dict[str, Any]
    evidence_urls: EvidenceUrls
    sign: bool


class ClaimCreate(BaseModel):
    """``/claim``: the vendor claims against the deposit inside the claim window."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "amount": "20000.00",
                    "description": "Cracked rear screen",
                    "evidence_urls": ["https://cdn.example/claims/1.jpg"],
                }
            ]
        },
    )

    amount: MoneyStr
    description: str = Field(min_length=1, max_length=2000)
    evidence_urls: EvidenceUrls


class Resolve(BaseModel):
    """``/resolve``: an admin settles a disputed booking."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"decision": "deduct", "reason": "Photos show the damage", "amount": "20000.00"}
            ]
        },
    )

    decision: Literal["release", "deduct", "forfeit"]
    reason: str = Field(min_length=1, max_length=2000)
    amount: MoneyStr | None = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class DepositLine(BaseModel):
    amount: str = Field(description="The deposit hold, D.", examples=["150000.00"])
    status: Literal["due", "held", "released", "deducted", "forfeited"]
    held_by: Literal["platform"] = "platform"


class RentalLine(BaseModel):
    amount: str = Field(description="Rental plus delivery fee, R + F.", examples=["45000.00"])
    status: Literal["due", "received", "refunded", "paid_out"]


class NextStep(BaseModel):
    label: str = Field(examples=["Handover Fri 10 Oct"])
    amount_due: str = Field(examples=["0.00"])


class MoneyLine(BaseModel):
    """Where the deposit and rental money stand, and what is due next (spec 3.4).

    Computed from the ledger and the booking status on every read; never stored.
    """

    deposit: DepositLine
    rental: RentalLine
    next: NextStep


class PriceOut(BaseModel):
    """The money values frozen in the booking's ``price_snapshot``."""

    rental_total: str
    delivery_fee: str
    deposit: str
    damage_contribution: str
    currency: str = "NGN"


class BookingOut(BaseModel):
    id: uuid.UUID
    status: str
    asset_id: uuid.UUID
    renter_id: uuid.UUID
    vendor_id: uuid.UUID
    starts_on: datetime.date
    ends_on: datetime.date
    fulfilment_choice: str
    hold_expires_at: datetime.datetime | None
    deposit_due_at: datetime.datetime | None
    claim_window_ends_at: datetime.datetime | None
    vendor_approved_at: datetime.datetime | None
    price: PriceOut
    money_line: MoneyLine
