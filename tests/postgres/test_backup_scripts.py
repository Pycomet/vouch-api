"""Live tests for ``scripts/backup.sh`` and ``scripts/restore_check.sh`` (M3a §2.3).

They need ``pg_dump``, ``pg_restore`` and ``psql`` (PostgreSQL 16 or newer) on the
PATH, and are skipped without them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE_CHECK = ROOT / "scripts" / "restore_check.sh"


def _client_major() -> int:
    if not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore", "psql")):
        return 0
    version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.", version)
    return int(match.group(1)) if match else 0


pytestmark = [
    pytest.mark.skipif(
        not os.getenv("VOUCH_TEST_DATABASE_URL"),
        reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
    ),
    pytest.mark.skipif(_client_major() < 16, reason="needs PostgreSQL 16+ client tools"),
]


def _run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_backup_then_restore_drill(migrated_db: str, tmp_path: Path) -> None:
    # The asyncpg form is accepted, as VOUCH_DATABASE_URL uses it.
    backup = _run(
        BACKUP,
        env={
            "BACKUP_DATABASE_URL": os.environ["VOUCH_TEST_DATABASE_URL"],
            "BACKUP_DIR": str(tmp_path / "nightly"),
        },
    )
    assert backup.returncode == 0, backup.stderr
    dump = Path(backup.stdout.strip())
    assert dump.parent == tmp_path / "nightly"
    assert re.fullmatch(r"vouch-\d{8}T\d{6}Z\.dump", dump.name)
    assert dump.stat().st_size > 0
    assert list(dump.parent.iterdir()) == [dump]

    server = migrated_db.rsplit("/", 1)[0] + "/postgres"
    check = _run(RESTORE_CHECK, str(dump), env={"RESTORE_SERVER_URL": server})

    assert check.returncode == 0, check.stderr
    assert "alembic_version" in check.stdout
    assert "ledger check passed (no rows)" in check.stdout
    scratch = re.search(r"into (vouch_restore_check_\w+)", check.stdout).group(1)
    listed = subprocess.run(
        ["psql", server, "-At", "-c", "SELECT datname FROM pg_database"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert scratch not in listed


def test_backup_fails_without_leaving_a_file(tmp_path: Path) -> None:
    result = _run(
        BACKUP,
        env={
            "BACKUP_DATABASE_URL": "postgresql://nobody:wrong@127.0.0.1:1/none",
            "BACKUP_DIR": str(tmp_path),
        },
    )
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []


def _recording_shim(bin_dir: Path, tool: str, log: Path) -> dict[str, str]:
    """A fake ``tool`` first on PATH that logs its argv and PGPASSWORD, then fails."""
    bin_dir.mkdir()
    shim = bin_dir / tool
    shim.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{log}"\n'
        f'printf "PGPASSWORD=%s\\n" "${{PGPASSWORD:-}}" >> "{log}"\nexit 1\n'
    )
    shim.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_backup_keeps_the_password_out_of_argv(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    env = _recording_shim(tmp_path / "bin", "pg_dump", log)
    result = subprocess.run(
        ["bash", str(BACKUP)],
        env={
            **env,
            "BACKUP_DATABASE_URL": "postgresql+asyncpg://vouch:s3cret-Pass@db:5432/vouch",
            "BACKUP_DIR": str(tmp_path / "out"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    logged = log.read_text()
    assert "--dbname=postgresql://vouch@db:5432/vouch" in logged
    assert logged.count("s3cret-Pass") == 1
    assert "PGPASSWORD=s3cret-Pass" in logged


def test_restore_check_keeps_the_password_out_of_argv(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    env = _recording_shim(tmp_path / "bin", "psql", log)
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"")
    result = subprocess.run(
        ["bash", str(RESTORE_CHECK), str(dump)],
        env={**env, "RESTORE_SERVER_URL": "postgresql://vouch:s3cret-Pass@db:5432/postgres"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    logged = log.read_text()
    assert "postgresql://vouch@db:5432/postgres" in logged
    assert logged.count("s3cret-Pass") == 1
    assert "PGPASSWORD=s3cret-Pass" in logged


def test_backup_with_libpq_environment_only(migrated_db: str, tmp_path: Path) -> None:
    """No URL: the standard PG* variables (and PGPASSWORD) name the database."""
    url = os.environ["VOUCH_TEST_DATABASE_URL"]
    match = re.match(r".*://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)$", url)
    user, password, host, port, database = match.groups()
    result = _run(
        BACKUP,
        env={
            "PGHOST": host,
            "PGPORT": port,
            "PGUSER": user,
            "PGPASSWORD": password,
            "PGDATABASE": database,
            "BACKUP_DIR": str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).stat().st_size > 0


def test_backup_needs_a_database_url(tmp_path: Path) -> None:
    result = _run(BACKUP, env={"BACKUP_DIR": str(tmp_path)})
    assert result.returncode == 2
    assert "BACKUP_DATABASE_URL" in result.stderr


def test_restore_check_needs_a_dump_and_a_server(tmp_path: Path) -> None:
    assert _run(RESTORE_CHECK, env={}).returncode == 2
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"")
    result = _run(RESTORE_CHECK, str(dump), env={})
    assert result.returncode == 2
    assert "RESTORE_SERVER_URL" in result.stderr
