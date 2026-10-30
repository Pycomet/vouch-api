"""The upfront booking payment type (A14).

Revision ID: 0004_booking_payment
Revises: 0003_schema_alignment
Create Date: 2026-10-30

The renter now pays the deposit, the damage contribution, the rental and the
delivery fee in ONE payment to confirm a booking. That payment row has the new
``paymenttype`` value ``booking``.

Upgrade: ``ALTER TYPE paymenttype ADD VALUE 'booking'`` (nothing uses the value in
the same transaction).

Downgrade: Postgres cannot drop an enum value, so the type is recreated without
it. It first refuses (raises) while any ``payments`` row still has type
``booking``: those rows would have no value to go back to. The three CHECKs that
compare ``type`` with a literal are dropped before the swap and recreated after it
(their stored expressions name the type being replaced).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_booking_payment"
down_revision: str | Sequence[str] | None = "0003_schema_alignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_VALUE = "booking"
# The type's values before this revision, in order (0001, with the A4 rename of 0003).
PREVIOUS_VALUES = ("deposit", "rental_balance", "payout", "refund", "damage_contribution")

# The payments CHECKs that compare ``type``, as the model defines them.
TYPE_CHECKS = {
    "ck_payments_payout_fields_required": (
        "type <> 'payout' OR (vendor_recipient_code IS NOT NULL AND platform_fee IS NOT NULL)"
    ),
    "ck_payments_deposit_lifecycle_type": (
        "(type = 'deposit' AND deposit_status IS NOT NULL) OR "
        "(type <> 'deposit' AND deposit_status IS NULL AND deposit_held_at IS NULL "
        "AND deposit_released_at IS NULL AND deposit_claimed_at IS NULL)"
    ),
    "ck_payments_payout_lifecycle_type": (
        "(type = 'payout' AND payout_status IS NOT NULL) OR "
        "(type <> 'payout' AND payout_status IS NULL AND payout_eligible_at IS NULL "
        "AND paid_at IS NULL)"
    ),
}


def upgrade() -> None:
    op.execute(f"ALTER TYPE paymenttype ADD VALUE '{NEW_VALUE}'")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM payments WHERE type::text = '{NEW_VALUE}') THEN "
        "RAISE EXCEPTION 'payments of type booking exist; "
        "remove or convert them before downgrading 0004_booking_payment'; "
        "END IF; END $$"
    )
    for name in TYPE_CHECKS:
        op.execute(f"ALTER TABLE payments DROP CONSTRAINT {name}")
    values = ", ".join(f"'{value}'" for value in PREVIOUS_VALUES)
    op.execute("ALTER TYPE paymenttype RENAME TO paymenttype_old")
    op.execute(f"CREATE TYPE paymenttype AS ENUM ({values})")
    op.execute(
        "ALTER TABLE payments ALTER COLUMN type TYPE paymenttype USING type::text::paymenttype"
    )
    op.execute("DROP TYPE paymenttype_old")
    for name, condition in TYPE_CHECKS.items():
        op.execute(f"ALTER TABLE payments ADD CONSTRAINT {name} CHECK ({condition})")
