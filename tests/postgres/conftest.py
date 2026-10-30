"""Shared fixtures for live PostgreSQL tests of migration 0003 and the inventory.

Every fixture here needs ``VOUCH_TEST_DATABASE_URL``; tests are skipped when it is
absent (the same pattern as ``tests/test_postgres_migration.py``).

Fixtures
--------
``migrated_db``
    Drops the schema to ``base`` and upgrades to ``head``. Yields the asyncpg URL.
``db_at_0002``
    Drops the schema to ``base``, upgrades to ``0002_listing_publication_fields`` and
    seeds it with raw SQL (see :func:`seed_0002`). Yields a :class:`Seed0002` with the
    ids of the seeded rows. Later tasks extend :func:`seed_0002` and ``Seed0002``.
``conn``
    An asyncpg connection to the test database. Request it after ``migrated_db`` or
    ``db_at_0002`` so it sees the prepared schema.
``db_session``
    An ``AsyncSession`` on a fresh ``head`` schema, inside one outer transaction
    that is rolled back at teardown (the append-only tables cannot be truncated).
    ``SET CONSTRAINTS ALL IMMEDIATE`` runs first, so the deferred ledger balance
    trigger fires at the end of each INSERT statement instead of at a COMMIT that
    never comes. ``session.commit()`` inside a test only releases a SAVEPOINT.
    Row factories for it live in ``tests/postgres/factories.py``.
``session_factory``
    An ``async_sessionmaker`` on a fresh ``head`` schema whose sessions really
    commit, for code that owns its transactions (timers, races).

Every fixture that moves the schema leaves the database at ``head`` when it finishes,
so later test files still work.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

ROOT = Path(__file__).parents[2]
DATABASE_URL = os.getenv("VOUCH_TEST_DATABASE_URL")
REVISION_0002 = "0002_listing_publication_fields"


def asyncpg_url() -> str:
    assert DATABASE_URL is not None
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


@pytest.fixture
def alembic_cfg(monkeypatch) -> Iterator[Config]:
    """Alembic config pointed at the test database (skips without one)."""
    if not DATABASE_URL:
        pytest.skip("VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks")
    monkeypatch.setenv("VOUCH_DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    try:
        yield alembic_config()
    finally:
        get_settings.cache_clear()


@pytest.fixture
def migrated_db(alembic_cfg: Config) -> Iterator[str]:
    """A fresh schema at ``head``."""
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    try:
        yield asyncpg_url()
    finally:
        command.upgrade(alembic_cfg, "head")


# ---------------------------------------------------------------------------
# Seeded 0002 database
# ---------------------------------------------------------------------------


@dataclass
class Seed0002:
    """Ids of the rows :func:`seed_0002` inserted. Extend as later parts need more."""

    renter_id: uuid.UUID
    vendor_id: uuid.UUID
    asset_id: uuid.UUID
    extra: dict[str, uuid.UUID] = field(default_factory=dict)


async def seed_0002(url: str) -> Seed0002:
    """Seed a database at revision 0002 with raw SQL (no ORM: models track head).

    Rows: a renter and a vendor, one vendor asset in ``lagos`` with category
    ``other`` and ``minimum_trust_tier = 1``, kyc rows at old tiers 2 and 1, and a
    ``pending`` and a ``cancelled`` booking of the asset on the same dates.
    """
    connection = await asyncpg.connect(url)
    try:
        renter_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ('+2348000000101', 'Seed Renter', 'renter', true) RETURNING id"
        )
        vendor_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ('+2348000000102', 'Seed Vendor', 'vendor', true) RETURNING id"
        )
        asset_id = await connection.fetchval(
            "INSERT INTO assets "
            "(vendor_id, name, category, daily_rate, deposit_amount, city, "
            "listing_status, minimum_trust_tier) "
            "VALUES ($1, 'Seed Rig', 'other', 50000, 150000, 'lagos', 'active', 1) "
            "RETURNING id",
            vendor_id,
        )
        # Old trust tiers: the renter at old tier 2 (ID + liveness + address), the
        # vendor at old tier 1 (ID + liveness only).
        extra = {}
        for key, user_id, tier in (("kyc_tier2", renter_id, 2), ("kyc_tier1", vendor_id, 1)):
            extra[key] = await connection.fetchval(
                "INSERT INTO kyc_profiles (user_id, provider, status, tier, verified_at) "
                "VALUES ($1, 'seed', 'verified', $2, now()) RETURNING id",
                user_id,
                tier,
            )
        # Old booking statuses on the same dates (the old constraint ignored both).
        for key, status in (("booking_pending", "pending"), ("booking_cancelled", "cancelled")):
            extra[key] = await connection.fetchval(
                "INSERT INTO bookings (asset_id, renter_id, vendor_id, starts_on, ends_on, "
                "status, total_rental_fee, deposit_amount) "
                "VALUES ($1, $2, $3, DATE '2026-11-01', DATE '2026-11-05', $4, "
                "250000, 150000) RETURNING id",
                asset_id,
                renter_id,
                vendor_id,
                status,
            )
        return Seed0002(renter_id=renter_id, vendor_id=vendor_id, asset_id=asset_id, extra=extra)
    finally:
        await connection.close()


@pytest.fixture
def db_at_0002(alembic_cfg: Config) -> Iterator[Seed0002]:
    """A fresh schema at 0002 holding the :func:`seed_0002` rows."""
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, REVISION_0002)
    try:
        yield asyncio.run(seed_0002(asyncpg_url()))
    finally:
        command.downgrade(alembic_cfg, "base")
        command.upgrade(alembic_cfg, "head")


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """An asyncpg connection to the test database."""
    if not DATABASE_URL:
        pytest.skip("VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks")
    connection = await asyncpg.connect(asyncpg_url())
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def db_session(migrated_db: str) -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back at teardown, with constraints immediate."""
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
                if outer.is_active:
                    await outer.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(migrated_db: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, on a fresh ``head`` schema."""
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()
