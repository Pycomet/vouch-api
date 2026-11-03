"""ORM model for vendor references (spec 2.3).

A vendor reference vouches for a renter after a booking with that vendor. One
reference row is one of the two ways to reach the computed trust tier 2. The
booking must belong to the same renter and vendor (composite foreign keys).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VendorReference(Base):
    __tablename__ = "vendor_references"
    __table_args__ = (
        UniqueConstraint("renter_id", "vendor_id", name="uq_vendor_references_renter_vendor"),
        ForeignKeyConstraint(
            ["booking_id", "renter_id"],
            ["bookings.id", "bookings.renter_id"],
            name="fk_vendor_references_booking_renter",
        ),
        ForeignKeyConstraint(
            ["booking_id", "vendor_id"],
            ["bookings.id", "bookings.vendor_id"],
            name="fk_vendor_references_booking_vendor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    renter_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", name="fk_vendor_references_renter_id"),
        nullable=False,
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", name="fk_vendor_references_vendor_id"),
        nullable=False,
    )
    booking_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<VendorReference id={self.id} renter_id={self.renter_id} "
            f"vendor_id={self.vendor_id} booking_id={self.booking_id}>"
        )
