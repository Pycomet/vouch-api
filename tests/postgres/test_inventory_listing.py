"""Live PostgreSQL checks for the listing-publication columns (migration 0002).

Moved from ``tests/test_postgres_listing.py`` when migration 0003 replaced the city
and category enums. The visibility, date-availability and restricted-field checks
that lived there are now exercised through the API in ``test_inventory_api.py``;
the trust-tier range and the public index are covered in ``test_migration_0003.py``.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.postgres.conftest import asyncpg_url

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


async def test_listing_columns_enum_and_draft_default(
    migrated_db: str, conn: asyncpg.Connection
) -> None:
    columns = {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'assets' AND column_name IN "
            "('listing_status', 'minimum_trust_tier', 'review_actor_id', "
            "'reviewed_at', 'review_reason')"
        )
    }
    assert columns == {
        "listing_status",
        "minimum_trust_tier",
        "review_actor_id",
        "reviewed_at",
        "review_reason",
    }

    enum_vals = await conn.fetchval(
        "SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder) "
        "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
        "WHERE t.typname = 'listingstatus'"
    )
    assert set(enum_vals) == {"draft", "review", "active", "paused", "rejected"}

    vendor_id = await conn.fetchval(
        "INSERT INTO users (phone, full_name, role, is_active) "
        "VALUES ('+2348000000401', 'Vendor One', 'vendor', true) RETURNING id"
    )
    category_id = await conn.fetchval("SELECT id FROM categories WHERE slug = 'camera'")
    city_id = await conn.fetchval("SELECT id FROM cities WHERE slug = 'abuja'")
    item_id = await conn.fetchval(
        "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
        "VALUES ($1, 'Canon', 'C70', 'B') RETURNING id",
        category_id,
    )
    asset_id = await conn.fetchval(
        "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
        "daily_rate, deposit_amount) VALUES ($1, $2, $3, $4, 75000, 200000) RETURNING id",
        vendor_id,
        item_id,
        city_id,
        category_id,
    )
    status = await conn.fetchval("SELECT listing_status FROM assets WHERE id = $1", asset_id)
    assert status == "draft"


def test_round_trip_through_0001(migrated_db: str, alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "0001_initial_schema")
    command.upgrade(alembic_cfg, "head")

    async def count_cities() -> int:
        connection = await asyncpg.connect(asyncpg_url())
        try:
            return await connection.fetchval("SELECT count(*) FROM cities")
        finally:
            await connection.close()

    assert asyncio.run(count_cities()) == 2
