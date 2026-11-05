"""Vendor profile ORM model (M3a spec 3.1): one row per vendor user.

The full bank account number is never stored: only the bank's name, the account
name and the last four digits. ``recipient_code`` is the payment provider's
transfer recipient, entered by operations. The CHECKs make an ``active`` profile
carry ``verified_at`` and a ``suspended`` one carry both ``suspended_at`` and
``suspended_reason``; the service layer requires the bank fields before a new
activation.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin


class VendorStatus(enum.StrEnum):
    pending = "pending"
    active = "active"
    suspended = "suspended"


class VendorBusinessType(enum.StrEnum):
    rental_house = "rental_house"
    individual = "individual"


class VendorProfile(TimestampMixin, Base):
    __tablename__ = "vendor_profiles"
    __table_args__ = (
        UniqueConstraint("recipient_code", name="uq_vendor_profiles_recipient_code"),
        CheckConstraint(
            "status <> 'active' OR verified_at IS NOT NULL",
            name="ck_vendor_profiles_active_verified",
        ),
        CheckConstraint(
            "account_last4 IS NULL OR account_last4 ~ '^[0-9]{4}$'",
            name="ck_vendor_profiles_last4_digits",
        ),
        CheckConstraint(
            "(status = 'suspended') = (suspended_at IS NOT NULL AND suspended_reason IS NOT NULL)",
            name="ck_vendor_profiles_suspension",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_vendor_profiles_user_id"),
        primary_key=True,
    )
    status: Mapped[VendorStatus] = mapped_column(
        Enum(VendorStatus, name="vendorstatus"),
        nullable=False,
        default=VendorStatus.pending,
        server_default=VendorStatus.pending.value,
    )
    business_type: Mapped[VendorBusinessType] = mapped_column(
        Enum(VendorBusinessType, name="vendorbusinesstype"), nullable=False
    )
    # Self-declared, shown on the profile.
    is_sub_broker: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    business_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_registration_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    bank_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_last4: Mapped[str | None] = mapped_column(CHAR(4), nullable=True)
    recipient_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_vendor_profiles_verified_by"),
        nullable=True,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        # Bank details are restricted financial data: none of them is shown.
        return f"<VendorProfile user_id={self.user_id} status={self.status.value!r}>"
