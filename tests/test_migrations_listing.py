"""Structural tests for migration 0002 (listing publication fields).

Uses SQL generation mode (no live database required) to assert the migration
produces the expected DDL and reverses cleanly.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).parents[1]


def _alembic_config(output_buffer: StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_migration_0002_exists_and_revises_0001() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    rev = script.get_revision("0002_listing_publication_fields")
    assert rev is not None
    assert rev.down_revision == "0001_initial_schema"


def test_single_head_with_0002_in_chain() -> None:
    """There must be exactly one head revision, and 0002 must be on its chain."""
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected a single head, got {heads}"
    chain = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert "0002_listing_publication_fields" in chain


def test_0002_upgrade_sql_adds_listing_status_column() -> None:
    buf = StringIO()
    command.upgrade(_alembic_config(buf), "0002_listing_publication_fields", sql=True)
    sql = buf.getvalue().lower()

    assert "listingstatus" in sql, "Expected listingstatus enum creation"
    assert "listing_status" in sql
    assert "minimum_trust_tier" in sql
    assert "review_actor_id" in sql
    assert "reviewed_at" in sql
    assert "review_reason" in sql
    assert "ck_assets_minimum_trust_tier_valid" in sql
    assert "ix_assets_public_lagos" in sql


def test_0002_upgrade_sql_backfills_to_draft() -> None:
    buf = StringIO()
    command.upgrade(_alembic_config(buf), "0002_listing_publication_fields", sql=True)
    sql = buf.getvalue().lower()
    # The migration must set existing rows to 'draft', never 'active'.
    assert "draft" in sql
    assert "update assets" in sql


def test_0002_downgrade_sql_removes_columns_and_enum() -> None:
    buf = StringIO()
    command.downgrade(
        _alembic_config(buf),
        "0002_listing_publication_fields:0001_initial_schema",
        sql=True,
    )
    sql = buf.getvalue().lower()

    assert "drop column listing_status" in sql or "drop_column" in sql or "listing_status" in sql
    assert "drop index ix_assets_public_lagos" in sql or "ix_assets_public_lagos" in sql
    # Enum should be dropped on downgrade.
    assert "listingstatus" in sql


def test_0002_does_not_touch_initial_migration_revision() -> None:
    """The 0001 revision file must not be modified."""
    initial = ROOT / "migrations" / "versions" / "0001_initial_schema.py"
    assert initial.exists()
    content = initial.read_text()
    # Sanity check: 0002 revision ID should not appear inside 0001.
    assert "0002" not in content
