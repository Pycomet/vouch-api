"""Phone OTP challenges (spec 4, R26, A10).

Revision ID: 0005_otp_challenges
Revises: 0004_booking_payment
Create Date: 2026-11-02

Upgrade:

* ``otp_challenges``: one row per code sent. Only an HMAC-SHA256 of the code is
  stored (``code_hash``), never the code. ``attempts`` counts wrong guesses
  (CHECK 0..5; at 5 the challenge is exhausted), ``consumed_at`` marks the one
  successful use. Index ``(phone, created_at)`` serves both the latest-challenge
  lookup and the rolling-hour request count.
* ``users.phone_verified_at``: when the user last proved the phone (nullable).
* ``users.full_name`` becomes nullable: a user created at first OTP verify may
  not have given a name yet.

Downgrade drops the table and the column, and makes ``full_name`` NOT NULL again.
It first refuses (raises) while any user has no name: those rows would have no
value to go back to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_otp_challenges"
down_revision: str | Sequence[str] | None = "0004_booking_payment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "otp_challenges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_otp_challenges"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 5", name="ck_otp_challenges_attempts_range"),
    )
    op.create_index("ix_otp_challenges_phone_created_at", "otp_challenges", ["phone", "created_at"])
    op.add_column(
        "users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.alter_column("users", "full_name", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM users WHERE full_name IS NULL) THEN "
        "RAISE EXCEPTION 'users without a full_name exist; "
        "name them before downgrading 0005_otp_challenges'; "
        "END IF; END $$"
    )
    op.alter_column("users", "full_name", existing_type=sa.String(), nullable=False)
    op.drop_column("users", "phone_verified_at")
    op.drop_index("ix_otp_challenges_phone_created_at", table_name="otp_challenges")
    op.drop_table("otp_challenges")
