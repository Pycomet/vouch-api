"""Shared fixtures for live PostgreSQL tests of migration 0003 and the inventory.

Every fixture here needs ``VOUCH_TEST_DATABASE_URL``; tests are skipped when it is
absent (the same pattern as ``tests/test_postgres_migration.py``).

Fixtures
--------
``head_db`` (session scope)
    Rebuilds the schema at ``head`` once per test session (see "One fresh head
    schema per session" below for how later tests reuse or rebuild it).
``migrated_db``
    A fresh schema at ``head`` (rebuilt only if an earlier test left it stale) that
    the test may commit to or move. Yields the asyncpg URL.
``db_at_0002``
    Drops the schema, upgrades to ``0002_listing_publication_fields`` and seeds it
    with raw SQL (see :func:`seed_0002`). Yields a :class:`Seed0002` with the ids of
    the seeded rows. Later tasks extend :func:`seed_0002` and ``Seed0002``.
``db_at_0005``
    The same at ``0005_otp_challenges`` with :func:`seed_0005` (vendor, assets
    including a paused one and one whose category disagrees with its catalogue
    item, bookings and webhook events), for migration 0006. Yields a
    :class:`Seed0005`.
``conn``
    An asyncpg connection to the test database. Request it after ``migrated_db`` or
    ``db_at_0002`` so it sees the prepared schema.
``db_session``
    An ``AsyncSession`` on the session's fresh ``head`` schema (no migration per
    test), inside one outer transaction that is rolled back at teardown (the
    append-only tables cannot be truncated).
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
import datetime
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
from app.db import models as _models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base

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


# ---------------------------------------------------------------------------
# One fresh head schema per session (R17)
# ---------------------------------------------------------------------------
#
# ``_HEAD["fresh"]`` is True while the database is a just-migrated ``head`` schema
# holding nothing but the migration's own seed rows. Rolled-back ``db_session``
# work keeps it fresh. Anything that may commit or move the schema (every
# ``alembic_cfg`` or ``conn`` user, so ``migrated_db``, ``db_at_0002`` and
# ``session_factory`` too) marks it stale at teardown, and the next fixture that
# needs a fresh schema rebuilds it with :func:`rebuild_head`. Tests run file by
# file, so a stretch of ``db_session`` tests costs one rebuild at most.

_HEAD = {"fresh": False}

# Reference tables migration 0003 seeds: never empty on a fresh schema, so not guarded.
# (``alembic_version`` is not a model table, so ``Base.metadata`` never lists it.)
_SEEDED_TABLES = frozenset({"cities", "categories"})


def _guarded_tables() -> tuple[str, ...]:
    """Every model table a fresh schema leaves empty; ``db_session`` refuses to start otherwise."""
    return tuple(
        table.name for table in Base.metadata.sorted_tables if table.name not in _SEEDED_TABLES
    )


async def _drop_public_schema(url: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        # A leaked open transaction makes the rebuild fail fast instead of hanging.
        await connection.execute("SET lock_timeout = '10s'")
        await connection.execute("DROP SCHEMA public CASCADE")
        await connection.execute("CREATE SCHEMA public")
    finally:
        await connection.close()


def rebuild_head(config: Config) -> None:
    """Drop every object and migrate an empty database to ``head``.

    Dropping the schema is the only way to remove committed rows from the
    append-only tables (their triggers refuse UPDATE, DELETE and TRUNCATE), and it
    is faster than ``downgrade base``.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VOUCH_DATABASE_URL", DATABASE_URL)
        get_settings.cache_clear()
        try:
            asyncio.run(_drop_public_schema(asyncpg_url()))
            command.upgrade(config, "head")
        finally:
            get_settings.cache_clear()
    _HEAD["fresh"] = True


def _mark_stale() -> None:
    _HEAD["fresh"] = False


@pytest.fixture(scope="session")
def head_db() -> Config:
    """The session's database, rebuilt at ``head`` once, when the first test needs it."""
    if not DATABASE_URL:
        pytest.skip("VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks")
    config = alembic_config()
    rebuild_head(config)
    return config


@pytest.fixture
def fresh_head(head_db: Config) -> str:
    """A fresh ``head`` schema, rebuilt only if an earlier test left it stale."""
    if not _HEAD["fresh"]:
        rebuild_head(head_db)
    return asyncpg_url()


@pytest.fixture
def alembic_cfg(head_db: Config, monkeypatch) -> Iterator[Config]:
    """Alembic config pointed at the test database (skips without one).

    The test may commit or move the schema, so the schema counts as stale after it.
    """
    monkeypatch.setenv("VOUCH_DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    try:
        yield alembic_config()
    finally:
        _mark_stale()
        get_settings.cache_clear()


@pytest.fixture
def migrated_db(alembic_cfg: Config) -> Iterator[str]:
    """A fresh schema at ``head`` that the test may commit to or move."""
    if not _HEAD["fresh"]:
        rebuild_head(alembic_cfg)
    _mark_stale()
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
    """A fresh schema at 0002 holding the :func:`seed_0002` rows; ``head`` after."""
    asyncio.run(_drop_public_schema(asyncpg_url()))
    command.upgrade(alembic_cfg, REVISION_0002)
    try:
        yield asyncio.run(seed_0002(asyncpg_url()))
    finally:
        command.upgrade(alembic_cfg, "head")


# ---------------------------------------------------------------------------
# Seeded 0005 database (for migration 0006)
# ---------------------------------------------------------------------------

REVISION_0005 = "0005_otp_challenges"
SEED_0005_VENDOR_CREATED_AT = datetime.datetime(2026, 3, 14, 10, 0, tzinfo=datetime.UTC)


@dataclass
class Seed0005:
    """Ids of the rows :func:`seed_0005` inserted."""

    renter_id: uuid.UUID
    vendor_id: uuid.UUID
    active_asset_id: uuid.UUID
    paused_asset_id: uuid.UUID
    mismatched_asset_id: uuid.UUID
    camera_item_id: uuid.UUID
    camera_category_id: int
    lens_category_id: int
    booking_ids: list[uuid.UUID]
    webhook_event_ids: list[int]


async def seed_0005(url: str) -> Seed0005:
    """Seed a database at revision 0005 with raw SQL (no ORM: models track head).

    Rows: a renter and a vendor (``created_at`` fixed at
    :data:`SEED_0005_VENDOR_CREATED_AT`); a camera catalogue item; three of the
    vendor's assets on it: an active one with serial ``SN-SEED-1``, a paused one and
    one whose ``category_id`` says ``lens`` (0005 has no FK that stops this); a
    completed and a cancelled booking of the active asset; and two webhook events,
    one processed and one with a ``processing_error``.
    """
    connection = await asyncpg.connect(url)
    try:
        renter_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ('+2348000000501', 'Seed Renter', 'renter', true) RETURNING id"
        )
        vendor_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active, created_at) "
            "VALUES ('+2348000000502', 'Seed Vendor', 'vendor', true, $1) "
            "RETURNING id",
            SEED_0005_VENDOR_CREATED_AT,
        )
        camera = await connection.fetchval("SELECT id FROM categories WHERE slug = 'camera'")
        lens = await connection.fetchval("SELECT id FROM categories WHERE slug = 'lens'")
        city = await connection.fetchval("SELECT id FROM cities WHERE slug = 'lagos'")
        item_id = await connection.fetchval(
            "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
            "VALUES ($1, 'Sony', 'FX3', 'B') RETURNING id",
            camera,
        )
        asset_sql = (
            "INSERT INTO assets (vendor_id, catalogue_item_id, category_id, city_id, "
            "daily_rate, deposit_amount, listing_status, serial_number) "
            "VALUES ($1, $2, $3, $4, 50000, 150000, $5, $6) RETURNING id"
        )
        active = await connection.fetchval(
            asset_sql, vendor_id, item_id, camera, city, "active", "SN-SEED-1"
        )
        paused = await connection.fetchval(
            asset_sql, vendor_id, item_id, camera, city, "paused", "SN-SEED-2"
        )
        mismatched = await connection.fetchval(
            asset_sql, vendor_id, item_id, lens, city, "active", None
        )
        booking_ids = []
        for status, start in (
            ("completed", datetime.date(2026, 11, 1)),
            ("cancelled_by_renter", datetime.date(2026, 11, 10)),
        ):
            booking_ids.append(
                await connection.fetchval(
                    "INSERT INTO bookings (asset_id, renter_id, vendor_id, starts_on, ends_on, "
                    "status, total_rental_fee, deposit_amount, price_snapshot, "
                    "fulfilment_choice) "
                    "VALUES ($1, $2, $3, $4::date, $4::date + 2, $5, 150000, 150000, "
                    "'{}', 'pickup') RETURNING id",
                    active,
                    renter_id,
                    vendor_id,
                    start,
                    status,
                )
            )
        webhook_event_ids = []
        for event, error in (("evt-seed-ok", None), ("evt-seed-failed", "amount mismatch")):
            webhook_event_ids.append(
                await connection.fetchval(
                    "INSERT INTO webhook_events (provider, provider_event_id, payload, "
                    "signature_ok, processed_at, processing_error) "
                    "VALUES ('fake', $1, '{}'::jsonb, true, now(), $2) RETURNING id",
                    event,
                    error,
                )
            )
        return Seed0005(
            renter_id=renter_id,
            vendor_id=vendor_id,
            active_asset_id=active,
            paused_asset_id=paused,
            mismatched_asset_id=mismatched,
            camera_item_id=item_id,
            camera_category_id=camera,
            lens_category_id=lens,
            booking_ids=booking_ids,
            webhook_event_ids=webhook_event_ids,
        )
    finally:
        await connection.close()


@pytest.fixture
def db_at_0005(alembic_cfg: Config) -> Iterator[Seed0005]:
    """A fresh schema at 0005 holding the :func:`seed_0005` rows; ``head`` after."""
    asyncio.run(_drop_public_schema(asyncpg_url()))
    command.upgrade(alembic_cfg, REVISION_0005)
    try:
        yield asyncio.run(seed_0005(asyncpg_url()))
    finally:
        command.upgrade(alembic_cfg, "head")


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    """An asyncpg connection to the test database (autocommit, so stale after)."""
    if not DATABASE_URL:
        pytest.skip("VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks")
    connection = await asyncpg.connect(asyncpg_url())
    try:
        yield connection
    finally:
        _mark_stale()
        await connection.close()


@pytest.fixture
async def db_session(fresh_head: str) -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back at teardown, with constraints immediate.

    It runs on the session's fresh ``head`` schema; rolling back keeps it fresh,
    so the next ``db_session`` test does not migrate again.
    """
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            await _assert_fresh(connection)
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


async def _assert_fresh(connection) -> None:
    """Fail loudly if committed rows leaked into the shared schema.

    A leak means some test committed without going through a fixture that marks
    the schema stale (``alembic_cfg``, ``conn``, ``migrated_db``, ``session_factory``).
    """
    # One round trip: the names of the guarded tables that hold any row.
    checks = " UNION ALL ".join(
        f"SELECT '{table}' WHERE EXISTS (SELECT 1 FROM \"{table}\")" for table in _guarded_tables()
    )
    leaked = (await connection.execute(text(checks))).scalars().all()
    if leaked:
        _mark_stale()
        pytest.fail(
            f"committed rows leaked into the shared test schema: {', '.join(leaked)}; "
            "use migrated_db or session_factory for tests that commit"
        )


@pytest.fixture
async def session_factory(migrated_db: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, on a fresh ``head`` schema (stale after)."""
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()
