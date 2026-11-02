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
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    StringConstraints,
    WithJsonSchema,
    model_serializer,
    model_validator,
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

# A handover checklist is flat: at most 50 entries, keys of at most 100 characters,
# each value a short string, a boolean, a number or null (no nested objects/lists).
CHECKLIST_MAX_KEYS = 50
ChecklistKey = Annotated[str, StringConstraints(max_length=100)]
ChecklistValue = (
    Annotated[str, StringConstraints(max_length=500)]
    | bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
    | None
)
Checklist = Annotated[dict[ChecklistKey, ChecklistValue], Field(max_length=CHECKLIST_MAX_KEYS)]


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


class NoShowReport(BaseModel):
    """``/no-show``: the renter reports that the vendor never handed the item over."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"reason": "Nobody at the pickup address"}]},
    )

    reason: str | None = Field(default=None, max_length=500)


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

    checklist: Checklist
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
    """``/resolve``: an admin settles a disputed booking.

    ``amount`` is required for ``deduct`` and refused for ``release`` and
    ``forfeit`` (422). The service checks it again (positive, within the deposit).
    """

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

    @model_validator(mode="after")
    def _amount_only_for_deduct(self) -> Resolve:
        if self.decision == "deduct" and self.amount is None:
            raise ValueError("amount is required when the decision is deduct")
        if self.decision != "deduct" and self.amount is not None:
            raise ValueError(f"amount must not be given when the decision is {self.decision}")
        return self


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class DepositLine(BaseModel):
    amount: str = Field(description="The deposit hold, D.", examples=["150000.00"])
    status: Literal["due", "held", "released", "deducted", "forfeited", "cancelled"] = Field(
        description="``cancelled`` when the booking closed before any payment."
    )
    held_by: Literal["platform"] = "platform"


class RentalLine(BaseModel):
    amount: str = Field(description="Rental plus delivery fee, R + F.", examples=["45000.00"])
    status: Literal["due", "received", "refunded", "paid_out", "cancelled"] = Field(
        description="``cancelled`` when the booking closed before any payment."
    )


class ContributionLine(BaseModel):
    amount: str = Field(description="The damage contribution, C.", examples=["7500.00"])
    status: Literal["due", "received", "refunded", "cancelled"] = Field(
        description="``cancelled`` when the booking closed before any payment."
    )


class CancellationFeeLine(BaseModel):
    amount: str = Field(description="The renter's cancellation fee.", examples=["15000.00"])
    vendor_part: str = Field(description="The vendor's share of the fee.", examples=["7500.00"])
    platform_part: str = Field(description="Vouch's share of the fee.", examples=["7500.00"])


class RefundLine(BaseModel):
    amount: str = Field(
        description="Everything paid (D + C + R + F) less any cancellation fee.",
        examples=["292500.00"],
    )


class NextStep(BaseModel):
    label: str = Field(examples=["Handover Fri 10 Oct"])
    amount_due: str = Field(examples=["0.00"])


# Parts present only when they apply; absent (not null) otherwise.
_OPTIONAL_PARTS = ("contribution", "cancellation_fee", "refund")


class MoneyLine(BaseModel):
    """Where the deposit and rental money stand, and what is due next (spec 3.4).

    Computed from the ledger and the booking status on every read; never stored.
    It adds up: while ``accepted``, ``next.amount_due`` is deposit + contribution +
    rental; after a paid cancellation, ``refund`` + ``cancellation_fee`` is
    everything paid. A booking that closed before any payment (declined, expired
    or cancelled) shows ``cancelled`` for each part and owes nothing.
    ``contribution`` appears only when the booking carries one, ``cancellation_fee``
    only after a paid renter cancellation, and ``refund`` only on a booking
    cancelled after payment.
    """

    deposit: DepositLine
    contribution: ContributionLine | None = None
    rental: RentalLine
    cancellation_fee: CancellationFeeLine | None = None
    refund: RefundLine | None = None
    next: NextStep

    # No return annotation on purpose: with one, pydantic would publish that type
    # (a bare object) as the OpenAPI schema instead of this model's fields.
    @model_serializer(mode="wrap")
    def _drop_absent_parts(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        for part in _OPTIONAL_PARTS:
            if data.get(part) is None:
                data.pop(part, None)
        return data


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
