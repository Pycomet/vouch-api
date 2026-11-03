"""Live PostgreSQL migration and critical-constraint integration test."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app.core.config import get_settings

ROOT = Path(__file__).parents[1]
DATABASE_URL = os.getenv("VOUCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def _asyncpg_url() -> str:
    assert DATABASE_URL is not None
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _exercise_constraints() -> None:
    connection = await asyncpg.connect(_asyncpg_url())
    try:
        tables = await connection.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('users', 'kyc_profiles', 'assets', 'availability_windows', "
            "'bookings', 'payments', 'handover_records')"
        )
        assert tables == 7

        renter_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ('+2348000000001', 'Renter', 'renter', true) RETURNING id"
        )
        vendor_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ('+2348000000002', 'Vendor', 'vendor', true) RETURNING id"
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO kyc_profiles "
                "(user_id, provider, status, tier, verified_at) "
                "VALUES ($1, 'test', 'verified', 0, now())",
                renter_id,
            )

        category_id = await connection.fetchval("SELECT id FROM categories WHERE slug = 'camera'")
        item_id = await connection.fetchval(
            "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
            "VALUES ($1, 'Sony', 'FX3', 'B') RETURNING id",
            category_id,
        )
        asset_id = await connection.fetchval(
            "INSERT INTO assets "
            "(vendor_id, catalogue_item_id, category_id, city_id, daily_rate, deposit_amount) "
            "VALUES ($1, $2, $3, (SELECT id FROM cities WHERE slug = 'abuja'), "
            "75000, 200000) RETURNING id",
            vendor_id,
            item_id,
            category_id,
        )
        await connection.execute(
            "INSERT INTO bookings "
            "(asset_id, renter_id, vendor_id, starts_on, ends_on, status, "
            "total_rental_fee, deposit_amount, price_snapshot, fulfilment_choice) "
            "VALUES ($1, $2, $3, DATE '2026-10-10', DATE '2026-10-12', "
            "'confirmed', 225000, 200000, '{}', 'pickup')",
            asset_id,
            renter_id,
            vendor_id,
        )

        with pytest.raises(asyncpg.ExclusionViolationError):
            await connection.execute(
                "INSERT INTO bookings "
                "(asset_id, renter_id, vendor_id, starts_on, ends_on, status, "
                "total_rental_fee, deposit_amount, price_snapshot, fulfilment_choice) "
                "VALUES ($1, $2, $3, DATE '2026-10-12', DATE '2026-10-14', "
                "'in_progress', 225000, 200000, '{}', 'pickup')",
                asset_id,
                renter_id,
                vendor_id,
            )
    finally:
        await connection.close()


def test_initial_migration_round_trip_and_database_constraints(monkeypatch) -> None:
    assert DATABASE_URL is not None
    monkeypatch.setenv("VOUCH_DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    config = _alembic_config()

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    asyncio.run(_exercise_constraints())
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    get_settings.cache_clear()
