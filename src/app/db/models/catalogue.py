"""ORM model for the gear catalogue (make/model records that assets point at)."""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# IMMUTABLE SQL function created by migration 0003. It wraps
# to_tsvector('simple', make || ' ' || model || ' ' || array_to_string(aliases, ' '))
# and backs the GIN index ix_catalogue_items_search.
CATALOGUE_SEARCH_FUNCTION = "catalogue_search_vector"


class ValueBand(StrEnum):
    """Replacement-value band of a catalogue item."""

    A = "A"
    B = "B"
    C = "C"


# Default ``assets.minimum_trust_tier`` for a new listing, by band (amendment A3).
BAND_DEFAULT_TIER: dict[ValueBand, int] = {ValueBand.A: 1, ValueBand.B: 2, ValueBand.C: 3}


class CatalogueItem(Base):
    __tablename__ = "catalogue_items"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    category_id: Mapped[int] = mapped_column(
        SmallInteger,
        ForeignKey("categories.id", name="fk_catalogue_items_category_id"),
        nullable=False,
    )
    make: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # Lens mount ('E', 'RF', 'L', 'MFT'); NULL for non-optics.
    mount: Mapped[str | None] = mapped_column(Text, nullable=True)
    specs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    replacement_value_band: Mapped[str] = mapped_column(CHAR(1), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("make", "model", name="uq_catalogue_items_make_model"),
        CheckConstraint(
            "replacement_value_band IN ('A', 'B', 'C')",
            name="ck_catalogue_items_replacement_value_band_valid",
        ),
    )

    category: Mapped[object] = relationship("Category", lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<CatalogueItem id={self.id} make={self.make!r} model={self.model!r} "
            f"band={self.replacement_value_band}>"
        )
