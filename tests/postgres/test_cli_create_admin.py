"""Live PostgreSQL tests for ``python -m app.cli create-admin`` (M3a §2.4).

The command owns its transaction, so these tests use ``migrated_db`` (which points
``VOUCH_DATABASE_URL`` at the test database and marks the schema stale after) and
check the committed rows with a direct asyncpg connection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from app import cli
from app.db.models import User
from app.db.models.user import RenterRole

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

ROOT = Path(__file__).parents[2]
PHONE = "+2348031234567"


async def _users(url: str, phone: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(
            "SELECT id, role::text AS role, full_name, is_active FROM users WHERE phone = $1",
            phone,
        )
    finally:
        await connection.close()


async def _audits(url: str, entity_id: uuid.UUID) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(
            "SELECT actor_id, actor_role, entity, action, before, after "
            "FROM audit_events WHERE entity_id = $1 ORDER BY id",
            entity_id,
        )
    finally:
        await connection.close()


async def _insert_user(url: str, phone: str, role: str) -> uuid.UUID:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ($1, 'Existing', $2, true) RETURNING id",
            phone,
            role,
        )
    finally:
        await connection.close()


def _run(*args: str) -> int:
    return cli.main(["create-admin", *args])


async def test_creates_admin_and_prints_only_the_id(migrated_db: str, capsys) -> None:
    # The local form of the number is normalised as the OTP flow does.
    assert await cli.run(["create-admin", "--phone", "08031234567", "--name", "Ada Ops"]) == 0

    rows = await _users(migrated_db, PHONE)
    assert len(rows) == 1
    user = rows[0]
    assert user["role"] == "admin"
    assert user["full_name"] == "Ada Ops"
    assert user["is_active"] is True
    out = capsys.readouterr().out
    assert out.strip() == str(user["id"])
    assert PHONE not in out and "08031234567" not in out


async def test_writes_system_audit_event(migrated_db: str) -> None:
    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "Ada Ops"]) == 0

    user_id = (await _users(migrated_db, PHONE))[0]["id"]
    audits = await _audits(migrated_db, user_id)
    assert len(audits) == 1
    event = audits[0]
    assert event["actor_role"] == "system"
    assert event["actor_id"] is None
    assert event["entity"] == "user"
    assert event["action"] == "user.admin_created"
    # record_audit stores a missing ``before`` as JSON null.
    assert json.loads(event["before"]) is None
    assert json.loads(event["after"]) == {"role": "admin", "is_active": True}


async def test_idempotent_on_existing_admin(migrated_db: str, capsys) -> None:
    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "Ada Ops"]) == 0
    first = capsys.readouterr().out.strip()

    assert await cli.run(["create-admin", "--phone", "+2348031234567", "--name", "Other"]) == 0

    rows = await _users(migrated_db, PHONE)
    assert len(rows) == 1
    assert rows[0]["full_name"] == "Ada Ops"
    assert capsys.readouterr().out.strip() == first
    # Nothing changed the second time, so nothing new is audited.
    assert len(await _audits(migrated_db, rows[0]["id"])) == 1


@pytest.mark.parametrize("role", ["renter", "vendor"])
async def test_refuses_phone_of_another_role(migrated_db: str, capsys, role: str) -> None:
    user_id = await _insert_user(migrated_db, PHONE, role)

    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "Ada Ops"]) == 1

    rows = await _users(migrated_db, PHONE)
    assert [(row["id"], row["role"]) for row in rows] == [(user_id, role)]
    assert await _audits(migrated_db, user_id) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"already belongs to a {role}" in captured.err


async def test_refuses_inactive_admin(migrated_db: str, capsys) -> None:
    connection = await asyncpg.connect(migrated_db)
    try:
        await connection.execute(
            "INSERT INTO users (phone, full_name, role, is_active) "
            "VALUES ($1, 'Old Admin', 'admin', false)",
            PHONE,
        )
    finally:
        await connection.close()

    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "Ada Ops"]) == 1

    rows = await _users(migrated_db, PHONE)
    assert len(rows) == 1 and rows[0]["is_active"] is False
    assert "inactive" in capsys.readouterr().err


@pytest.mark.parametrize("phone", ["12345", "+44 20 7946 0958", "0803-123-4567", ""])
async def test_refuses_bad_phone(migrated_db: str, capsys, phone: str) -> None:
    assert await cli.run(["create-admin", "--phone", phone, "--name", "Ada Ops"]) == 2

    connection = await asyncpg.connect(migrated_db)
    try:
        assert await connection.fetchval("SELECT count(*) FROM users") == 0
    finally:
        await connection.close()
    assert "Nigerian phone number" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["", "   "])
async def test_refuses_blank_name(migrated_db: str, capsys, name: str) -> None:
    assert await cli.run(["create-admin", "--phone", PHONE, "--name", name]) == 2
    assert await _users(migrated_db, PHONE) == []
    assert "name" in capsys.readouterr().err


def test_main_runs_the_command(migrated_db: str, capsys) -> None:
    assert _run("--phone", PHONE, "--name", "Ada Ops") == 0
    assert uuid.UUID(capsys.readouterr().out.strip())


def test_module_entry_point_rejects_bad_phone() -> None:
    """``python -m app.cli`` is wired up; a bad phone fails before any database work."""
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "create-admin", "--phone", "123", "--name", "A"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Nigerian phone number" in result.stderr


def test_requires_a_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


async def test_concurrent_registration_is_refused(migrated_db: str, capsys, monkeypatch) -> None:
    """A user created between the lookup and the insert trips the unique phone."""
    user_id = await _insert_user(migrated_db, PHONE, "renter")

    async def lookup_misses(session, *, phone, full_name):
        # As if the lookup ran before the other process committed its row: the
        # insert's flush raises IntegrityError on the unique phone.
        session.add(User(phone=phone, full_name=full_name, role=RenterRole.admin))
        await session.flush()

    monkeypatch.setattr(cli, "create_admin", lookup_misses)

    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "Ada Ops"]) == 1

    rows = await _users(migrated_db, PHONE)
    assert [(row["id"], row["role"]) for row in rows] == [(user_id, "renter")]
    assert "concurrently" in capsys.readouterr().err


async def test_refuses_soft_deleted_admin(migrated_db: str, capsys) -> None:
    """A soft-deleted admin is not reactivated (amendment A21)."""
    connection = await asyncpg.connect(migrated_db)
    try:
        user_id = await connection.fetchval(
            "INSERT INTO users (phone, full_name, role, is_active, deleted_at) "
            "VALUES ($1, 'Old Admin', 'admin', true, now()) RETURNING id",
            PHONE,
        )
    finally:
        await connection.close()

    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "Ada Ops"]) == 1

    connection = await asyncpg.connect(migrated_db)
    try:
        row = await connection.fetchrow(
            "SELECT is_active, deleted_at FROM users WHERE id = $1", user_id
        )
    finally:
        await connection.close()
    assert row["is_active"] is True and row["deleted_at"] is not None
    assert await _audits(migrated_db, user_id) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inactive or deleted" in captured.err


async def test_refuses_a_name_over_200_characters(migrated_db: str, capsys) -> None:
    assert await cli.run(["create-admin", "--phone", PHONE, "--name", "A" * 201]) == 2
    assert await _users(migrated_db, PHONE) == []
    assert "200" in capsys.readouterr().err


async def test_accepts_a_200_character_name(migrated_db: str) -> None:
    assert await cli.run(["create-admin", "--phone", PHONE, "--name", " " + "A" * 200 + " "]) == 0
    assert (await _users(migrated_db, PHONE))[0]["full_name"] == "A" * 200
