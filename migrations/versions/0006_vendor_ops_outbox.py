"""Vendor profiles, listing operations, webhook resolution and the outbox (M3a 3).

Revision ID: 0006_vendor_ops_outbox
Revises: 0005_otp_challenges
Create Date: 2026-11-05

One revision, built from parts. ``upgrade()`` runs the parts in order and
``downgrade()`` runs them in reverse.

Part A: vendor profiles (3.1)
-----------------------------
* Enums ``vendorstatus`` (pending, active, suspended) and ``vendorbusinesstype``
  (rental_house, individual).
* ``vendor_profiles``, keyed by ``users.id``. CHECKs: ``active`` needs
  ``verified_at``; ``account_last4`` is four digits; ``suspended`` <=> both
  ``suspended_at`` and ``suspended_reason``. ``recipient_code`` is unique. The full
  bank account number is not stored anywhere.
* Backfill: every user with role ``vendor`` gets an ``active`` profile,
  ``business_type = 'individual'``, ``verified_at = users.created_at``, bank fields
  NULL.

Part B: listing columns (3.2)
-----------------------------
* ``assets.paused_by`` (``vendor`` or ``admin``); ``paused`` <=> ``paused_by`` set.
  Backfill: every paused asset gets ``paused_by = 'admin'`` (mark_lost pauses are
  admin acts).
* Partial unique index ``uq_assets_vendor_item_serial`` on
  ``(vendor_id, catalogue_item_id, lower(serial_number))`` over live assets with a
  serial. If live duplicates exist, the upgrade raises, naming the asset ids, and
  deletes nothing: an operator must resolve them first.

Part C: category consistency (3.3)
----------------------------------
* Backfill first: ``assets.category_id`` is set from the catalogue item where they
  disagree; the number of corrected assets is logged.
* ``uq_catalogue_items_id_category`` on ``catalogue_items (id, category_id)`` and the
  composite FK ``fk_assets_item_category`` onto it.

Part D: webhook event resolution (3.4)
--------------------------------------
* ``webhook_events.resolution`` (returned, reposted, written_off), ``resolved_at``,
  ``resolved_by`` (FK users) and ``resolution_note``: all four or none.

Part E: outbox and OTP (3.5)
----------------------------
* Enum ``outboxstatus`` (pending, sent, dead) and ``notification_outbox``:
  ``sent`` <=> ``sent_at``; ``attempts >= 0``. Partial index ``ix_outbox_due`` on
  ``(next_attempt_at, id)`` over pending rows.
* ``otp_challenges.ip_hash`` (HMAC of the client IP, NULL when unknown) and the
  partial index ``ix_otp_challenges_ip_created``.

Downgrade (lossy where noted)
-----------------------------
It first refuses (raises, nothing changes) while:

* a ``notification_outbox`` row is still ``pending``: dropping the table would
  silently lose a message that was never sent. Sent and dead rows do not block;
* a vendor profile is ``suspended``: 0005 has nowhere to record the suspension,
  so dropping it would silently let the vendor trade again.

Then every object above is dropped. Lossy, by design:

* the rest of ``vendor_profiles`` (status, business and bank details,
  verification) and every sent or dead outbox row;
* ``assets.paused_by`` (paused assets stay paused);
* the webhook resolution columns (the audit log keeps who resolved what);
* ``otp_challenges.ip_hash``;
* the category correction is kept: the old, inconsistent ``category_id`` values
  are not restored.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0006_vendor_ops_outbox"
down_revision: str | Sequence[str] | None = "0005_otp_challenges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

SERIAL_INDEX = "uq_assets_vendor_item_serial"


def upgrade() -> None:
    _upgrade_vendor_profiles()
    _upgrade_listing_columns()
    _upgrade_category_consistency()
    _upgrade_webhook_resolution()
    _upgrade_outbox_and_otp()


def downgrade() -> None:
    _refuse_lossy_downgrade()
    _downgrade_outbox_and_otp()
    _downgrade_webhook_resolution()
    _downgrade_category_consistency()
    _downgrade_listing_columns()
    _downgrade_vendor_profiles()


def _refuse_lossy_downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM notification_outbox WHERE status = 'pending') THEN "
        "RAISE EXCEPTION 'pending notification_outbox rows exist; "
        "send them or mark them dead before downgrading 0006_vendor_ops_outbox'; "
        "END IF; "
        "IF EXISTS (SELECT 1 FROM vendor_profiles WHERE status = 'suspended') THEN "
        "RAISE EXCEPTION 'a suspended vendor exists and 0005 cannot record the suspension; "
        "lift or otherwise resolve it before downgrading 0006_vendor_ops_outbox'; "
        "END IF; END $$"
    )


# ---------------------------------------------------------------------------
# Part A: vendor profiles
# ---------------------------------------------------------------------------

VENDOR_STATUS = postgresql.ENUM(
    "pending", "active", "suspended", name="vendorstatus", create_type=False
)
VENDOR_BUSINESS_TYPE = postgresql.ENUM(
    "rental_house", "individual", name="vendorbusinesstype", create_type=False
)


def _upgrade_vendor_profiles() -> None:
    op.execute("CREATE TYPE vendorstatus AS ENUM ('pending', 'active', 'suspended')")
    op.execute("CREATE TYPE vendorbusinesstype AS ENUM ('rental_house', 'individual')")
    op.create_table(
        "vendor_profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", VENDOR_STATUS, server_default="pending", nullable=False),
        sa.Column("business_type", VENDOR_BUSINESS_TYPE, nullable=False),
        sa.Column("is_sub_broker", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("business_name", sa.Text(), nullable=True),
        sa.Column("business_registration_ref", sa.Text(), nullable=True),
        sa.Column("bank_name", sa.Text(), nullable=True),
        sa.Column("account_name", sa.Text(), nullable=True),
        sa.Column("account_last4", sa.CHAR(4), nullable=True),
        sa.Column("recipient_code", sa.Text(), nullable=True),
        sa.Column("verified_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_vendor_profiles"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_vendor_profiles_user_id"),
        sa.ForeignKeyConstraint(
            ["verified_by"], ["users.id"], name="fk_vendor_profiles_verified_by"
        ),
        sa.UniqueConstraint("recipient_code", name="uq_vendor_profiles_recipient_code"),
        sa.CheckConstraint(
            "status <> 'active' OR verified_at IS NOT NULL",
            name="ck_vendor_profiles_active_verified",
        ),
        sa.CheckConstraint(
            "account_last4 IS NULL OR account_last4 ~ '^[0-9]{4}$'",
            name="ck_vendor_profiles_last4_digits",
        ),
        sa.CheckConstraint(
            "(status = 'suspended') = (suspended_at IS NOT NULL AND suspended_reason IS NOT NULL)",
            name="ck_vendor_profiles_suspension",
        ),
    )
    op.execute(
        "INSERT INTO vendor_profiles (user_id, status, business_type, verified_at) "
        "SELECT id, 'active', 'individual', created_at FROM users WHERE role = 'vendor'"
    )


def _downgrade_vendor_profiles() -> None:
    op.drop_table("vendor_profiles")
    op.execute("DROP TYPE vendorbusinesstype")
    op.execute("DROP TYPE vendorstatus")


# ---------------------------------------------------------------------------
# Part B: listing columns
# ---------------------------------------------------------------------------


def _upgrade_listing_columns() -> None:
    op.add_column("assets", sa.Column("paused_by", sa.Text(), nullable=True))
    op.execute("UPDATE assets SET paused_by = 'admin' WHERE listing_status = 'paused'")
    op.create_check_constraint(
        "ck_assets_paused_by_values", "assets", "paused_by IN ('vendor', 'admin')"
    )
    op.create_check_constraint(
        "ck_assets_paused_by_matches_status",
        "assets",
        "(listing_status = 'paused') = (paused_by IS NOT NULL)",
    )

    # Fail loudly, naming the assets, rather than delete anything. (A DO block, so
    # the check is in the offline SQL as well.)
    op.execute(
        "DO $$ DECLARE n integer; groups text; BEGIN "
        "SELECT count(*), string_agg(format('serial %L: %s', serial, ids), '; ' ORDER BY ids) "
        "INTO n, groups FROM ("
        "  SELECT lower(serial_number) AS serial, "
        "  string_agg(id::text, ', ' ORDER BY created_at, id) AS ids "
        "  FROM assets WHERE deleted_at IS NULL AND serial_number IS NOT NULL "
        "  GROUP BY vendor_id, catalogue_item_id, lower(serial_number) "
        "  HAVING count(*) > 1"
        ") AS duplicates; "
        "IF n > 0 THEN RAISE EXCEPTION "
        f"'cannot create {SERIAL_INDEX}: % group(s) of live assets share a vendor, "
        "catalogue item and serial number (ignoring letter case): %. Correct the serials "
        "or soft-delete the duplicates by hand, then upgrade again; no rows were deleted', "
        "n, groups; "
        "END IF; END $$"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {SERIAL_INDEX} "
        "ON assets (vendor_id, catalogue_item_id, lower(serial_number)) "
        "WHERE deleted_at IS NULL AND serial_number IS NOT NULL"
    )


def _downgrade_listing_columns() -> None:
    op.execute(f"DROP INDEX {SERIAL_INDEX}")
    op.drop_constraint("ck_assets_paused_by_matches_status", "assets", type_="check")
    op.drop_constraint("ck_assets_paused_by_values", "assets", type_="check")
    op.drop_column("assets", "paused_by")


# ---------------------------------------------------------------------------
# Part C: category consistency
# ---------------------------------------------------------------------------


def _upgrade_category_consistency() -> None:
    correct = (
        "UPDATE assets a SET category_id = c.category_id FROM catalogue_items c "
        "WHERE c.id = a.catalogue_item_id AND a.category_id <> c.category_id"
    )
    if context.is_offline_mode():
        op.execute(correct)  # no row count to log when only SQL is emitted
    else:
        corrected = op.get_bind().execute(sa.text(correct)).rowcount
        logger.info(
            "0006_vendor_ops_outbox: corrected category_id on %d asset(s) to match "
            "their catalogue item",
            corrected,
        )
    op.create_unique_constraint(
        "uq_catalogue_items_id_category", "catalogue_items", ["id", "category_id"]
    )
    op.create_foreign_key(
        "fk_assets_item_category",
        "assets",
        "catalogue_items",
        ["catalogue_item_id", "category_id"],
        ["id", "category_id"],
    )


def _downgrade_category_consistency() -> None:
    op.drop_constraint("fk_assets_item_category", "assets", type_="foreignkey")
    op.drop_constraint("uq_catalogue_items_id_category", "catalogue_items", type_="unique")


# ---------------------------------------------------------------------------
# Part D: webhook event resolution
# ---------------------------------------------------------------------------


def _upgrade_webhook_resolution() -> None:
    op.add_column("webhook_events", sa.Column("resolution", sa.Text(), nullable=True))
    op.add_column(
        "webhook_events", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "webhook_events",
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("webhook_events", sa.Column("resolution_note", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_webhook_events_resolved_by", "webhook_events", "users", ["resolved_by"], ["id"]
    )
    op.create_check_constraint(
        "ck_webhook_events_resolution_values",
        "webhook_events",
        "resolution IN ('returned', 'reposted', 'written_off')",
    )
    op.create_check_constraint(
        "ck_webhook_events_resolution_complete",
        "webhook_events",
        "(resolution IS NULL) = (resolved_at IS NULL) "
        "AND (resolution IS NULL) = (resolved_by IS NULL) "
        "AND (resolution IS NULL) = (resolution_note IS NULL)",
    )


def _downgrade_webhook_resolution() -> None:
    op.drop_constraint("ck_webhook_events_resolution_complete", "webhook_events", type_="check")
    op.drop_constraint("ck_webhook_events_resolution_values", "webhook_events", type_="check")
    op.drop_constraint("fk_webhook_events_resolved_by", "webhook_events", type_="foreignkey")
    for column in ("resolution_note", "resolved_by", "resolved_at", "resolution"):
        op.drop_column("webhook_events", column)


# ---------------------------------------------------------------------------
# Part E: outbox and OTP
# ---------------------------------------------------------------------------

OUTBOX_STATUS = postgresql.ENUM("pending", "sent", "dead", name="outboxstatus", create_type=False)


def _upgrade_outbox_and_otp() -> None:
    op.execute("CREATE TYPE outboxstatus AS ENUM ('pending', 'sent', 'dead')")
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),  # BIGSERIAL
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", OUTBOX_STATUS, server_default="pending", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notification_outbox"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notification_outbox_user_id"),
        sa.CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="ck_outbox_sent_at"),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonnegative"),
    )
    op.create_index(
        "ix_outbox_due",
        "notification_outbox",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.add_column("otp_challenges", sa.Column("ip_hash", sa.Text(), nullable=True))
    op.create_index(
        "ix_otp_challenges_ip_created",
        "otp_challenges",
        ["ip_hash", "created_at"],
        postgresql_where=sa.text("ip_hash IS NOT NULL"),
    )


def _downgrade_outbox_and_otp() -> None:
    op.drop_index("ix_otp_challenges_ip_created", table_name="otp_challenges")
    op.drop_column("otp_challenges", "ip_hash")
    op.drop_index("ix_outbox_due", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.execute("DROP TYPE outboxstatus")
