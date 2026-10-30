"""Live PostgreSQL tests for migration 0003 part C: ledger, audit and webhooks."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.postgres.conftest import asyncpg_url

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

ACCOUNTS = (
    "renter_deposit_liability",
    "platform_cash",
    "vendor_payable",
    "platform_revenue",
    "protection_pool",
    "processing_fees",
)

INSERT_LEDGER = (
    "INSERT INTO ledger_entries (account, direction, amount, txn_group) VALUES ($1, $2, $3, $4)"
)


async def _post(conn: asyncpg.Connection, group: uuid.UUID, dr: int, cr: int) -> None:
    await conn.execute(INSERT_LEDGER, "platform_cash", "dr", dr, group)
    await conn.execute(INSERT_LEDGER, "renter_deposit_liability", "cr", cr, group)


@pytest.fixture
async def db(migrated_db: str, conn: asyncpg.Connection) -> asyncpg.Connection:
    return conn


async def _user(conn: asyncpg.Connection) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO users (phone, full_name, role, is_active) "
        "VALUES ('+2348000000401', 'Audit Actor', 'renter', true) RETURNING id"
    )


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


async def test_ledger_update_delete_raise(db: asyncpg.Connection) -> None:
    async with db.transaction():
        await _post(db, uuid.uuid4(), 100, 100)
    with pytest.raises(asyncpg.RaiseError, match="ledger_entries is append-only"):
        await db.execute("UPDATE ledger_entries SET memo = 'x'")
    with pytest.raises(asyncpg.RaiseError, match="ledger_entries is append-only"):
        await db.execute("DELETE FROM ledger_entries")


async def test_audit_update_delete_raise(db: asyncpg.Connection) -> None:
    await db.execute(
        "INSERT INTO audit_events (actor_role, entity, entity_id, action) "
        "VALUES ('system', 'booking', $1, 'booking.expired')",
        uuid.uuid4(),
    )
    with pytest.raises(asyncpg.RaiseError, match="audit_events is append-only"):
        await db.execute("UPDATE audit_events SET reason = 'x'")
    with pytest.raises(asyncpg.RaiseError, match="audit_events is append-only"):
        await db.execute("DELETE FROM audit_events")


async def _seed_one_of_each(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await _post(conn, uuid.uuid4(), 100, 100)
    await conn.execute(
        "INSERT INTO audit_events (actor_role, entity, entity_id, action) "
        "VALUES ('system', 'booking', $1, 'booking.expired')",
        uuid.uuid4(),
    )


@pytest.mark.parametrize(
    "statement",
    ["TRUNCATE ledger_entries", "TRUNCATE audit_events", "TRUNCATE bookings CASCADE"],
)
async def test_truncate_is_rejected(db: asyncpg.Connection, statement: str) -> None:
    await _seed_one_of_each(db)
    # Rolled back, so nothing is lost if the guard were ever missing.
    transaction = db.transaction()
    await transaction.start()
    try:
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await db.execute(statement)
    finally:
        await transaction.rollback()
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 2
    assert await db.fetchval("SELECT count(*) FROM audit_events") == 1


async def test_guard_functions_pin_search_path(db: asyncpg.Connection) -> None:
    rows = await db.fetch(
        "SELECT proname, proconfig FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
        "AND proname IN ('reject_mutation', 'assert_txn_group_balanced')"
    )
    assert {row["proname"] for row in rows} == {"reject_mutation", "assert_txn_group_balanced"}
    for row in rows:
        assert "search_path=pg_catalog, public" in row["proconfig"]


# ---------------------------------------------------------------------------
# Deferred balance trigger
# ---------------------------------------------------------------------------


async def test_unbalanced_txn_group_fails_at_commit(db: asyncpg.Connection) -> None:
    group = uuid.uuid4()
    tx = db.transaction()
    await tx.start()
    try:
        # The INSERTs succeed: the check is deferred, not run per statement.
        await db.execute(INSERT_LEDGER, "platform_cash", "dr", 100, group)
        await db.execute(INSERT_LEDGER, "renter_deposit_liability", "cr", 90, group)
        # The rows are visible inside the transaction: nothing rejected them yet.
        assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 2
        # The failure arrives only at COMMIT.
        with pytest.raises(asyncpg.RaiseError, match="not balanced"):
            await tx.commit()
    finally:
        if db.is_in_transaction():
            await tx.rollback()
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 0


async def test_single_sided_entry_fails_at_commit(db: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="not balanced"):
        async with db.transaction():
            await db.execute(INSERT_LEDGER, "platform_cash", "dr", 100, uuid.uuid4())


async def test_balanced_txn_group_commits(db: asyncpg.Connection) -> None:
    group = uuid.uuid4()
    async with db.transaction():
        await db.execute(INSERT_LEDGER, "platform_cash", "dr", "150.50", group)
        await db.execute(INSERT_LEDGER, "vendor_payable", "cr", "100.25", group)
        await db.execute(INSERT_LEDGER, "platform_revenue", "cr", "50.25", group)
    assert await db.fetchval("SELECT count(*) FROM ledger_entries WHERE txn_group = $1", group) == 3


async def test_two_groups_each_balanced_commit(db: asyncpg.Connection) -> None:
    async with db.transaction():
        await _post(db, uuid.uuid4(), 100, 100)
        await _post(db, uuid.uuid4(), 40, 40)
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 4


async def test_groups_that_only_balance_together_fail(db: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="not balanced"):
        async with db.transaction():
            # Group one is 100 dr / 60 cr, group two is 60 dr / 100 cr: the
            # transaction as a whole balances, each group does not.
            await _post(db, uuid.uuid4(), 100, 60)
            await _post(db, uuid.uuid4(), 60, 100)
    assert await db.fetchval("SELECT count(*) FROM ledger_entries") == 0


# ---------------------------------------------------------------------------
# CHECKs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("account", ["cash", "", "Platform_Cash"])
async def test_ledger_rejects_unknown_account(db: asyncpg.Connection, account: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="account"):
        await db.execute(INSERT_LEDGER, account, "dr", 1, uuid.uuid4())


async def test_ledger_accepts_all_six_accounts(db: asyncpg.Connection) -> None:
    async with db.transaction():
        for account in ACCOUNTS:
            group = uuid.uuid4()
            await db.execute(INSERT_LEDGER, account, "dr", 5, group)
            await db.execute(INSERT_LEDGER, "platform_cash", "cr", 5, group)


async def test_ledger_rejects_bad_direction(db: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="direction"):
        await db.execute(INSERT_LEDGER, "platform_cash", "xx", 1, uuid.uuid4())


@pytest.mark.parametrize("amount", [0, -5])
async def test_ledger_rejects_nonpositive_amount(db: asyncpg.Connection, amount: int) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="amount"):
        await db.execute(INSERT_LEDGER, "platform_cash", "dr", amount, uuid.uuid4())


async def test_audit_rejects_invalid_actor_role(db: asyncpg.Connection) -> None:
    actor = await _user(db)
    with pytest.raises(asyncpg.CheckViolationError, match="actor_role_valid"):
        await db.execute(
            "INSERT INTO audit_events (actor_id, actor_role, entity, entity_id, action) "
            "VALUES ($1, 'superuser', 'booking', $2, 'x')",
            actor,
            uuid.uuid4(),
        )


async def test_audit_non_system_actor_needs_actor_id(db: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="actor"):
        await db.execute(
            "INSERT INTO audit_events (actor_role, entity, entity_id, action) "
            "VALUES ('admin', 'booking', $1, 'x')",
            uuid.uuid4(),
        )
    actor = await _user(db)
    await db.execute(
        "INSERT INTO audit_events (actor_id, actor_role, entity, entity_id, action) "
        "VALUES ($1, 'renter', 'booking', $2, 'booking.requested')",
        actor,
        uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def test_webhook_event_unique_per_provider(db: asyncpg.Connection) -> None:
    insert = (
        "INSERT INTO webhook_events (provider, provider_event_id, payload, signature_ok) "
        "VALUES ($1, $2, '{}'::jsonb, true)"
    )
    await db.execute(insert, "fake", "evt_1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(insert, "fake", "evt_1")
    await db.execute(insert, "other", "evt_1")
    await db.execute(insert, "fake", "evt_2")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


async def _names(query: str) -> set[str]:
    connection = await asyncpg.connect(asyncpg_url())
    try:
        return {row[0] for row in await connection.fetch(query)}
    finally:
        await connection.close()


TABLES = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
FUNCTIONS = (
    "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
    "AND proname IN ('reject_mutation', 'assert_txn_group_balanced')"
)


def test_downgrade_removes_ledger_audit_webhooks(migrated_db: str, alembic_cfg: Config) -> None:
    parts = {"ledger_entries", "audit_events", "webhook_events"}
    assert parts <= asyncio.run(_names(TABLES))
    assert asyncio.run(_names(FUNCTIONS)) == {"reject_mutation", "assert_txn_group_balanced"}

    command.downgrade(alembic_cfg, "-1")
    assert not parts & asyncio.run(_names(TABLES))
    assert not asyncio.run(_names(FUNCTIONS))

    command.upgrade(alembic_cfg, "head")
    assert parts <= asyncio.run(_names(TABLES))
