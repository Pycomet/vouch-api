"""``get_current_actor`` loads the token's user (R25).

A valid signature is not enough: the user must still exist, be active and not
soft-deleted, and hold the role the token names. Anything else is the same 401
``unauthenticated`` as a bad token. A restricted renter still authenticates; the
booking guards stop them booking.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_actor, require_role
from app.core.config import get_settings
from app.db.models import ActorRole
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.services.audit import Actor
from tests.helpers.auth import bearer
from tests.postgres.factories import DEFAULT_NOW, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)
    router = APIRouter(prefix="/test-actor")

    @router.get("/me")
    async def _me(actor: Actor = Depends(get_current_actor)) -> dict[str, str]:  # noqa: B008
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    @router.get("/vendor-only")
    async def _vendor(
        actor: Actor = Depends(require_role(ActorRole.vendor)),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": actor.role.value}

    app.include_router(router)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _assert_401(response) -> None:
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "unauthenticated"
    assert body["message"] == "Authentication is required."
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("role", [RenterRole.renter, RenterRole.vendor, RenterRole.admin])
async def test_active_user_authenticates(client, db_session, role: RenterRole) -> None:
    user_id = (await create_user(db_session, role=role)).id

    response = await client.get(
        "/test-actor/me", headers=bearer(user_id, ActorRole(role.value), SETTINGS)
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user_id), "role": role.value}


async def test_unknown_user_401(client) -> None:
    response = await client.get(
        "/test-actor/me", headers=bearer(uuid.uuid4(), ActorRole.renter, SETTINGS)
    )

    _assert_401(response)


async def test_inactive_user_401(client, db_session) -> None:
    user = await create_user(db_session)
    user.is_active = False
    await db_session.flush()

    _assert_401(
        await client.get("/test-actor/me", headers=bearer(user.id, ActorRole.renter, SETTINGS))
    )


async def test_soft_deleted_user_401(client, db_session) -> None:
    user = await create_user(db_session)
    user.deleted_at = DEFAULT_NOW
    await db_session.flush()

    _assert_401(
        await client.get("/test-actor/me", headers=bearer(user.id, ActorRole.renter, SETTINGS))
    )


@pytest.mark.parametrize(
    ("stored", "claimed"),
    [
        (RenterRole.renter, ActorRole.vendor),
        (RenterRole.renter, ActorRole.admin),
        (RenterRole.vendor, ActorRole.admin),
        (RenterRole.vendor, ActorRole.renter),
    ],
)
async def test_role_mismatch_401(client, db_session, stored, claimed) -> None:
    user = await create_user(db_session, role=stored)

    _assert_401(await client.get("/test-actor/me", headers=bearer(user.id, claimed, SETTINGS)))


async def test_role_mismatch_is_401_not_403_on_a_role_route(client, db_session) -> None:
    # A renter holding a forged-role (but validly signed) vendor token is not a vendor.
    user = await create_user(db_session, role=RenterRole.renter)

    _assert_401(
        await client.get(
            "/test-actor/vendor-only", headers=bearer(user.id, ActorRole.vendor, SETTINGS)
        )
    )


async def test_demoted_user_loses_the_old_role(client, db_session) -> None:
    user = await create_user(db_session, role=RenterRole.vendor)
    headers = bearer(user.id, ActorRole.vendor, SETTINGS)
    assert (await client.get("/test-actor/vendor-only", headers=headers)).status_code == 200

    user.role = RenterRole.renter
    await db_session.flush()

    _assert_401(await client.get("/test-actor/vendor-only", headers=headers))


async def test_restricted_user_still_authenticates(client, db_session) -> None:
    user = await create_user(db_session)
    user.restricted_at = DEFAULT_NOW
    user.restricted_reason = "no contact after the rental started"
    await db_session.flush()

    response = await client.get(
        "/test-actor/me", headers=bearer(user.id, ActorRole.renter, SETTINGS)
    )

    assert response.status_code == 200
