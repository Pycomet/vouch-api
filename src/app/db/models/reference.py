"""ORM models for the reference tables that replaced the city and category enums.

Rows are seeded by migration 0003. Code refers to them by ``slug``; which cities
are open to the public is decided by ``Settings.launched_cities``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, SmallInteger, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class City(Base):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = (UniqueConstraint("slug", name="uq_cities_slug"),)

    def __repr__(self) -> str:
        return f"<City id={self.id} slug={self.slug!r}>"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Handover checklist: {"mode": "serial" | "count", "items": [...]}.
    checklist_template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = (UniqueConstraint("slug", name="uq_categories_slug"),)

    def __repr__(self) -> str:
        return f"<Category id={self.id} slug={self.slug!r}>"
