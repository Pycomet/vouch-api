"""HandoverRecord ORM model for rental item pickup and return handovers."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin


class HandoverDirection(enum.StrEnum):
    pickup = "pickup"
    return_ = "return"


class HandoverStatus(enum.StrEnum):
    draft = "draft"
    vendor_signed = "vendor_signed"
    renter_signed = "renter_signed"
    completed = "completed"


class HandoverRecord(TimestampMixin, Base):
    __tablename__ = "handover_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id"),
        nullable=False,
    )
    direction: Mapped[HandoverDirection] = mapped_column(
        Enum(HandoverDirection, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    status: Mapped[HandoverStatus] = mapped_column(
        Enum(HandoverStatus, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
        default=HandoverStatus.draft,
        server_default=HandoverStatus.draft.value,
    )
    condition_checklist: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # No plain FK to users here: the migrations never created one, and the
    # composite fk_handover_records_booking_vendor (booking_id, vendor_signer_id)
    # -> bookings(id, vendor_id) already ties the signer to a user (M3a A19).
    vendor_signer_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    renter_signer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    vendor_signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    renter_signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    evidence_urls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    location_lat: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    location_lng: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    booking: Mapped[Booking] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Booking",
        foreign_keys=[booking_id],
        back_populates="handover_records",
        lazy="raise",
    )
    renter_signer: Mapped[User | None] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User",
        foreign_keys=[renter_signer_id],
        back_populates="handovers_signed_as_renter",
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint(
            "status <> 'completed' OR ("
            "vendor_signer_id IS NOT NULL AND renter_signer_id IS NOT NULL AND "
            "vendor_signed_at IS NOT NULL AND renter_signed_at IS NOT NULL AND "
            "evidence_urls IS NOT NULL AND jsonb_typeof(evidence_urls) = 'array' AND "
            "jsonb_array_length(evidence_urls) > 0)",
            name="ck_handover_records_completed_requirements",
        ),
        ForeignKeyConstraint(
            ["booking_id", "vendor_signer_id"],
            ["bookings.id", "bookings.vendor_id"],
            name="fk_handover_records_booking_vendor",
        ),
        ForeignKeyConstraint(
            ["booking_id", "renter_signer_id"],
            ["bookings.id", "bookings.renter_id"],
            name="fk_handover_records_booking_renter",
        ),
        UniqueConstraint(
            "booking_id",
            "direction",
            name="uq_handover_records_booking_direction",
        ),
        Index("ix_handover_records_booking_id", "booking_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<HandoverRecord id={self.id!r} booking_id={self.booking_id!r} "
            f"direction={self.direction!r} status={self.status!r}>"
        )
