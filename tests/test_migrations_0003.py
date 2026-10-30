"""Structural tests for migration 0003 part A, in SQL generation mode (no live DB)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).parents[1]
REVISION = "0003_schema_alignment"
PREVIOUS = "0002_listing_publication_fields"


def _config(buffer: StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def _sql(range_: str, *, downgrade: bool = False) -> str:
    buffer = StringIO()
    if downgrade:
        command.downgrade(_config(buffer), range_, sql=True)
    else:
        command.upgrade(_config(buffer), range_, sql=True)
    return buffer.getvalue().lower()


def test_0003_revises_0002() -> None:
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_0003_upgrade_sql_part_a() -> None:
    sql = _sql(f"{PREVIOUS}:{REVISION}")
    for fragment in (
        "create table cities",
        "id smallserial not null",
        "create table categories",
        "create table catalogue_items",
        "create function catalogue_search_vector",
        "create index ix_catalogue_items_search on catalogue_items using gin",
        "drop index ix_assets_public_lagos",
        "drop type city",
        "drop type assetcategory",
        "'accessory'",
        "jsonb_build_object('needs_review', true)",
        "greatest(minimum_trust_tier, 2)",
        "minimum_trust_tier between 0 and 3",
        "create index ix_assets_public on assets (city_id, category_id, daily_rate",
    ):
        assert fragment in sql, fragment


def test_0003_downgrade_sql_part_a() -> None:
    sql = _sql(f"{REVISION}:{PREVIOUS}", downgrade=True)
    for fragment in (
        "drop index ix_assets_public",
        "minimum_trust_tier between 0 and 2",
        "make || ' ' || c.model",
        "create type city as enum ('lagos', 'abuja')",
        "create type assetcategory as enum",
        "then 'other'",
        "create index ix_assets_public_lagos",
        "drop table catalogue_items",
        "drop function catalogue_search_vector",
        "drop table categories",
        "drop table cities",
    ):
        assert fragment in sql, fragment
