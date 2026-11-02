"""The ``db_session`` leak guard covers every table that is empty on a fresh schema."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.postgres.conftest import DATABASE_URL, _assert_fresh, _guarded_tables


def test_guarded_tables_are_derived_from_the_models() -> None:
    guarded = set(_guarded_tables())

    assert {"webhook_events", "users", "assets", "bookings", "ledger_entries"} <= guarded
    # Seeded by migration 0003 (and alembic's own bookkeeping): never empty.
    assert not guarded & {"cities", "categories", "alembic_version"}


async def test_a_committed_row_in_a_formerly_unchecked_table_is_detected(
    migrated_db: str, conn
) -> None:
    assert DATABASE_URL is not None
    await conn.execute(
        "INSERT INTO webhook_events (provider, provider_event_id, payload, signature_ok) "
        "VALUES ('fake', 'evt-leak', '{}'::jsonb, true)"
    )
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            with pytest.raises(pytest.fail.Exception, match="webhook_events"):
                await _assert_fresh(connection)
    finally:
        await engine.dispose()
