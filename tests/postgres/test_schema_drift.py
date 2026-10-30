"""The ORM metadata must describe exactly what migration 0003 builds."""

from __future__ import annotations

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401
from app.db.base import Base
from app.db.migration_filters import include_object

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

# Known, accepted differences. Only this one: the ORM declares an FK on
# handover_records.vendor_signer_id that the older migration never created; adding
# it is outside Step 2.
ALLOWED = {("add_fk", "handover_records", ("vendor_signer_id",))}


def _describe(diff) -> tuple | None:
    if isinstance(diff, tuple) and diff[0] == "add_fk":
        fk = diff[1]
        return ("add_fk", fk.parent.name, tuple(c.name for c in fk.columns))
    return None


def _compare(connection) -> list:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_object": include_object,
        },
    )
    return compare_metadata(context, Base.metadata)


async def test_orm_matches_migrated_schema(migrated_db: str) -> None:
    engine = create_async_engine(os.environ["VOUCH_TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            diffs = await connection.run_sync(_compare)
    finally:
        await engine.dispose()
    unexpected = [diff for diff in diffs if _describe(diff) not in ALLOWED]
    assert unexpected == []
