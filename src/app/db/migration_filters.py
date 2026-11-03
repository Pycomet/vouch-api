"""Autogenerate filters shared by ``migrations/env.py`` and the schema-drift test."""

from __future__ import annotations

from typing import Any

# Indexes the migrations own and the ORM cannot express (expression indexes over a
# SQL function); autogenerate must neither drop nor recreate them.
MIGRATION_OWNED_INDEXES = frozenset({"ix_catalogue_items_search"})


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Alembic ``include_object`` hook: skip migration-owned indexes."""
    return not (type_ == "index" and name in MIGRATION_OWNED_INDEXES)
