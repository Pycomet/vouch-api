"""Structural tests for migration 0003 parts A and B, in SQL generation mode (no live DB)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.models.booking import live_status_predicate

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


def test_0003_upgrade_sql_part_b() -> None:
    sql = _sql(f"{PREVIOUS}:{REVISION}")
    for fragment in (
        "alter table users add column restricted_at timestamp with time zone",
        "restricted_at is null or restricted_reason is not null",
        "status = 'submitted', tier = 0, verified_at = null",
        "tier between 0 and 1",
        "create table vendor_references",
        "uq_vendor_references_renter_vendor",
        "alter type bookingstatus rename to bookingstatus_old",
        "create type bookingstatus as enum ('requested', 'accepted', 'deposit_paid'",
        "when 'pending' then 'requested'",
        "when 'active' then 'in_progress'",
        "when 'cancelled' then 'cancelled_by_renter'",
        "drop type bookingstatus_old",
        "created_at + interval '4 hours'",
        "'basis', 'legacy'",
        "drop constraint ex_bookings_no_active_asset_overlap",
        "ex_bookings_no_live_overlap",
        "ck_bookings_requested_has_hold",
        "ck_bookings_accepted_has_deposit_due",
        "ck_bookings_returned_has_claim_window",
        "ck_bookings_claim_complete",
        "ck_bookings_not_self",
        "ex_availability_windows_no_blocked_overlap",
        "alter type paymenttype rename value 'protection_waiver' to 'damage_contribution'",
    ):
        assert fragment in sql, fragment


def test_0003_live_overlap_predicate_matches_the_model() -> None:
    sql = _sql(f"{PREVIOUS}:{REVISION}")
    statement = next(
        line for line in sql.splitlines() if "add constraint ex_bookings_no_live_overlap" in line
    )
    assert f"where ({live_status_predicate().lower()})" in statement


def test_0003_downgrade_sql_part_b() -> None:
    sql = _sql(f"{REVISION}:{PREVIOUS}", downgrade=True)
    for fragment in (
        "alter type paymenttype rename value 'damage_contribution' to 'protection_waiver'",
        "drop constraint ex_availability_windows_no_blocked_overlap",
        "drop constraint ex_bookings_no_live_overlap",
        "create type bookingstatus as enum ('pending', 'confirmed', 'active'",
        "when 'deposit_paid' then 'pending'",
        "when 'returned' then 'active'",
        "when 'expired' then 'cancelled'",
        "when 'lost' then 'disputed'",
        "ex_bookings_no_active_asset_overlap",
        "drop table vendor_references",
        "set tier = 2",
        "tier between 0 and 2",
        "drop column restricted_at",
    ):
        assert fragment in sql, fragment
    # Part B is undone before part A.
    assert sql.index("drop table vendor_references") < sql.index("drop table catalogue_items")
