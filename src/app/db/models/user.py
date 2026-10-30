"""ORM models for users and KYC profiles."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import SoftDeleteMixin, TimestampMixin

if TYPE_CHECKING:
    pass


class RenterRole(enum.StrEnum):
    renter = "renter"
    vendor = "vendor"
    admin = "admin"


class KYCStatus(enum.StrEnum):
    pending = "pending"
    submitted = "submitted"
    verified = "verified"
    failed = "failed"


class TrustTier(enum.IntEnum):
    """Renter trust tiers (spec 2.3, amendments A3 and A6).

    ``kyc_profiles.tier`` stores only the verified level (0 or 1). Tier 2 is
    computed in the service layer from history or a vendor reference, and tier 3
    is per booking (``bookings.vendor_approved_at``). Assets name the tier they
    need in ``assets.minimum_trust_tier`` (0-3).
    """

    phone_verified = 0
    identity_verified = 1
    established = 2
    vendor_approved = 3


class VerificationRequirement(enum.StrEnum):
    national_id = "national_id"
    liveness = "liveness"
    address = "address"


@dataclass(frozen=True)
class TrustTierPolicy:
    required_verifications: frozenset[VerificationRequirement]


_IDENTITY_CHECKS = frozenset(VerificationRequirement)

# Tiers 2 and 3 add history or approval on top of tier 1, not more identity checks.
_TRUST_TIER_POLICIES = {
    TrustTier.phone_verified: TrustTierPolicy(required_verifications=frozenset()),
    TrustTier.identity_verified: TrustTierPolicy(required_verifications=_IDENTITY_CHECKS),
    TrustTier.established: TrustTierPolicy(required_verifications=_IDENTITY_CHECKS),
    TrustTier.vendor_approved: TrustTierPolicy(required_verifications=_IDENTITY_CHECKS),
}


def trust_tier_policy(tier: TrustTier) -> TrustTierPolicy:
    return _TRUST_TIER_POLICIES[tier]


class User(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone", "phone"),
        CheckConstraint(
            "restricted_at IS NULL OR restricted_reason IS NOT NULL",
            name="ck_users_restriction_has_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    phone: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    role: Mapped[RenterRole] = mapped_column(
        Enum(RenterRole, name="renterrole"),
        nullable=False,
        default=RenterRole.renter,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Set when the renter is frozen (e.g. no contact after the rental started).
    restricted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restricted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    kyc_profile: Mapped[KYCProfile | None] = relationship(
        "KYCProfile",
        back_populates="user",
        uselist=False,
        lazy="raise",
    )
    assets: Mapped[list] = relationship(
        "Asset",
        foreign_keys="Asset.vendor_id",
        back_populates="vendor",
        lazy="raise",
    )
    bookings_as_renter: Mapped[list] = relationship(
        "Booking",
        foreign_keys="Booking.renter_id",
        back_populates="renter",
        lazy="raise",
    )
    bookings_as_vendor: Mapped[list] = relationship(
        "Booking",
        foreign_keys="Booking.vendor_id",
        back_populates="vendor",
        lazy="raise",
    )
    handovers_signed_as_vendor: Mapped[list] = relationship(
        "HandoverRecord",
        foreign_keys="HandoverRecord.vendor_signer_id",
        back_populates="vendor_signer",
        lazy="raise",
    )
    handovers_signed_as_renter: Mapped[list] = relationship(
        "HandoverRecord",
        foreign_keys="HandoverRecord.renter_signer_id",
        back_populates="renter_signer",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} phone={self.phone!r} role={self.role.value!r}>"


class KYCProfile(TimestampMixin, Base):
    __tablename__ = "kyc_profiles"
    __table_args__ = (
        CheckConstraint("tier BETWEEN 0 AND 1", name="ck_kyc_profiles_supported_tier"),
        CheckConstraint(
            "status <> 'verified' OR verified_at IS NOT NULL",
            name="ck_kyc_profiles_verified_at_required",
        ),
        CheckConstraint(
            "(status = 'verified' AND verified_at IS NOT NULL AND tier = 1) OR "
            "(status <> 'verified' AND verified_at IS NULL AND tier = 0)",
            name="ck_kyc_profiles_verification_consistency",
        ),
        UniqueConstraint("provider_reference", name="uq_kyc_profiles_provider_reference"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[KYCStatus] = mapped_column(
        Enum(KYCStatus, name="kycstatus"),
        nullable=False,
        default=KYCStatus.pending,
    )
    tier: Mapped[TrustTier] = mapped_column(
        Integer, nullable=False, default=TrustTier.phone_verified
    )
    national_id_type: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="kyc_profile",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<KYCProfile id={self.id} user_id={self.user_id} status={self.status.value!r}>"
