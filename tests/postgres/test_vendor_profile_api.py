"""Live PostgreSQL tests for ``GET /vendor/profile`` (M3a §4.3)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.audit import ActorRole
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from tests.helpers.auth import bearer
from tests.postgres.factories import create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/vendor/profile"


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _auth(user, role: ActorRole) -> dict[str, str]:
    return bearer(user.id, role, SETTINGS)


async def test_profile_fields_and_no_recipient_code(client, db_session) -> None:
    vendor = await create_user(
        db_session,
        role=RenterRole.vendor,
        vendor_profile={
            "business_type": "rental_house",
            "is_sub_broker": True,
            "business_name": "Lens House Ltd",
            "bank_name": "Example Bank",
            "account_name": "LENS HOUSE LTD",
            "account_last4": "4321",
            "recipient_code": "RCP_secret_123",
            "business_registration_ref": "RC-000000",
        },
    )

    response = await client.get(API, headers=_auth(vendor, ActorRole.vendor))

    assert response.status_code == 200
    assert response.json() == {
        "status": "active",
        "business_type": "rental_house",
        "is_sub_broker": True,
        "business_name": "Lens House Ltd",
        "bank_name": "Example Bank",
        "account_name": "LENS HOUSE LTD",
        "account_last4": "4321",
        "has_recipient_code": True,
    }
    assert "RCP_secret_123" not in response.text
    assert "RC-000000" not in response.text


async def test_profile_without_bank_details_or_code(client, db_session) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor)

    body = (await client.get(API, headers=_auth(vendor, ActorRole.vendor))).json()

    assert body["has_recipient_code"] is False
    assert body["business_name"] is None and body["account_last4"] is None
    assert body["business_type"] == "individual"


async def test_vendor_without_profile_is_404(client, db_session) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor, vendor_status=None)

    response = await client.get(API, headers=_auth(vendor, ActorRole.vendor))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


async def test_renter_is_forbidden(client, db_session) -> None:
    renter = await create_user(db_session)

    response = await client.get(API, headers=_auth(renter, ActorRole.renter))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_no_token_is_401(client) -> None:
    response = await client.get(API)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
