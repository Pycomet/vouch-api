"""Model structure tests for payments (no live DB)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum
from sqlalchemy.dialects.postgresql import JSONB

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.models.payment import DepositStatus, PaymentType, PayoutStatus


def _table(name: str):
    return Base.metadata.tables[name]


def test_payments_table_in_metadata() -> None:
    assert "payments" in Base.metadata.tables


def test_payments_type_is_enum() -> None:
    col = _table("payments").c["type"]
    assert isinstance(col.type, Enum)


def test_payments_metadata_is_jsonb() -> None:
    col = _table("payments").c["metadata"]
    assert isinstance(col.type, JSONB)


def test_payments_booking_id_fk_references_bookings() -> None:
    col = _table("payments").c["booking_id"]
    fk_targets = {fk.target_fullname for fk in col.foreign_keys}
    assert "bookings.id" in fk_targets


def test_payment_amount_is_positive() -> None:
    checks = {
        str(constraint.sqltext)
        for constraint in _table("payments").constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "amount > 0" in checks


def test_deposit_lifecycle_is_forward_only_from_held() -> None:
    assert DepositStatus.held.can_transition_to(DepositStatus.released)
    assert DepositStatus.held.can_transition_to(DepositStatus.claimed)
    assert not DepositStatus.released.can_transition_to(DepositStatus.held)
    assert not DepositStatus.claimed.can_transition_to(DepositStatus.held)


def test_payout_lifecycle_is_forward_only_until_paid() -> None:
    assert PayoutStatus.pending.can_transition_to(PayoutStatus.eligible)
    assert PayoutStatus.eligible.can_transition_to(PayoutStatus.paid)
    assert not PayoutStatus.pending.can_transition_to(PayoutStatus.paid)
    assert not PayoutStatus.paid.can_transition_to(PayoutStatus.eligible)


def test_payment_persists_deposit_and_payout_lifecycle() -> None:
    table = _table("payments")

    assert isinstance(table.c["deposit_status"].type, Enum)
    assert isinstance(table.c["payout_status"].type, Enum)
    assert table.c["deposit_held_at"].nullable
    assert table.c["deposit_released_at"].nullable
    assert table.c["deposit_claimed_at"].nullable
    assert table.c["payout_eligible_at"].nullable
    assert table.c["paid_at"].nullable


def test_payment_persists_paystack_split_and_settlement_fields() -> None:
    table = _table("payments")
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert table.c["vendor_recipient_code"].nullable
    assert table.c["vendor_subaccount_code"].nullable
    assert table.c["platform_fee"].nullable
    assert table.c["settlement_reference"].nullable
    assert table.c["settled_at"].nullable
    assert checks["ck_payments_payout_fields_required"] == (
        "type <> 'payout' OR (vendor_recipient_code IS NOT NULL AND platform_fee IS NOT NULL)"
    )
    assert checks["ck_payments_platform_fee_nonnegative"] == (
        "platform_fee IS NULL OR platform_fee >= 0"
    )


def test_payment_lifecycle_columns_match_transaction_type() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in _table("payments").constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "type = 'deposit'" in checks["ck_payments_deposit_lifecycle_type"]
    assert "deposit_status IS NOT NULL" in checks["ck_payments_deposit_lifecycle_type"]
    assert "type = 'payout'" in checks["ck_payments_payout_lifecycle_type"]
    assert "payout_status IS NOT NULL" in checks["ck_payments_payout_lifecycle_type"]
    assert "deposit_released_at IS NOT NULL" in checks["ck_payments_deposit_status_timestamps"]
    assert "deposit_claimed_at IS NOT NULL" in checks["ck_payments_deposit_status_timestamps"]
    assert "payout_eligible_at IS NOT NULL" in checks["ck_payments_payout_status_timestamps"]
    assert "paid_at IS NOT NULL" in checks["ck_payments_payout_status_timestamps"]


def test_payment_type_uses_damage_contribution() -> None:
    assert PaymentType.damage_contribution.value == "damage_contribution"
    assert "protection_waiver" not in {member.value for member in PaymentType}
    assert "damage_contribution" in _table("payments").c["type"].type.enums
