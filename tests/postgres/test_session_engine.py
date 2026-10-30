"""The application engine never echoes SQL parameter values in its errors.

A failed statement's ``str(exc)`` ends up in logs and tracebacks; with the bound
parameters in it, that would leak personal data (phones, names) and amounts.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.session import create_database_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

PHONE = "+2348099990001"
NAME = "Parameter Canary"


async def test_integrity_error_hides_parameters(migrated_db, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_DATABASE_URL", os.environ["VOUCH_TEST_DATABASE_URL"])
    get_settings.cache_clear()
    engine = create_database_engine(get_settings())
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            insert = text(
                "INSERT INTO users (phone, full_name, role, is_active)"
                " VALUES (:phone, :name, 'renter', true)"
            )
            await connection.execute(insert, {"phone": PHONE, "name": "First"})
            with pytest.raises(IntegrityError) as caught:
                # The duplicate phone violates the unique constraint.
                await connection.execute(insert, {"phone": PHONE, "name": NAME})
            await transaction.rollback()
    finally:
        await engine.dispose()
        get_settings.cache_clear()

    message = str(caught.value)
    assert "[SQL parameters hidden due to hide_parameters=True]" in message
    assert PHONE not in message
    assert NAME not in message
