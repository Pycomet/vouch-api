"""Payment ORM model."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin


class PaymentType(enum.StrEnum):
    deposit = "deposit"
    rental_balance = "rental_balance"
    payout = "payout"
    refund = "refund"
    protection_waiver = "protection_waiver"


class PaymentStatus(enum.StrEnum):
    pending = "pending"
    authorized = "authorized"
    captured = "captured"
    failed = "failed"
    refunded = "refunded"


class DepositStatus(enum.StrEnum):
    held = "held"
    released = "released"
    claimed = "claimed"

    def can_transition_to(self, target: DepositStatus) -> bool:
        return self is DepositStatus.held and target in {
            DepositStatus.released,
            DepositStatus.claimed,
        }


class PayoutStatus(enum.StrEnum):
    pending = "pending"
    eligible = "eligible"
    paid = "paid"

    def can_transition_to(self, target: PayoutStatus) -> bool:
        return (self, target) in {
            (PayoutStatus.pending, PayoutStatus.eligible),
            (PayoutStatus.eligible, PayoutStatus.paid),
        }


class Payment(TimestampMixin, Base):
    """Represents a financial transaction tied to a booking."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "type <> 'payout' OR (vendor_recipient_code IS NOT NULL AND platform_fee IS NOT NULL)",
            name="ck_payments_payout_fields_required",
        ),
        CheckConstraint(
            "platform_fee IS NULL OR platform_fee >= 0",
            name="ck_payments_platform_fee_nonnegative",
        ),
        CheckConstraint(
            "(type = 'deposit' AND deposit_status IS NOT NULL) OR "
            "(type <> 'deposit' AND deposit_status IS NULL AND deposit_held_at IS NULL "
            "AND deposit_released_at IS NULL AND deposit_claimed_at IS NULL)",
            name="ck_payments_deposit_lifecycle_type",
        ),
        CheckConstraint(
            "(type = 'payout' AND payout_status IS NOT NULL) OR "
            "(type <> 'payout' AND payout_status IS NULL AND payout_eligible_at IS NULL "
            "AND paid_at IS NULL)",
            name="ck_payments_payout_lifecycle_type",
        ),
        CheckConstraint(
            "deposit_status IS NULL OR (deposit_held_at IS NOT NULL "
            "AND (deposit_status <> 'released' OR deposit_released_at IS NOT NULL) "
            "AND (deposit_status <> 'claimed' OR deposit_claimed_at IS NOT NULL))",
            name="ck_payments_deposit_status_timestamps",
        ),
        CheckConstraint(
            "payout_status IS NULL OR (payout_status = 'pending' OR "
            "payout_eligible_at IS NOT NULL) AND "
            "(payout_status <> 'paid' OR paid_at IS NOT NULL)",
            name="ck_payments_payout_status_timestamps",
        ),
        Index("ix_payments_booking_id", "booking_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id"),
        nullable=False,
    )
    type: Mapped[PaymentType] = mapped_column(Enum(PaymentType, name="paymenttype"), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="paymentstatus"), default=PaymentStatus.pending
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String, default="NGN")
    provider: Mapped[str] = mapped_column(String, default="paystack")
    provider_reference: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    provider_virtual_account: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_recipient_code: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_subaccount_code: Mapped[str | None] = mapped_column(String, nullable=True)
    platform_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    settlement_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    deposit_status: Mapped[DepositStatus | None] = mapped_column(
        Enum(DepositStatus, name="depositstatus"), nullable=True
    )
    payout_status: Mapped[PayoutStatus | None] = mapped_column(
        Enum(PayoutStatus, name="payoutstatus"), nullable=True
    )
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deposit_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deposit_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deposit_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payout_eligible_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column(JSONB, name="metadata", nullable=True)

    booking: Mapped[Booking] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="payments",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id!s} type={self.type.value} "
            f"status={self.status.value} amount={self.amount} {self.currency}>"
        )
