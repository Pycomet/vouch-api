"""Alembic configuration and initial schema migration tests."""

from __future__ import annotations

import runpy
from contextlib import asynccontextmanager, nullcontext
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from alembic import command, context
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import get_settings

ROOT = Path(__file__).parents[1]
ALEMBIC_INI = ROOT / "alembic.ini"
ENV_PY = ROOT / "migrations" / "env.py"
TABLES = {
    "users",
    "kyc_profiles",
    "assets",
    "availability_windows",
    "bookings",
    "payments",
    "handover_records",
}
ENUMS = {
    "renterrole",
    "kycstatus",
    "assetcategory",
    "city",
    "bookingstatus",
    "handoverdirection",
    "handoverstatus",
    "paymenttype",
    "paymentstatus",
    "depositstatus",
    "payoutstatus",
}
INDEXES = {
    "ix_users_phone",
    "ix_assets_vendor_id",
    "ix_availability_windows_asset_id",
    "ix_bookings_asset_id",
    "ix_bookings_renter_id",
    "ix_bookings_vendor_id",
    "ix_payments_booking_id",
    "ix_handover_records_booking_id",
}
CONSTRAINTS = {
    "ck_assets_daily_rate_positive",
    "ck_assets_deposit_amount_nonnegative",
    "ck_availability_windows_valid_date_range",
    "ck_bookings_total_rental_fee_positive",
    "ck_bookings_deposit_amount_nonnegative",
    "ck_bookings_valid_date_range",
    "ex_bookings_no_active_asset_overlap",
    "ck_payments_amount_positive",
    "ck_payments_payout_fields_required",
    "ck_payments_platform_fee_nonnegative",
    "ck_payments_deposit_lifecycle_type",
    "ck_payments_payout_lifecycle_type",
    "ck_payments_deposit_status_timestamps",
    "ck_payments_payout_status_timestamps",
    "ck_handover_records_completed_requirements",
    "uq_handover_records_booking_direction",
    "ck_kyc_profiles_supported_tier",
    "ck_kyc_profiles_verified_at_required",
    "ck_kyc_profiles_verification_consistency",
    "uq_kyc_profiles_provider_reference",
    "uq_bookings_id_renter",
    "uq_bookings_id_vendor",
    "fk_handover_records_booking_vendor",
    "fk_handover_records_booking_renter",
}


def alembic_config(output_buffer: StringIO | None = None) -> Config:
    config = Config(str(ALEMBIC_INI), output_buffer=output_buffer)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_single_initial_migration_is_head() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == ["0003_schema_alignment"]
    assert script.get_revision("0001_initial_schema").down_revision is None


def test_env_overrides_url_from_validated_settings_in_offline_mode(
    monkeypatch,
) -> None:
    expected_url = "postgresql+asyncpg://validated:secret@db.example/vouch"
    monkeypatch.setenv("VOUCH_DATABASE_URL", expected_url)
    get_settings.cache_clear()
    config = alembic_config()
    configured = Mock()

    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", configured)
    monkeypatch.setattr(context, "run_migrations", Mock())
    monkeypatch.setattr(context, "begin_transaction", nullcontext)

    try:
        runpy.run_path(str(ENV_PY), run_name="alembic_env_offline")
    finally:
        get_settings.cache_clear()

    assert config.get_main_option("sqlalchemy.url") == expected_url
    assert configured.call_args.kwargs["url"] == expected_url


def test_env_overrides_url_from_validated_settings_in_online_mode(monkeypatch) -> None:
    expected_url = "postgresql+asyncpg://validated:secret@db.example/vouch"
    monkeypatch.setenv("VOUCH_DATABASE_URL", expected_url)
    get_settings.cache_clear()
    config = alembic_config()
    engine_factory = Mock()
    connection = Mock()
    connection.run_sync = AsyncMock()

    @asynccontextmanager
    async def connect():
        yield connection

    engine = Mock()
    engine.connect = connect
    engine.dispose = AsyncMock()
    engine_factory.return_value = engine

    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_engine_from_config", engine_factory)

    try:
        runpy.run_path(str(ENV_PY), run_name="alembic_env_online")
    finally:
        get_settings.cache_clear()

    assert config.get_main_option("sqlalchemy.url") == expected_url
    assert engine_factory.call_args.args[0]["sqlalchemy.url"] == expected_url
    connection.run_sync.assert_called_once()
    engine.dispose.assert_awaited_once()


def test_initial_upgrade_and_downgrade_are_structurally_complete() -> None:
    upgrade_output = StringIO()
    command.upgrade(alembic_config(upgrade_output), "head", sql=True)
    upgrade_sql = upgrade_output.getvalue().lower()

    downgrade_output = StringIO()
    command.downgrade(
        alembic_config(downgrade_output),
        "0001_initial_schema:base",
        sql=True,
    )
    downgrade_sql = downgrade_output.getvalue().lower()

    assert "create extension if not exists pgcrypto" in upgrade_sql
    assert "create extension if not exists btree_gist" in upgrade_sql
    for table in TABLES:
        assert f"create table {table}" in upgrade_sql
        assert f"drop table {table}" in downgrade_sql
    for enum_name in ENUMS:
        assert f"create type {enum_name}" in upgrade_sql
        assert f"drop type {enum_name}" in downgrade_sql
    for index_name in INDEXES:
        assert f"create index {index_name}" in upgrade_sql
        assert f"drop index {index_name}" in downgrade_sql
    for constraint_name in CONSTRAINTS:
        assert constraint_name in upgrade_sql
    assert "drop extension" not in downgrade_sql
