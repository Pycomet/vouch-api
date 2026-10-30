"""Model structure tests for assets and availability_windows (no live DB)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Enum, Index, Numeric, SmallInteger
from sqlalchemy.dialects.postgresql import ExcludeConstraint, dialect
from sqlalchemy.schema import CreateTable

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.models.asset import ListingStatus
from app.db.models.catalogue import BAND_DEFAULT_TIER, ValueBand


def _table(name: str):
    return Base.metadata.tables[name]


def _check_sql(table_name: str) -> set[str]:
    return {
        str(constraint.sqltext)
        for constraint in _table(table_name).constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_assets_table_in_metadata() -> None:
    assert "assets" in Base.metadata.tables


def test_availability_windows_table_in_metadata() -> None:
    assert "availability_windows" in Base.metadata.tables


def test_assets_daily_rate_is_numeric() -> None:
    col = _table("assets").c["daily_rate"]
    assert isinstance(col.type, Numeric)


def test_assets_deposit_amount_is_numeric() -> None:
    col = _table("assets").c["deposit_amount"]
    assert isinstance(col.type, Numeric)


def test_availability_windows_asset_id_fk_references_assets() -> None:
    col = _table("availability_windows").c["asset_id"]
    fk_targets = {fk.target_fullname for fk in col.foreign_keys}
    assert "assets.id" in fk_targets


def test_asset_monetary_amounts_are_constrained() -> None:
    checks = _check_sql("assets")
    assert "daily_rate > 0" in checks
    assert "deposit_amount >= 0" in checks


def test_availability_window_has_valid_date_range() -> None:
    assert "starts_at <= ends_at" in _check_sql("availability_windows")


# --- Listing publication fields ---


def test_assets_listing_status_column_exists() -> None:
    col = _table("assets").c["listing_status"]
    assert isinstance(col.type, Enum)


def test_assets_listing_status_enum_values() -> None:
    col = _table("assets").c["listing_status"]
    assert set(col.type.enums) == {"draft", "review", "active", "paused", "rejected"}


def test_assets_listing_status_default_is_draft() -> None:
    col = _table("assets").c["listing_status"]
    # Server default is set to 'draft' — new rows are never accidentally active.
    assert col.server_default is not None
    assert "draft" in str(col.server_default.arg)


def test_assets_minimum_trust_tier_column_exists() -> None:
    _table("assets").c["minimum_trust_tier"]


def test_assets_minimum_trust_tier_constrained() -> None:
    assert "minimum_trust_tier BETWEEN 0 AND 3" in _check_sql("assets")


def test_assets_review_actor_id_column_exists() -> None:
    _table("assets").c["review_actor_id"]


def test_assets_reviewed_at_column_exists() -> None:
    _table("assets").c["reviewed_at"]


def test_assets_review_reason_column_exists() -> None:
    _table("assets").c["review_reason"]


def test_listing_status_enum_has_expected_members() -> None:
    assert set(ListingStatus) == {
        ListingStatus.draft,
        ListingStatus.review,
        ListingStatus.active,
        ListingStatus.paused,
        ListingStatus.rejected,
    }


# --- Reference tables and catalogue (migration 0003) ---


def test_assets_reference_columns_are_foreign_keys() -> None:
    table = _table("assets")
    expected = {
        "city_id": "cities.id",
        "category_id": "categories.id",
        "catalogue_item_id": "catalogue_items.id",
    }
    for column, target in expected.items():
        col = table.c[column]
        assert not col.nullable
        assert {fk.target_fullname for fk in col.foreign_keys} == {target}
    assert isinstance(table.c["city_id"].type, SmallInteger)
    assert isinstance(table.c["category_id"].type, SmallInteger)


def test_assets_enum_columns_are_gone() -> None:
    columns = set(_table("assets").c.keys())
    assert "city" not in columns
    assert "category" not in columns


def test_assets_name_is_a_nullable_display_override() -> None:
    assert _table("assets").c["name"].nullable


def test_assets_listing_detail_columns() -> None:
    table = _table("assets")
    assert table.c["condition_grade"].nullable
    assert not table.c["included_accessories"].nullable
    assert not table.c["fulfilment"].nullable
    for column in ("weekend_rate", "weekly_rate"):
        assert isinstance(table.c[column].type, Numeric)
        assert table.c[column].type.precision == 12
        assert table.c[column].type.scale == 2
    checks = _check_sql("assets")
    assert "condition_grade IN ('excellent', 'good', 'fair')" in checks
    assert "weekend_rate IS NULL OR weekend_rate > 0" in checks
    assert "weekly_rate IS NULL OR weekly_rate > 0" in checks


def test_assets_public_index_replaces_city_specific_index() -> None:
    indexes = {index.name: index for index in _table("assets").indexes}
    assert "ix_assets_public" in indexes
    assert not any(name.startswith("ix_assets_public_") for name in indexes)
    public: Index = indexes["ix_assets_public"]
    assert [c.name for c in public.columns] == [
        "city_id",
        "category_id",
        "daily_rate",
        "created_at",
        "id",
    ]


def test_asset_relationships_raise_on_lazy_load() -> None:
    from app.db.models.asset import Asset

    for name in ("city_ref", "category_ref", "catalogue_item"):
        assert Asset.__mapper__.relationships[name].lazy == "raise"


def test_reference_tables_in_metadata() -> None:
    for name in ("cities", "categories", "catalogue_items"):
        assert name in Base.metadata.tables
    assert not _table("categories").c["checklist_template"].nullable
    assert isinstance(_table("cities").c["id"].type, SmallInteger)


def test_catalogue_item_band_constrained_and_unique_make_model() -> None:
    table = _table("catalogue_items")
    assert "replacement_value_band IN ('A', 'B', 'C')" in _check_sql("catalogue_items")
    uniques = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("make", "model") in uniques


def test_band_default_tier() -> None:
    assert BAND_DEFAULT_TIER == {ValueBand.A: 1, ValueBand.B: 2, ValueBand.C: 3}


def test_listing_status_docstring_names_no_city() -> None:
    doc = (ListingStatus.__doc__ or "").lower()
    assert "a launched city" in doc
    assert "lagos" not in doc
    assert "abuja" not in doc


def test_blocked_availability_windows_cannot_overlap() -> None:
    table = Base.metadata.tables["availability_windows"]
    constraints = [c for c in table.constraints if isinstance(c, ExcludeConstraint)]
    assert [c.name for c in constraints] == ["ex_availability_windows_no_blocked_overlap"]
    assert str(constraints[0].where) == "is_blocked"
    compiled = str(CreateTable(table).compile(dialect=dialect()))
    assert "daterange(starts_at, ends_at, '[]') WITH &&" in compiled
