"""Model structure tests for vendor references (no live DB)."""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.models import VendorReference


def test_vendor_references_table() -> None:
    table = Base.metadata.tables["vendor_references"]
    assert VendorReference.__table__ is table
    assert set(table.c.keys()) == {"id", "renter_id", "vendor_id", "booking_id", "created_at"}
    assert not table.c["booking_id"].nullable
    assert not table.c["renter_id"].nullable
    assert not table.c["vendor_id"].nullable
    assert table.c["created_at"].type.timezone


def test_vendor_references_unique_pair_and_foreign_keys() -> None:
    table = Base.metadata.tables["vendor_references"]
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert uniques["uq_vendor_references_renter_vendor"] == ("renter_id", "vendor_id")
    fks = {
        c.name: (tuple(c.column_keys), tuple(e.target_fullname for e in c.elements))
        for c in table.constraints
        if isinstance(c, ForeignKeyConstraint)
    }
    assert fks["fk_vendor_references_renter_id"] == (("renter_id",), ("users.id",))
    assert fks["fk_vendor_references_vendor_id"] == (("vendor_id",), ("users.id",))
    assert fks["fk_vendor_references_booking_renter"] == (
        ("booking_id", "renter_id"),
        ("bookings.id", "bookings.renter_id"),
    )
    assert fks["fk_vendor_references_booking_vendor"] == (
        ("booking_id", "vendor_id"),
        ("bookings.id", "bookings.vendor_id"),
    )
