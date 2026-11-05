"""Live PostgreSQL tests for ``GET /catalogue/items`` (M3a §4.1): the public lookup."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import CatalogueItem
from app.db.models.reference import Category
from app.db.session import get_session
from app.main import create_app

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/catalogue/items"


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


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _item(
    session: AsyncSession,
    *,
    make: str,
    model: str,
    category: str = "camera",
    mount: str | None = None,
    band: str = "B",
    aliases: list[str] | None = None,
    is_active: bool = True,
) -> SimpleNamespace:
    category_id = (
        await session.execute(select(Category.id).where(Category.slug == category))
    ).scalar_one()
    item = CatalogueItem(
        category_id=category_id,
        make=make,
        model=model,
        mount=mount,
        replacement_value_band=band,
        aliases=aliases or [],
        is_active=is_active,
    )
    session.add(item)
    await session.flush()
    # The request commits (and so expires) the ORM row; keep just the id.
    return SimpleNamespace(id=item.id)


def _ids(response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


async def test_lookup_is_public_and_shapes_each_item(client, db_session) -> None:
    make = f"Sony{_tag()}"
    item = await _item(db_session, make=make, model="FX3", mount="E", band="C", aliases=["fx-3"])

    response = await client.get(API, params={"q": make})

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"] == {"limit": 20, "next_cursor": None}
    assert body["items"] == [
        {
            "id": str(item.id),
            "category": "camera",
            "make": make,
            "model": "FX3",
            "mount": "E",
            "specs": {},
            "replacement_value_band": "C",
        }
    ]


async def test_q_matches_make_and_alias(client, db_session) -> None:
    tag = _tag()
    by_make = await _item(db_session, make=f"Zeiss{tag}", model="Alpha")
    by_alias = await _item(db_session, make="Other", model="Beta", aliases=[f"nickname{tag}"])
    await _item(db_session, make=f"Unrelated{_tag()}", model="Gamma")

    assert _ids(await client.get(API, params={"q": f"Zeiss{tag}"})) == [str(by_make.id)]
    assert _ids(await client.get(API, params={"q": f"nickname{tag}"})) == [str(by_alias.id)]


async def test_category_filter_by_slug(client, db_session) -> None:
    tag = _tag()
    cam = await _item(db_session, make=f"Mk{tag}", model="Body", category="camera")
    await _item(db_session, make=f"Mk{tag}", model="Glass", category="lens")

    response = await client.get(API, params={"q": f"Mk{tag}", "category": "camera"})

    assert _ids(response) == [str(cam.id)]
    assert response.json()["items"][0]["category"] == "camera"


async def test_unknown_category_is_invalid_request(client) -> None:
    response = await client.get(API, params={"category": "no-such-category"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"] == {"category": "no-such-category"}


async def test_inactive_items_are_hidden(client, db_session) -> None:
    make = f"Hidden{_tag()}"
    await _item(db_session, make=make, model="Old", is_active=False)
    live = await _item(db_session, make=make, model="New")

    assert _ids(await client.get(API, params={"q": make})) == [str(live.id)]


async def test_cursor_pagination_walks_every_item_once(client, db_session) -> None:
    make = f"Page{_tag()}"
    created = {str((await _item(db_session, make=make, model=f"M{n}")).id) for n in range(5)}

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"q": make, "limit": "2", **({"cursor": cursor} if cursor else {})}
        response = await client.get(API, params=params)
        seen += _ids(response)
        pages += 1
        cursor = response.json()["pagination"]["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert len(seen) == 5 and set(seen) == created


async def test_bad_cursor_is_400(client) -> None:
    response = await client.get(API, params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"
