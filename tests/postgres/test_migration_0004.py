"""Live PostgreSQL tests for migration 0004 (the ``booking`` payment type, A14)."""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentType
from tests.postgres.conftest import DATABASE_URL, asyncpg_url
from tests.postgres.factories import create_asset, create_booking, create_payment, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

REVISION_0003 = "0003_schema_alignment"
PAYMENT_CHECKS = {
    "ck_payments_amount_positive",
    "ck_payments_payout_fields_required",
    "ck_payments_platform_fee_nonnegative",
    "ck_payments_deposit_lifecycle_type",
    "ck_payments_payout_lifecycle_type",
    "ck_payments_deposit_status_timestamps",
    "ck_payments_payout_status_timestamps",
}


async def _fetch(query: str, *args):
    connection = await asyncpg.connect(asyncpg_url())
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _fetch_sync(query: str, *args):
    return asyncio.run(_fetch(query, *args))


def _payment_types() -> list[str]:
    rows = _fetch_sync("SELECT unnest(enum_range(NULL::paymenttype))::text AS v")
    return [row["v"] for row in rows]


def _payment_checks() -> set[str]:
    rows = _fetch_sync(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'payments'::regclass AND contype = 'c'"
    )
    return {row["conname"] for row in rows}


async def _insert_booking_payment() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            renter = await create_user(session, kyc_verified=True)
            asset = await create_asset(session)
            booking = await create_booking(
                session, asset=asset, renter=renter, status=BookingStatus.confirmed
            )
            await create_payment(session, booking, type=PaymentType.booking)
    finally:
        await engine.dispose()


def test_migration_0004_roundtrip(migrated_db: str, alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    assert "booking" in _payment_types()

    command.downgrade(alembic_cfg, "-1")
    types = _payment_types()
    assert "booking" not in types
    assert types == ["deposit", "rental_balance", "payout", "refund", "damage_contribution"]
    # The CHECKs that compare the type are recreated with it.
    assert PAYMENT_CHECKS <= _payment_checks()

    command.upgrade(alembic_cfg, "head")
    assert _payment_types()[-1] == "booking"
    assert PAYMENT_CHECKS <= _payment_checks()


def test_migration_0004_downgrade_refused_while_a_booking_payment_exists(
    migrated_db: str, alembic_cfg: Config
) -> None:
    asyncio.run(_insert_booking_payment())
    try:
        with pytest.raises(Exception, match="booking"):
            command.downgrade(alembic_cfg, REVISION_0003)
        # Nothing changed: the value and the row are still there.
        assert "booking" in _payment_types()
        rows = _fetch_sync("SELECT type::text AS t FROM payments")
        assert [row["t"] for row in rows] == ["booking"]
    finally:
        _fetch_sync("DELETE FROM payments")
    command.downgrade(alembic_cfg, REVISION_0003)
    assert "booking" not in _payment_types()
