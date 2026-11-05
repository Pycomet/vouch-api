"""Live PostgreSQL tests for migration 0006 (vendor profiles, listing ops, outbox; M3a 3)."""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.postgres.conftest import (
    REVISION_0005,
    SEED_0005_VENDOR_CREATED_AT,
    Seed0005,
    asyncpg_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

REVISION_0006 = "0006_vendor_ops_outbox"
NEW_TABLES = {"vendor_profiles", "notification_outbox"}
NEW_TYPES = {"vendorstatus", "vendorbusinesstype", "outboxstatus"}


async def _fetch(query: str, *args):
    connection = await asyncpg.connect(asyncpg_url())
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _fetch_sync(query: str, *args):
    return asyncio.run(_fetch(query, *args))


def _value(query: str, *args):
    rows = _fetch_sync(query, *args)
    return rows[0][0] if rows else None


def _revision() -> str:
    return _value("SELECT version_num FROM alembic_version")


def _shape() -> dict[str, frozenset]:
    """Every table, column, constraint, index and enum type in ``public``."""
    columns = _fetch_sync(
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public'"
    )
    constraints = _fetch_sync(
        "SELECT conrelid::regclass::text AS t, conname, contype, "
        "pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE connamespace = 'public'::regnamespace AND conrelid <> 0"
    )
    indexes = _fetch_sync(
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
    )
    types = _fetch_sync(
        "SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) AS labels "
        "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
        "WHERE t.typnamespace = 'public'::regnamespace GROUP BY t.typname"
    )
    return {
        "columns": frozenset(tuple(row) for row in columns),
        "constraints": frozenset(tuple(row) for row in constraints),
        "indexes": frozenset(tuple(row) for row in indexes),
        "types": frozenset(tuple(row) for row in types),
    }


def _tables() -> set[str]:
    rows = _fetch_sync("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows}


def _columns(table: str) -> set[str]:
    rows = _fetch_sync(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {row["column_name"] for row in rows}


def _indexes(table: str) -> set[str]:
    rows = _fetch_sync("SELECT indexname FROM pg_indexes WHERE tablename = $1", table)
    return {row["indexname"] for row in rows}


def _types() -> set[str]:
    rows = _fetch_sync("SELECT typname FROM pg_type WHERE typnamespace = 'public'::regnamespace")
    return {row["typname"] for row in rows}


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


def test_migration_0006_roundtrip_on_an_empty_database(
    migrated_db: str, alembic_cfg: Config
) -> None:
    assert _revision() == REVISION_0006
    assert NEW_TABLES <= _tables()
    assert NEW_TYPES <= _types()
    assert "paused_by" in _columns("assets")
    assert {"resolution", "resolved_at", "resolved_by", "resolution_note"} <= _columns(
        "webhook_events"
    )
    assert "ip_hash" in _columns("otp_challenges")
    assert "uq_assets_vendor_item_serial" in _indexes("assets")
    assert "ix_outbox_due" in _indexes("notification_outbox")
    assert "ix_otp_challenges_ip_created" in _indexes("otp_challenges")

    command.downgrade(alembic_cfg, "-1")
    assert _revision() == REVISION_0005
    assert not NEW_TABLES & _tables()
    assert not NEW_TYPES & _types()
    assert "paused_by" not in _columns("assets")
    assert "resolution" not in _columns("webhook_events")
    assert "ip_hash" not in _columns("otp_challenges")

    command.upgrade(alembic_cfg, "head")
    assert NEW_TABLES <= _tables()


def test_migration_0006_downgrade_restores_the_0005_shape_on_an_empty_database(
    migrated_db: str, alembic_cfg: Config
) -> None:
    command.downgrade(alembic_cfg, REVISION_0005)
    before = _shape()
    command.upgrade(alembic_cfg, REVISION_0006)
    command.downgrade(alembic_cfg, REVISION_0005)
    assert _shape() == before


# ---------------------------------------------------------------------------
# Seeded at 0005
# ---------------------------------------------------------------------------


def test_upgrade_backfills_vendor_profiles(db_at_0005: Seed0005, alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, REVISION_0006)

    rows = _fetch_sync("SELECT * FROM vendor_profiles")
    assert [row["user_id"] for row in rows] == [db_at_0005.vendor_id]  # not the renter
    profile = rows[0]
    assert profile["status"] == "active"
    assert profile["business_type"] == "individual"
    assert profile["is_sub_broker"] is False
    assert profile["verified_at"] == SEED_0005_VENDOR_CREATED_AT
    assert profile["verified_at"] == _value(
        "SELECT created_at FROM users WHERE id = $1", db_at_0005.vendor_id
    )
    for column in (
        "bank_name",
        "account_name",
        "account_last4",
        "recipient_code",
        "verified_by",
        "suspended_at",
        "suspended_reason",
        "business_name",
        "business_registration_ref",
    ):
        assert profile[column] is None, column


def test_upgrade_marks_paused_assets_as_admin_pauses(
    db_at_0005: Seed0005, alembic_cfg: Config
) -> None:
    command.upgrade(alembic_cfg, REVISION_0006)

    rows = _fetch_sync("SELECT id, listing_status::text AS s, paused_by FROM assets")
    paused_by = {row["id"]: row["paused_by"] for row in rows}
    assert paused_by == {
        db_at_0005.active_asset_id: None,
        db_at_0005.paused_asset_id: "admin",
        db_at_0005.mismatched_asset_id: None,
    }


def test_upgrade_corrects_a_mismatched_category_and_logs_the_count(
    db_at_0005: Seed0005, alembic_cfg: Config, capfd: pytest.CaptureFixture[str]
) -> None:
    command.upgrade(alembic_cfg, REVISION_0006)

    category = _value(
        "SELECT category_id FROM assets WHERE id = $1", db_at_0005.mismatched_asset_id
    )
    assert category == db_at_0005.camera_category_id
    # Nothing else moved.
    assert (
        _value("SELECT count(*) FROM assets WHERE category_id <> $1", db_at_0005.camera_category_id)
        == 0
    )
    assert "corrected category_id on 1 asset(s)" in capfd.readouterr().err


def test_upgrade_keeps_bookings_and_webhook_events(
    db_at_0005: Seed0005, alembic_cfg: Config
) -> None:
    command.upgrade(alembic_cfg, REVISION_0006)

    bookings = _fetch_sync("SELECT id FROM bookings")
    assert {row["id"] for row in bookings} == set(db_at_0005.booking_ids)
    events = _fetch_sync(
        "SELECT id, resolution, resolved_at, resolved_by, resolution_note FROM webhook_events"
    )
    assert {row["id"] for row in events} == set(db_at_0005.webhook_event_ids)
    assert all(
        row[column] is None
        for row in events
        for column in ("resolution", "resolved_at", "resolved_by", "resolution_note")
    )


def test_downgrade_restores_the_0005_shape_on_the_seeded_database(
    db_at_0005: Seed0005, alembic_cfg: Config
) -> None:
    before = _shape()
    counts = {
        table: _value(f'SELECT count(*) FROM "{table}"')
        for table in ("users", "assets", "bookings", "webhook_events", "catalogue_items")
    }
    command.upgrade(alembic_cfg, REVISION_0006)

    command.downgrade(alembic_cfg, "-1")

    assert _revision() == REVISION_0005
    assert _shape() == before
    for table, count in counts.items():
        assert _value(f'SELECT count(*) FROM "{table}"') == count, table
    # The paused asset is still paused; only the attribution column went away.
    assert (
        _value("SELECT listing_status::text FROM assets WHERE id = $1", db_at_0005.paused_asset_id)
        == "paused"
    )
    # And the round trip works again.
    command.upgrade(alembic_cfg, REVISION_0006)
    assert _value("SELECT count(*) FROM vendor_profiles") == 1


# ---------------------------------------------------------------------------
# Duplicate serials refuse the upgrade
# ---------------------------------------------------------------------------


def _duplicate_serial(seed: Seed0005, *, deleted: bool = False) -> None:
    """A second live asset of the seed vendor and item, serial differing only in case."""
    _fetch_sync(
        "INSERT INTO assets (vendor_id, catalogue_item_id, category_id, city_id, daily_rate, "
        "deposit_amount, serial_number, deleted_at) "
        "SELECT vendor_id, catalogue_item_id, category_id, city_id, daily_rate, deposit_amount, "
        "'sn-seed-1', CASE WHEN $2 THEN now() END FROM assets WHERE id = $1",
        seed.active_asset_id,
        deleted,
    )


def test_upgrade_fails_loudly_on_duplicate_serials_and_deletes_nothing(
    db_at_0005: Seed0005, alembic_cfg: Config
) -> None:
    _duplicate_serial(db_at_0005)
    assets_before = _value("SELECT count(*) FROM assets")

    with pytest.raises(Exception, match="uq_assets_vendor_item_serial") as failure:
        command.upgrade(alembic_cfg, REVISION_0006)
    message = str(failure.value)
    assert "share a vendor, catalogue item and serial number" in message
    assert str(db_at_0005.active_asset_id) in message
    assert "no rows were deleted" in message

    # The whole revision rolled back: still 0005, no row gone.
    assert _revision() == REVISION_0005
    assert not NEW_TABLES & _tables()
    assert _value("SELECT count(*) FROM assets") == assets_before

    # Once the duplicate is soft-deleted (by hand), the upgrade goes through.
    _fetch_sync("UPDATE assets SET deleted_at = now() WHERE serial_number = 'sn-seed-1'")
    command.upgrade(alembic_cfg, REVISION_0006)
    assert _revision() == REVISION_0006


def test_upgrade_ignores_a_soft_deleted_duplicate_serial(
    db_at_0005: Seed0005, alembic_cfg: Config
) -> None:
    _duplicate_serial(db_at_0005, deleted=True)
    command.upgrade(alembic_cfg, REVISION_0006)
    assert _revision() == REVISION_0006


# ---------------------------------------------------------------------------
# Downgrade refuses to drop state the old schema cannot hold
# ---------------------------------------------------------------------------


def test_downgrade_refused_while_an_outbox_row_is_pending(
    migrated_db: str, alembic_cfg: Config
) -> None:
    _fetch_sync("INSERT INTO notification_outbox (template) VALUES ('ops.webhook_failed')")
    with pytest.raises(Exception, match="pending notification_outbox"):
        command.downgrade(alembic_cfg, "-1")
    assert _revision() == REVISION_0006

    # Sent and dead rows are history; they do not block.
    _fetch_sync("UPDATE notification_outbox SET status = 'dead'")
    command.downgrade(alembic_cfg, "-1")
    assert _revision() == REVISION_0005


def test_downgrade_refused_while_a_vendor_is_suspended(
    migrated_db: str, alembic_cfg: Config
) -> None:
    vendor_id = _value(
        "INSERT INTO users (phone, full_name, role, is_active) "
        "VALUES ('+2348000000601', 'Suspended Vendor', 'vendor', true) RETURNING id"
    )
    _fetch_sync(
        "INSERT INTO vendor_profiles (user_id, status, business_type, suspended_at, "
        "suspended_reason) VALUES ($1, 'suspended', 'individual', now(), 'Fraud report')",
        vendor_id,
    )
    with pytest.raises(Exception, match="suspended vendor"):
        command.downgrade(alembic_cfg, "-1")
    assert _revision() == REVISION_0006

    _fetch_sync(
        "UPDATE vendor_profiles SET status = 'pending', suspended_at = NULL, "
        "suspended_reason = NULL"
    )
    command.downgrade(alembic_cfg, "-1")
    assert _revision() == REVISION_0005
