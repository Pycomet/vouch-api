"""Live PostgreSQL tests for migration 0005 (phone OTP challenges, R26 / A10)."""

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

REVISION_0004 = "0004_booking_payment"
REVISION_0005 = "0005_otp_challenges"
OTP_COLUMNS = {
    "id": ("uuid", "NO"),
    "phone": ("text", "NO"),
    "code_hash": ("text", "NO"),
    "expires_at": ("timestamp with time zone", "NO"),
    "attempts": ("smallint", "NO"),
    "consumed_at": ("timestamp with time zone", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
}


async def _fetch(query: str, *args):
    connection = await asyncpg.connect(asyncpg_url())
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _fetch_sync(query: str, *args):
    return asyncio.run(_fetch(query, *args))


def _columns(table: str) -> dict[str, tuple[str, str]]:
    rows = _fetch_sync(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {row["column_name"]: (row["data_type"], row["is_nullable"]) for row in rows}


def _indexes(table: str) -> set[str]:
    rows = _fetch_sync("SELECT indexname FROM pg_indexes WHERE tablename = $1", table)
    return {row["indexname"] for row in rows}


def _insert(attempts: int) -> None:
    _fetch_sync(
        "INSERT INTO otp_challenges (phone, code_hash, expires_at, attempts) "
        "VALUES ('+2348012345678', 'x', now() + interval '10 minutes', $1)",
        attempts,
    )


def test_migration_0005_roundtrip(migrated_db: str, alembic_cfg: Config) -> None:
    # Head is past 0005 since M3a's 0006 (which adds ip_hash): check 0005 itself.
    command.downgrade(alembic_cfg, REVISION_0005)
    assert _columns("otp_challenges") == OTP_COLUMNS
    assert "ix_otp_challenges_phone_created_at" in _indexes("otp_challenges")
    users = _columns("users")
    assert users["phone_verified_at"] == ("timestamp with time zone", "YES")
    assert users["full_name"] == ("character varying", "YES")

    command.downgrade(alembic_cfg, REVISION_0004)
    assert _columns("otp_challenges") == {}
    users = _columns("users")
    assert "phone_verified_at" not in users
    assert users["full_name"] == ("character varying", "NO")

    command.upgrade(alembic_cfg, REVISION_0005)
    assert _columns("otp_challenges") == OTP_COLUMNS


def test_attempts_check_allows_0_to_5(migrated_db: str) -> None:
    _insert(0)
    _insert(5)
    for bad in (-1, 6):
        with pytest.raises(asyncpg.CheckViolationError):
            _insert(bad)


def test_migration_0005_downgrade_refused_while_a_user_has_no_name(
    migrated_db: str, alembic_cfg: Config
) -> None:
    _fetch_sync(
        "INSERT INTO users (phone, role, is_active) VALUES ('+2348012345679', 'renter', true)"
    )
    with pytest.raises(Exception, match="full_name"):
        command.downgrade(alembic_cfg, REVISION_0004)
    # Nothing changed.
    assert "phone_verified_at" in _columns("users")

    _fetch_sync("UPDATE users SET full_name = 'Named Now' WHERE full_name IS NULL")
    command.downgrade(alembic_cfg, REVISION_0004)
    assert _columns("users")["full_name"] == ("character varying", "NO")
