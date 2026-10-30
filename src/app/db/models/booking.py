"""ORM model for the bookings table and the booking state machine's status sets.

``LIVE_BOOKING_STATUSES`` is the one definition of "a booking that holds the unit".
The exclusion constraint ``ex_bookings_no_live_overlap`` and the inventory date
filter are both built from it.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin


class BookingStatus(StrEnum):
    requested = "requested"
    accepted = "accepted"
    deposit_paid = "deposit_paid"
    confirmed = "confirmed"
    in_progress = "in_progress"
    returned = "returned"
    completed = "completed"
    declined = "declined"
    expired = "expired"
    cancelled_by_renter = "cancelled_by_renter"
    cancelled_by_vendor = "cancelled_by_vendor"
    disputed = "disputed"
    lost = "lost"


# A live booking holds the unit for its dates. ``returned`` still holds it until
# ``completed`` (claim window and turnaround buffer).
LIVE_BOOKING_STATUSES: frozenset[BookingStatus] = frozenset(
    {
        BookingStatus.requested,
        BookingStatus.accepted,
        BookingStatus.deposit_paid,
        BookingStatus.confirmed,
        BookingStatus.in_progress,
        BookingStatus.returned,
    }
)

# No transition leaves these. ``disputed`` is neither live nor terminal.
TERMINAL_BOOKING_STATUSES: frozenset[BookingStatus] = frozenset(
    {
        BookingStatus.completed,
        BookingStatus.declined,
        BookingStatus.expired,
        BookingStatus.cancelled_by_renter,
        BookingStatus.cancelled_by_vendor,
        BookingStatus.lost,
    }
)


def live_status_predicate() -> str:
    """SQL ``status IN (...)`` over the live statuses, in declaration order."""
    values = ", ".join(
        f"'{status.value}'" for status in BookingStatus if status in LIVE_BOOKING_STATUSES
    )
    return f"status IN ({values})"


# Claim columns are all empty, or amount, description and claimed_at are all set
# (evidence is optional).
CLAIM_COMPLETE = (
    "(claim_amount IS NULL AND claim_description IS NULL AND claim_evidence IS NULL "
    "AND claimed_at IS NULL) OR "
    "(claim_amount IS NOT NULL AND claim_description IS NOT NULL AND claimed_at IS NOT NULL)"
)


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id"),
        nullable=False,
    )
    renter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
    starts_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="bookingstatus"),
        default=BookingStatus.requested,
        server_default=BookingStatus.requested.value,
        nullable=False,
    )
    total_rental_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    deposit_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    platform_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(nullable=True)
    hold_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deposit_due_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_window_ends_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    vendor_approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    at_risk_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Rate basis, rates, delivery fee, deposit and commission frozen at request time.
    price_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fulfilment_choice: Mapped[str] = mapped_column(Text, nullable=False)
    claim_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    claim_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_evidence: Mapped[list | dict | None] = mapped_column(JSONB, nullable=True)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("total_rental_fee > 0", name="ck_bookings_total_rental_fee_positive"),
        CheckConstraint("deposit_amount >= 0", name="ck_bookings_deposit_amount_nonnegative"),
        CheckConstraint("starts_on <= ends_on", name="ck_bookings_valid_date_range"),
        CheckConstraint(
            "fulfilment_choice IN ('pickup', 'delivery')",
            name="ck_bookings_fulfilment_choice_valid",
        ),
        CheckConstraint("claim_amount > 0", name="ck_bookings_claim_amount_positive"),
        CheckConstraint(
            "status <> 'requested' OR hold_expires_at IS NOT NULL",
            name="ck_bookings_requested_has_hold",
        ),
        CheckConstraint(
            "status <> 'accepted' OR deposit_due_at IS NOT NULL",
            name="ck_bookings_accepted_has_deposit_due",
        ),
        CheckConstraint(
            "status NOT IN ('returned', 'disputed') OR claim_window_ends_at IS NOT NULL",
            name="ck_bookings_returned_has_claim_window",
        ),
        CheckConstraint(CLAIM_COMPLETE, name="ck_bookings_claim_complete"),
        CheckConstraint("renter_id <> vendor_id", name="ck_bookings_not_self"),
        ExcludeConstraint(
            ("asset_id", "="),
            (text("daterange(starts_on, ends_on, '[]')"), "&&"),
            where=text(live_status_predicate()),
            name="ex_bookings_no_live_overlap",
            using="gist",
        ),
        Index("ix_bookings_asset_id", "asset_id"),
        Index("ix_bookings_renter_id", "renter_id"),
        Index("ix_bookings_vendor_id", "vendor_id"),
        UniqueConstraint("id", "renter_id", name="uq_bookings_id_renter"),
        UniqueConstraint("id", "vendor_id", name="uq_bookings_id_vendor"),
    )

    asset: Mapped[object] = relationship(
        "Asset",
        back_populates="bookings",
        lazy="raise",
    )
    renter: Mapped[object] = relationship(
        "User",
        foreign_keys=[renter_id],
        back_populates="bookings_as_renter",
        lazy="raise",
    )
    vendor: Mapped[object] = relationship(
        "User",
        foreign_keys=[vendor_id],
        back_populates="bookings_as_vendor",
        lazy="raise",
    )
    payments: Mapped[list[object]] = relationship(
        "Payment",
        back_populates="booking",
        lazy="raise",
    )
    handover_records: Mapped[list[object]] = relationship(
        "HandoverRecord",
        foreign_keys="HandoverRecord.booking_id",
        back_populates="booking",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Booking id={self.id} asset_id={self.asset_id} "
            f"renter_id={self.renter_id} status={self.status.value}>"
        )
