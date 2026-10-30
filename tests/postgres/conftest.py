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
    """Seed a database at revision 0002 with raw SQL (no ORM: models track head)."""
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
        return Seed0002(renter_id=renter_id, vendor_id=vendor_id, asset_id=asset_id)
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
