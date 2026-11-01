"""Live PostgreSQL tests for ``GET /catalogue/search`` (spec 3.3, Task 4.3).

The app runs in-process on the test's ``db_session`` (``get_session`` overridden,
as in ``test_bookings_api.py``), so nothing is committed for real. No token is
needed. Catalogue makes and models carry random suffixes so the text query only
matches this test's rows.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.booking import BookingStatus
from app.db.session import get_session
from app.main import create_app
from tests.postgres.factories import create_asset, create_booking, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/catalogue/search"
FROM, TO = date(2027, 2, 1), date(2027, 2, 3)
RANGE = {"from": FROM.isoformat(), "to": TO.isoformat()}


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


async def _book(session, asset, status, starts_on, ends_on) -> None:
    renter = await create_user(session, kyc_verified=True)
    await create_booking(
        session, asset=asset, renter=renter, status=status, starts_on=starts_on, ends_on=ends_on
    )


def _ids(response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


async def test_catalogue_search_excludes_live_overlap(client, db_session) -> None:
    make = f"Canon{_tag()}"
    booked = await create_asset(db_session, make=make, model="C70")
    cancelled = await create_asset(db_session, make=make, model="R5")
    elsewhere = await create_asset(db_session, make=make, model="R6")
    await _book(db_session, booked, BookingStatus.confirmed, date(2027, 1, 31), FROM)
    await _book(db_session, cancelled, BookingStatus.cancelled_by_renter, FROM, TO)
    await _book(db_session, elsewhere, BookingStatus.confirmed, date(2027, 2, 4), date(2027, 2, 6))
    expected = sorted([str(cancelled.id), str(elsewhere.id)])
    booked_id = str(booked.id)

    response = await client.get(API, params={"q": make, **RANGE})

    assert response.status_code == 200
    body = response.json()
    assert sorted(_ids(response)) == expected
    assert body["equivalents"] is False
    assert body["pagination"] == {"limit": 20, "next_cursor": None}
    # Without dates the booked unit is listed too.
    response = await client.get(API, params={"q": make})
    assert booked_id in _ids(response)


async def test_catalogue_search_equivalents_on_empty(client, db_session) -> None:
    tag = _tag()
    wanted = await create_asset(db_session, make="Sony", model=f"FX3 {tag}")
    same_kind = await create_asset(db_session, make="Canon", model=f"R5 {tag}x")
    await create_asset(db_session, make="Aputure", model=f"600d {tag}", category_slug="lighting")
    await _book(db_session, wanted, BookingStatus.requested, FROM, TO)
    same_kind_id = str(same_kind.id)

    response = await client.get(API, params={"q": f"FX3 {tag}", **RANGE})

    assert response.status_code == 200
    assert response.json()["equivalents"] is True
    assert _ids(response) == [same_kind_id]


async def test_catalogue_search_no_match_has_no_equivalents(client, db_session) -> None:
    await create_asset(db_session)

    response = await client.get(API, params={"q": f"Hasselblad{_tag()}", **RANGE})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["equivalents"] is False


async def test_catalogue_search_without_q_lists_everything_free(client, db_session) -> None:
    free = await create_asset(db_session)
    booked = await create_asset(db_session)
    await _book(db_session, booked, BookingStatus.in_progress, FROM, TO)
    free_id = str(free.id)

    response = await client.get(API, params=RANGE)

    assert _ids(response) == [free_id]
    assert response.json()["equivalents"] is False


async def test_catalogue_search_unlaunched_city_400(client) -> None:
    response = await client.get(API, params={"q": "fx3", "city": "nowhere"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "city_not_launched"


async def test_catalogue_search_launched_city_filter(client, db_session) -> None:
    asset = await create_asset(db_session)
    asset_id = str(asset.id)

    response = await client.get(API, params={"city": SETTINGS.launched_cities[0].upper()})

    assert response.status_code == 200
    assert _ids(response) == [asset_id]


@pytest.mark.parametrize(
    "params",
    [{"from": "2027-02-01"}, {"to": "2027-02-01"}, {"from": "2027-02-03", "to": "2027-02-01"}],
)
async def test_catalogue_search_bad_dates_400(client, params) -> None:
    response = await client.get(API, params=params)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_catalogue_search_bad_cursor_400(client) -> None:
    response = await client.get(API, params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


async def test_catalogue_search_pages(client, db_session) -> None:
    make = f"Blackmagic{_tag()}"
    for model in ("P4K", "P6K", "URSA"):
        await create_asset(db_session, make=make, model=model)

    first = await client.get(API, params={"q": make, "limit": 2})
    cursor = first.json()["pagination"]["next_cursor"]
    second = await client.get(API, params={"q": make, "limit": 2, "cursor": cursor})

    assert len(_ids(first)) == 2
    assert len(_ids(second)) == 1
    assert second.json()["pagination"]["next_cursor"] is None
