"""Live PostgreSQL tests for migration 0003 (schema alignment).

Part A: reference tables, catalogue and the asset reshaping. Part B: users, trust
tiers, vendor references, the booking state machine and the payment type rename.
Constraint behaviour at head lives in ``test_constraints_0003.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.postgres.conftest import REVISION_0002, Seed0002, asyncpg_url

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

CATEGORY_SLUGS = {"camera", "lens", "lighting", "audio", "drone", "gimbal", "accessory"}
SERIAL_CATEGORIES = {"camera", "lens", "drone", "gimbal", "audio"}
COUNT_CATEGORIES = {"lighting", "accessory"}


async def _fetch(query: str, *args):
    connection = await asyncpg.connect(asyncpg_url())
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _fetch_sync(query: str, *args):
    return asyncio.run(_fetch(query, *args))


def test_upgrade_and_downgrade_on_empty_db(migrated_db: str, alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "-1")
    command.upgrade(alembic_cfg, "head")
    tables = {
        row["table_name"]
        for row in _fetch_sync(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert {"cities", "categories", "catalogue_items", "assets"} <= tables


async def test_reference_slugs_match_portal(migrated_db: str, conn: asyncpg.Connection) -> None:
    cities = {row["slug"] for row in await conn.fetch("SELECT slug FROM cities")}
    assert cities == {"abuja", "lagos"}
    assert await conn.fetchval("SELECT bool_and(is_active) FROM cities")

    rows = await conn.fetch("SELECT slug, checklist_template FROM categories")
    assert {row["slug"] for row in rows} == CATEGORY_SLUGS
    modes = {row["slug"]: row["checklist_template"] for row in rows}
    for slug in SERIAL_CATEGORIES:
        assert '"mode": "serial"' in modes[slug]
    for slug in COUNT_CATEGORIES:
        assert '"mode": "count"' in modes[slug]


async def test_catalogue_search_index_exists(migrated_db: str, conn: asyncpg.Connection) -> None:
    indexdef = await conn.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_catalogue_items_search'"
    )
    assert indexdef is not None
    assert "gin" in indexdef.lower()
    public = await conn.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_assets_public'"
    )
    assert public is not None
    assert "city_id, category_id, daily_rate, created_at, id" in public
    old = await conn.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_assets_public_lagos'"
    )
    assert old == 0


async def test_minimum_trust_tier_accepts_3_rejects_4(
    migrated_db: str, conn: asyncpg.Connection
) -> None:
    vendor_id = await conn.fetchval(
        "INSERT INTO users (phone, full_name, role, is_active) "
        "VALUES ('+2348000000201', 'Tier Vendor', 'vendor', true) RETURNING id"
    )
    category_id = await conn.fetchval("SELECT id FROM categories WHERE slug = 'camera'")
    city_id = await conn.fetchval("SELECT id FROM cities WHERE slug = 'abuja'")
    item_id = await conn.fetchval(
        "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
        "VALUES ($1, 'Sony', 'FX3', 'C') RETURNING id",
        category_id,
    )
    insert = (
        "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
        "daily_rate, deposit_amount, minimum_trust_tier) "
        "VALUES ($1, $2, $3, $4, 75000, 200000, $5) RETURNING id"
    )
    asset_id = await conn.fetchval(insert, vendor_id, item_id, city_id, category_id, 3)
    assert asset_id is not None
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(insert, vendor_id, item_id, city_id, category_id, 4)


async def test_asset_rate_and_grade_checks(migrated_db: str, conn: asyncpg.Connection) -> None:
    vendor_id = await conn.fetchval(
        "INSERT INTO users (phone, full_name, role, is_active) "
        "VALUES ('+2348000000202', 'Check Vendor', 'vendor', true) RETURNING id"
    )
    category_id = await conn.fetchval("SELECT id FROM categories WHERE slug = 'lens'")
    city_id = await conn.fetchval("SELECT id FROM cities WHERE slug = 'abuja'")
    item_id = await conn.fetchval(
        "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
        "VALUES ($1, 'Sigma', '18-35', 'A') RETURNING id",
        category_id,
    )
    base = (
        "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
        "daily_rate, deposit_amount, {col}) VALUES ($1, $2, $3, $4, 10000, 0, $5)"
    )
    args = (vendor_id, item_id, city_id, category_id)
    for col, bad in (("weekend_rate", 0), ("weekly_rate", -1), ("condition_grade", "mint")):
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(base.format(col=col), *args, bad)
    row = await conn.fetchrow(
        "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
        "daily_rate, deposit_amount, condition_grade) "
        "VALUES ($1, $2, $3, $4, 10000, 0, 'good') "
        "RETURNING name, included_accessories, fulfilment, minimum_trust_tier",
        *args,
    )
    assert row["name"] is None
    assert row["included_accessories"] == "[]"
    assert row["fulfilment"] == '{"pickup": true, "delivery": false}'


def test_upgrade_from_seeded_0002(db_at_0002: Seed0002, alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")

    asset = _fetch_sync(
        "SELECT ci.slug AS city, ca.slug AS category, a.minimum_trust_tier, a.name, "
        "c.make, c.model, c.replacement_value_band AS band, "
        "c.specs->>'needs_review' AS needs_review, c.category_id = a.category_id AS same_cat "
        "FROM assets a "
        "JOIN cities ci ON ci.id = a.city_id "
        "JOIN categories ca ON ca.id = a.category_id "
        "JOIN catalogue_items c ON c.id = a.catalogue_item_id "
        "WHERE a.id = $1",
        db_at_0002.asset_id,
    )[0]
    assert asset["city"] == "lagos"
    assert asset["category"] == "accessory"
    assert asset["needs_review"] == "true"
    assert asset["band"] == "B"
    assert asset["minimum_trust_tier"] == 2
    assert asset["make"] == "Unknown"
    assert asset["model"] == f"Seed Rig #{str(db_at_0002.asset_id)[:8]}"
    assert asset["name"] == "Seed Rig"
    assert asset["same_cat"]

    # Part B: trust tiers (A6).
    kyc = {
        row["id"]: row
        for row in _fetch_sync(
            "SELECT id, status::text AS status, tier, verified_at FROM kyc_profiles"
        )
    }
    old_two = kyc[db_at_0002.extra["kyc_tier2"]]
    assert (old_two["status"], old_two["tier"]) == ("verified", 1)
    assert old_two["verified_at"] is not None
    old_one = kyc[db_at_0002.extra["kyc_tier1"]]
    assert (old_one["status"], old_one["tier"], old_one["verified_at"]) == ("submitted", 0, None)

    # Part B: booking state machine.
    bookings = {
        row["id"]: row
        for row in _fetch_sync(
            "SELECT id, status::text AS status, hold_expires_at - created_at AS hold, "
            "price_snapshot, fulfilment_choice FROM bookings"
        )
    }
    requested = bookings[db_at_0002.extra["booking_pending"]]
    assert requested["status"] == "requested"
    assert requested["hold"] == datetime.timedelta(hours=4)
    assert json.loads(requested["price_snapshot"]) == {
        "basis": "legacy",
        "rental": "250000.00",
        "deposit": "150000.00",
    }
    assert requested["fulfilment_choice"] == "pickup"
    cancelled = bookings[db_at_0002.extra["booking_cancelled"]]
    assert cancelled["status"] == "cancelled_by_renter"
    assert cancelled["hold"] is None
    assert cancelled["price_snapshot"] is not None

    payment_types = {
        row["v"] for row in _fetch_sync("SELECT unnest(enum_range(NULL::paymenttype))::text AS v")
    }
    assert "damage_contribution" in payment_types
    assert "protection_waiver" not in payment_types


def test_downgrade_from_seeded_restores_0002_shape(
    db_at_0002: Seed0002, alembic_cfg: Config
) -> None:
    command.upgrade(alembic_cfg, "head")
    # A listing created after 0003 with no display name and tier 3 must survive.
    asyncio.run(_insert_post_0003_asset(db_at_0002.vendor_id, db_at_0002.renter_id))
    command.downgrade(alembic_cfg, REVISION_0002)

    enums = {
        row["typname"]
        for row in _fetch_sync(
            "SELECT typname FROM pg_type WHERE typname IN ('city', 'assetcategory')"
        )
    }
    assert enums == {"city", "assetcategory"}

    seeded = _fetch_sync(
        "SELECT category::text AS category, city::text AS city FROM assets WHERE id = $1",
        db_at_0002.asset_id,
    )[0]
    assert seeded["category"] == "other"
    assert seeded["city"] == "lagos"

    created = _fetch_sync(
        "SELECT name, category::text AS category, city::text AS city, minimum_trust_tier "
        "FROM assets WHERE id <> $1",
        db_at_0002.asset_id,
    )[0]
    assert created["name"] == "Sony FX3"
    assert created["category"] == "camera"
    assert created["city"] == "abuja"
    assert created["minimum_trust_tier"] == 2

    indexes = {
        row["indexname"]
        for row in _fetch_sync("SELECT indexname FROM pg_indexes WHERE tablename = 'assets'")
    }
    assert "ix_assets_public_lagos" in indexes
    assert "ix_assets_public" not in indexes

    tables = {
        row["table_name"]
        for row in _fetch_sync(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert not {"cities", "categories", "catalogue_items", "vendor_references"} & tables

    # Part B: the old booking statuses and kyc tiers come back.
    statuses = {
        row["id"]: row["status"]
        for row in _fetch_sync("SELECT id, status::text AS status FROM bookings")
    }
    assert statuses[db_at_0002.extra["booking_pending"]] == "pending"
    assert statuses[db_at_0002.extra["booking_cancelled"]] == "cancelled"
    assert sorted(statuses.values()) == ["active", "cancelled", "cancelled", "pending"]
    enum_values = [
        row["v"] for row in _fetch_sync("SELECT unnest(enum_range(NULL::bookingstatus))::text AS v")
    ]
    assert enum_values == ["pending", "confirmed", "active", "completed", "cancelled", "disputed"]
    tiers = {
        row["id"]: (row["status"], row["tier"])
        for row in _fetch_sync("SELECT id, status::text AS status, tier FROM kyc_profiles")
    }
    # Lossy: old tier 2 returns; old tier 1 stays submitted at tier 0.
    assert tiers[db_at_0002.extra["kyc_tier2"]] == ("verified", 2)
    assert tiers[db_at_0002.extra["kyc_tier1"]] == ("submitted", 0)
    constraints = {
        row["conname"]
        for row in _fetch_sync(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'bookings'::regclass"
        )
    }
    assert "ex_bookings_no_active_asset_overlap" in constraints
    assert "ex_bookings_no_live_overlap" not in constraints
    columns = {
        row["column_name"]
        for row in _fetch_sync(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name IN ('bookings', 'users')"
        )
    }
    assert not {"price_snapshot", "hold_expires_at", "claim_amount", "restricted_at"} & columns
    payment_types = {
        row["v"] for row in _fetch_sync("SELECT unnest(enum_range(NULL::paymenttype))::text AS v")
    }
    assert "protection_waiver" in payment_types

    # assets.name, city and category are NOT NULL again.
    nullable = {
        row["column_name"]: row["is_nullable"]
        for row in _fetch_sync(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'assets' AND column_name IN ('name', 'city', 'category')"
        )
    }
    assert nullable == {"name": "NO", "city": "NO", "category": "NO"}

    # The 0-2 trust-tier CHECK is back: tier 3 is rejected.
    with pytest.raises(asyncpg.CheckViolationError):
        _fetch_sync(
            "INSERT INTO assets (vendor_id, name, category, daily_rate, deposit_amount, "
            "city, listing_status, minimum_trust_tier) "
            "VALUES ($1, 'Tier Three', 'camera', 1000, 1000, 'abuja', 'active', 3) "
            "RETURNING id",
            db_at_0002.vendor_id,
        )


async def _insert_post_0003_asset(vendor_id, renter_id=None) -> None:
    connection = await asyncpg.connect(asyncpg_url())
    try:
        category_id = await connection.fetchval("SELECT id FROM categories WHERE slug = 'camera'")
        city_id = await connection.fetchval("SELECT id FROM cities WHERE slug = 'abuja'")
        item_id = await connection.fetchval(
            "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
            "VALUES ($1, 'Sony', 'FX3', 'C') RETURNING id",
            category_id,
        )
        asset_id = await connection.fetchval(
            "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
            "daily_rate, deposit_amount, minimum_trust_tier) "
            "VALUES ($1, $2, $3, $4, 75000, 200000, 3) RETURNING id",
            vendor_id,
            item_id,
            city_id,
            category_id,
        )
        if renter_id is not None:
            # New-only statuses must map back on downgrade (returned -> active,
            # expired -> cancelled).
            for status, starts in (("returned", "2027-01-01"), ("expired", "2027-01-01")):
                await connection.execute(
                    "INSERT INTO bookings (asset_id, renter_id, vendor_id, starts_on, ends_on, "
                    "status, total_rental_fee, deposit_amount, price_snapshot, "
                    "fulfilment_choice, claim_window_ends_at) "
                    "VALUES ($1, $2, $3, $4::date, $4::date + 2, $5, 1000, 0, '{}', 'pickup', "
                    "now())",
                    asset_id,
                    renter_id,
                    vendor_id,
                    datetime.date.fromisoformat(starts),
                    status,
                )
    finally:
        await connection.close()
