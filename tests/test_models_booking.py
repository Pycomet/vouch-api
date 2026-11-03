"""Model structure tests for bookings (no live DB)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, DateTime, Enum, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint, dialect
from sqlalchemy.schema import CreateTable

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.models.booking import (
    LIVE_BOOKING_STATUSES,
    TERMINAL_BOOKING_STATUSES,
    BookingStatus,
    live_status_predicate,
)


def _table(name: str):
    return Base.metadata.tables[name]


def test_bookings_table_in_metadata() -> None:
    assert "bookings" in Base.metadata.tables


def test_booking_status_is_enum_type() -> None:
    col = _table("bookings").c["status"]
    assert isinstance(col.type, Enum)


def test_booking_total_rental_fee_is_numeric() -> None:
    col = _table("bookings").c["total_rental_fee"]
    assert isinstance(col.type, Numeric)


def test_booking_asset_id_fk_references_assets() -> None:
    col = _table("bookings").c["asset_id"]
    fk_targets = {fk.target_fullname for fk in col.foreign_keys}
    assert "assets.id" in fk_targets


def test_booking_renter_id_fk_references_users() -> None:
    col = _table("bookings").c["renter_id"]
    fk_targets = {fk.target_fullname for fk in col.foreign_keys}
    assert "users.id" in fk_targets


def test_booking_renter_and_vendor_ids_reference_users() -> None:
    table = _table("bookings")
    for column_name in ("renter_id", "vendor_id"):
        fk_targets = {fk.target_fullname for fk in table.c[column_name].foreign_keys}
        assert fk_targets == {"users.id"}


def _check_sql() -> set[str]:
    return {
        str(constraint.sqltext)
        for constraint in _table("bookings").constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_booking_amounts_and_date_range_are_constrained() -> None:
    checks = _check_sql()
    assert "total_rental_fee > 0" in checks
    assert "deposit_amount >= 0" in checks
    assert "starts_on <= ends_on" in checks


LIVE = (
    "requested",
    "accepted",
    "deposit_paid",
    "confirmed",
    "in_progress",
    "returned",
)


def test_booking_status_has_the_thirteen_state_machine_values() -> None:
    assert [status.value for status in BookingStatus] == [
        "requested",
        "accepted",
        "deposit_paid",
        "confirmed",
        "in_progress",
        "returned",
        "completed",
        "declined",
        "expired",
        "cancelled_by_renter",
        "cancelled_by_vendor",
        "disputed",
        "lost",
    ]
    assert _table("bookings").c["status"].server_default.arg == "requested"


def test_live_and_terminal_status_sets() -> None:
    assert isinstance(LIVE_BOOKING_STATUSES, frozenset)
    assert {status.value for status in LIVE_BOOKING_STATUSES} == set(LIVE)
    assert {status.value for status in TERMINAL_BOOKING_STATUSES} == {
        "completed",
        "declined",
        "expired",
        "cancelled_by_renter",
        "cancelled_by_vendor",
        "lost",
    }
    assert not LIVE_BOOKING_STATUSES & TERMINAL_BOOKING_STATUSES
    # disputed is neither live (the unit is back) nor terminal (awaits resolution).
    assert BookingStatus.disputed not in LIVE_BOOKING_STATUSES | TERMINAL_BOOKING_STATUSES


def test_live_bookings_cannot_overlap() -> None:
    constraints = [
        constraint
        for constraint in _table("bookings").constraints
        if isinstance(constraint, ExcludeConstraint)
    ]
    assert len(constraints) == 1
    constraint = constraints[0]
    assert constraint.name == "ex_bookings_no_live_overlap"
    assert str(constraint.where) == live_status_predicate()
    assert live_status_predicate() == "status IN (" + ", ".join(f"'{s}'" for s in LIVE) + ")"
    compiled = str(CreateTable(constraint.table).compile(dialect=dialect()))
    assert "asset_id WITH =" in compiled
    assert "daterange(starts_on, ends_on, '[]') WITH &&" in compiled


def test_booking_state_machine_columns() -> None:
    table = _table("bookings")
    for name in (
        "hold_expires_at",
        "deposit_due_at",
        "claim_window_ends_at",
        "vendor_approved_at",
        "at_risk_at",
        "claimed_at",
    ):
        assert isinstance(table.c[name].type, DateTime), name
        assert table.c[name].type.timezone, name
        assert table.c[name].nullable, name
    assert isinstance(table.c["price_snapshot"].type, JSONB)
    assert not table.c["price_snapshot"].nullable
    assert isinstance(table.c["fulfilment_choice"].type, Text)
    assert not table.c["fulfilment_choice"].nullable
    assert isinstance(table.c["claim_amount"].type, Numeric)
    assert (table.c["claim_amount"].type.precision, table.c["claim_amount"].type.scale) == (12, 2)
    assert isinstance(table.c["claim_evidence"].type, JSONB)
    assert table.c["claim_description"].nullable


def test_booking_state_machine_checks() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in _table("bookings").constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_bookings_requested_has_hold"] == (
        "status <> 'requested' OR hold_expires_at IS NOT NULL"
    )
    assert checks["ck_bookings_accepted_has_deposit_due"] == (
        "status <> 'accepted' OR deposit_due_at IS NOT NULL"
    )
    assert checks["ck_bookings_returned_has_claim_window"] == (
        "status NOT IN ('returned', 'disputed') OR claim_window_ends_at IS NOT NULL"
    )
    assert "claim_amount IS NULL" in checks["ck_bookings_claim_complete"]
    assert "claimed_at IS NOT NULL" in checks["ck_bookings_claim_complete"]
    assert checks["ck_bookings_not_self"] == "renter_id <> vendor_id"
    assert checks["ck_bookings_claim_amount_positive"] == "claim_amount > 0"
    assert checks["ck_bookings_fulfilment_choice_valid"] == (
        "fulfilment_choice IN ('pickup', 'delivery')"
    )
