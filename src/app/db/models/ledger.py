"""Ledger ORM model (spec 2.5): append-only double-entry rows.

The database rejects UPDATE and DELETE (``trg_ledger_no_update``) and checks at
COMMIT that every ``txn_group`` has equal debits and credits
(``trg_ledger_balanced``). The model therefore offers no update helpers; fix a
mistake with a reversing entry.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LedgerAccount(enum.StrEnum):
    renter_deposit_liability = "renter_deposit_liability"
    platform_cash = "platform_cash"
    vendor_payable = "vendor_payable"
    platform_revenue = "platform_revenue"
    protection_pool = "protection_pool"
    processing_fees = "processing_fees"


class Direction(enum.StrEnum):
    dr = "dr"
    cr = "cr"


def _in_list(values: type[enum.StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in values)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(
            f"account IN ({_in_list(LedgerAccount)})", name="ck_ledger_entries_account_valid"
        ),
        CheckConstraint("direction IN ('dr', 'cr')", name="ck_ledger_entries_direction_valid"),
        CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
        Index("ix_ledger_booking", "booking_id"),
        Index("ix_ledger_group", "txn_group"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    account: Mapped[LedgerAccount] = mapped_column(String, nullable=False)
    direction: Mapped[Direction] = mapped_column(String(2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'NGN'"))
    txn_group: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<LedgerEntry id={self.id} {self.direction} {self.account} "
            f"{self.amount} group={self.txn_group}>"
        )
