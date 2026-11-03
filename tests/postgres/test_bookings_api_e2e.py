"""One end-to-end check through the production session path (Task 4.2).

``test_bookings_api.py`` overrides ``get_session`` with the test's own session, so
the real ``get_session`` and its commit never run there. This test does not
override it: the app (lifespan included) talks to the migrated database, and the
checks read the rows back on a separate connection, so they only find them if the
request really committed. ``session_factory`` builds on ``migrated_db``, which
marks the shared schema stale afterwards.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Environment, Settings
from app.db.models import AuditEvent, Booking
from app.db.models.audit import ActorRole
from app.main import create_app
from tests.helpers.auth import bearer
from tests.postgres.conftest import DATABASE_URL
from tests.postgres.factories import FIRST_START, create_asset, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


async def test_create_then_get_booking_through_the_real_session(session_factory) -> None:
    assert DATABASE_URL is not None
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=DATABASE_URL,
        jwt_secret=SecretStr("test-secret-that-is-long-enough-for-hs256"),
    )
    async with session_factory() as session:
        renter = await create_user(session, kyc_verified=True)
        asset = await create_asset(session)
        await session.commit()
        renter_id, asset_id = renter.id, asset.id
    headers = bearer(renter_id, ActorRole.renter, settings)

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        created = await http.post(
            "/api/v1/bookings",
            json={
                "asset_id": str(asset_id),
                "starts_on": FIRST_START.isoformat(),
                "ends_on": (FIRST_START + timedelta(days=2)).isoformat(),
                "fulfilment": "pickup",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        booking_id = created.json()["id"]

        fetched = await http.get(f"/api/v1/bookings/{booking_id}", headers=headers)

    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["id"] == booking_id
    assert body["status"] == "requested"
    assert body["money_line"]["deposit"]["status"] == "due"
    assert body["money_line"]["rental"]["status"] == "due"
    assert body["money_line"]["next"] == {
        "label": "Awaiting vendor response",
        "amount_due": "0.00",
    }

    async with session_factory() as session:
        stored = await session.get(Booking, uuid.UUID(booking_id))
        assert stored is not None
        assert stored.status.value == "requested"
        actions = (
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.entity_id == stored.id)
            )
        ).all()
    assert len(actions) == 1
