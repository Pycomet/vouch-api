"""Model structure tests for handover_records (no live DB)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

import app.db.models  # noqa: F401
from app.db.base import Base


def _table(name: str):
    return Base.metadata.tables[name]


def test_handover_records_table_in_metadata() -> None:
    assert "handover_records" in Base.metadata.tables


def test_handover_direction_enum_db_values_include_pickup_and_return() -> None:
    col = _table("handover_records").c["direction"]
    assert isinstance(col.type, Enum)
    assert "pickup" in col.type.enums
    assert "return" in col.type.enums
    assert "return_" not in col.type.enums


def test_handover_condition_checklist_is_jsonb() -> None:
    col = _table("handover_records").c["condition_checklist"]
    assert isinstance(col.type, JSONB)


def test_handover_booking_id_fk_references_bookings() -> None:
    col = _table("handover_records").c["booking_id"]
    fk_targets = {fk.target_fullname for fk in col.foreign_keys}
    assert "bookings.id" in fk_targets


def test_handover_has_one_booking_index() -> None:
    indexes = [
        index
        for index in _table("handover_records").indexes
        if index.name == "ix_handover_records_booking_id"
    ]
    assert len(indexes) == 1


def test_handover_direction_is_unique_per_booking() -> None:
    constraints = [
        constraint
        for constraint in _table("handover_records").constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    assert any(
        constraint.name == "uq_handover_records_booking_direction"
        and [column.name for column in constraint.columns] == ["booking_id", "direction"]
        for constraint in constraints
    )


def test_completed_handover_requires_signers_timestamps_and_evidence() -> None:
    checks = {
        str(constraint.sqltext)
        for constraint in _table("handover_records").constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert any(
        "status <> 'completed'" in check
        and "vendor_signer_id IS NOT NULL" in check
        and "renter_signer_id IS NOT NULL" in check
        and "vendor_signed_at IS NOT NULL" in check
        and "renter_signed_at IS NOT NULL" in check
        and "jsonb_array_length(evidence_urls) > 0" in check
        for check in checks
    )


def test_handover_signer_ids_reference_users() -> None:
    """Each signer is tied to its booking party; only the renter also has a plain FK.

    The migrations never created a plain ``vendor_signer_id -> users(id)`` FK, so
    the model does not declare one (M3a §7.3, A19): the composite FK to
    ``bookings(id, vendor_id)`` already ties the signer to a user.
    """
    table = _table("handover_records")
    expected = {
        "vendor_signer_id": {"bookings.vendor_id"},
        "renter_signer_id": {"users.id", "bookings.renter_id"},
    }
    for column_name, targets in expected.items():
        assert {fk.target_fullname for fk in table.c[column_name].foreign_keys} == targets


def test_handover_signers_reference_booking_parties() -> None:
    table = _table("handover_records")
    composite_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in table.foreign_key_constraints
        if constraint.name
        in {
            "fk_handover_records_booking_vendor",
            "fk_handover_records_booking_renter",
        }
    }

    assert ("bookings.id", "bookings.vendor_id") in composite_targets
    assert ("bookings.id", "bookings.renter_id") in composite_targets
