"""Live PostgreSQL tests for public inventory in the launched cities.

The API runs against the real test database. Launched cities come from the
``get_launched_cities`` dependency (default settings: Abuja only), and tests
override it to prove nothing about a city is hard-coded.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import asyncpg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dependencies import get_launched_cities
from app.core.config import Environment, Settings
from app.main import create_app
from app.schemas.errors import CityNotLaunchedError, ResourceNotFoundError
from app.services.inventory import get_public_asset, search_public_assets
from tests.postgres.conftest import DATABASE_URL, asyncpg_url

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


@dataclass
class InventorySeed:
    vendor_id: uuid.UUID
    renter_id: uuid.UUID
    abuja_camera: uuid.UUID
    abuja_light: uuid.UUID
    abuja_draft: uuid.UUID
    abuja_inactive_vendor: uuid.UUID
    lagos_camera: uuid.UUID


async def _seed() -> InventorySeed:
    conn = await asyncpg.connect(asyncpg_url())
    try:

        async def user(phone: str, role: str, active: bool = True) -> uuid.UUID:
            return await conn.fetchval(
                "INSERT INTO users (phone, full_name, role, is_active) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                phone,
                f"User {phone}",
                role,
                active,
            )

        async def item(category: str, make: str, model: str, aliases: list[str]) -> uuid.UUID:
            return await conn.fetchval(
                "INSERT INTO catalogue_items "
                "(category_id, make, model, aliases, replacement_value_band) "
                "VALUES ((SELECT id FROM categories WHERE slug = $1), $2, $3, $4, 'B') "
                "RETURNING id",
                category,
                make,
                model,
                aliases,
            )

        async def asset(
            vendor: uuid.UUID,
            item_id: uuid.UUID,
            city: str,
            category: str,
            rate: int,
            status: str = "active",
            name: str | None = None,
        ) -> uuid.UUID:
            return await conn.fetchval(
                "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
                "name, daily_rate, deposit_amount, listing_status, minimum_trust_tier, "
                "condition_grade, weekend_rate, weekly_rate) "
                "VALUES ($1, $2, (SELECT id FROM cities WHERE slug = $3), "
                "(SELECT id FROM categories WHERE slug = $4), $5, $6, 200000, $7, 2, "
                "'good', 120000, 400000) RETURNING id",
                vendor,
                item_id,
                city,
                category,
                name,
                rate,
                status,
            )

        vendor = await user("+2348000000301", "vendor")
        inactive_vendor = await user("+2348000000302", "vendor", active=False)
        renter = await user("+2348000000303", "renter")
        fx3 = await item("camera", "Sony", "FX3", ["ILME-FX3", "cinema line"])
        light = await item("lighting", "Aputure", "600d", ["light storm"])
        return InventorySeed(
            vendor_id=vendor,
            renter_id=renter,
            abuja_camera=await asset(vendor, fx3, "abuja", "camera", 75000),
            abuja_light=await asset(vendor, light, "abuja", "lighting", 40000, name="Key light"),
            abuja_draft=await asset(vendor, fx3, "abuja", "camera", 75000, status="draft"),
            abuja_inactive_vendor=await asset(inactive_vendor, fx3, "abuja", "camera", 75000),
            lagos_camera=await asset(vendor, fx3, "lagos", "camera", 80000),
        )
    finally:
        await conn.close()


async def _execute(query: str, *args) -> None:
    conn = await asyncpg.connect(asyncpg_url())
    try:
        await conn.execute(query, *args)
    finally:
        await conn.close()


@pytest.fixture
def seed(migrated_db: str) -> InventorySeed:
    return asyncio.run(_seed())


@pytest.fixture
def app():
    assert DATABASE_URL is not None
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=DATABASE_URL,
    )
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


def test_search_abuja_returns_active_assets(client: TestClient, seed: InventorySeed) -> None:
    response = client.get("/api/v1/assets", params={"city": "abuja"})
    assert _ids(response) == {str(seed.abuja_camera), str(seed.abuja_light)}

    items = {item["id"]: item for item in response.json()["items"]}
    camera = items[str(seed.abuja_camera)]
    assert camera["city"] == "abuja"
    assert camera["category"] == "camera"
    assert camera["make"] == "Sony"
    assert camera["model"] == "FX3"
    assert camera["name"] is None
    assert camera["display_name"] == "Sony FX3"
    assert camera["condition_grade"] == "good"
    assert camera["daily_rate"] == "75000.00"
    assert camera["weekend_rate"] == "120000.00"
    assert camera["weekly_rate"] == "400000.00"
    assert camera["minimum_trust_tier"] == 2
    assert items[str(seed.abuja_light)]["display_name"] == "Key light"


def test_search_lagos_returns_400_while_not_launched(
    client: TestClient, seed: InventorySeed
) -> None:
    response = client.get("/api/v1/assets", params={"city": "lagos"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "city_not_launched"
    assert error["details"] == {"city": "lagos"}


def test_search_unknown_city_400(client: TestClient, seed: InventorySeed) -> None:
    response = client.get("/api/v1/assets", params={"city": "atlantis"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "city_not_launched"
    assert error["details"] == {"city": "atlantis"}


def test_no_city_param_returns_all_launched(client: TestClient, seed: InventorySeed) -> None:
    assert _ids(client.get("/api/v1/assets")) == {
        str(seed.abuja_camera),
        str(seed.abuja_light),
    }


def test_lagos_asset_detail_is_404_when_not_launched(
    client: TestClient, seed: InventorySeed
) -> None:
    hidden = client.get(f"/api/v1/assets/{seed.lagos_camera}")
    unknown = client.get(f"/api/v1/assets/{uuid.uuid4()}")
    assert hidden.status_code == unknown.status_code == 404
    hidden_error = hidden.json()["error"]
    unknown_error = unknown.json()["error"]
    hidden_error.pop("request_id")
    unknown_error.pop("request_id")
    assert hidden_error == unknown_error


def test_hidden_asset_states_are_404(client: TestClient, seed: InventorySeed) -> None:
    for asset_id in (seed.abuja_draft, seed.abuja_inactive_vendor):
        assert client.get(f"/api/v1/assets/{asset_id}").status_code == 404
    detail = client.get(f"/api/v1/assets/{seed.abuja_camera}")
    assert detail.status_code == 200
    assert detail.json()["display_name"] == "Sony FX3"


def test_lagos_launched_via_override_returns_lagos(app, client: TestClient, seed) -> None:
    app.dependency_overrides[get_launched_cities] = lambda: ["abuja", "lagos"]
    try:
        assert _ids(client.get("/api/v1/assets", params={"city": "lagos"})) == {
            str(seed.lagos_camera)
        }
        assert _ids(client.get("/api/v1/assets")) == {
            str(seed.abuja_camera),
            str(seed.abuja_light),
            str(seed.lagos_camera),
        }
        detail = client.get(f"/api/v1/assets/{seed.lagos_camera}")
        assert detail.status_code == 200
        assert detail.json()["city"] == "lagos"
    finally:
        app.dependency_overrides.clear()


def test_inactive_city_row_hides_launched_city(client: TestClient, seed: InventorySeed) -> None:
    asyncio.run(_execute("UPDATE cities SET is_active = false WHERE slug = 'abuja'"))
    assert _ids(client.get("/api/v1/assets")) == set()


def test_live_booking_hides_asset_for_dates(client: TestClient, seed: InventorySeed) -> None:
    asyncio.run(_insert_booking(seed, "confirmed"))
    params = {"starts_on": "2026-11-03", "ends_on": "2026-11-04"}
    assert str(seed.abuja_camera) not in _ids(client.get("/api/v1/assets", params=params))
    params = {"starts_on": "2026-11-06", "ends_on": "2026-11-07"}
    assert str(seed.abuja_camera) in _ids(client.get("/api/v1/assets", params=params))


@pytest.mark.xfail(
    raises=asyncpg.InvalidTextRepresentationError,
    strict=True,
    reason="'requested' arrives with the booking status rename (Task 2.3); remove this mark then",
)
def test_requested_booking_hides_asset_for_dates(client: TestClient, seed: InventorySeed) -> None:
    asyncio.run(_insert_booking(seed, "requested"))
    params = {"starts_on": "2026-11-03", "ends_on": "2026-11-04"}
    assert str(seed.abuja_camera) not in _ids(client.get("/api/v1/assets", params=params))


def test_pending_booking_does_not_hide_asset(client: TestClient, seed: InventorySeed) -> None:
    asyncio.run(_insert_booking(seed, "pending"))
    params = {"starts_on": "2026-11-03", "ends_on": "2026-11-04"}
    assert str(seed.abuja_camera) in _ids(client.get("/api/v1/assets", params=params))


def test_blocked_window_hides_asset_for_dates(client: TestClient, seed: InventorySeed) -> None:
    asyncio.run(
        _execute(
            "INSERT INTO availability_windows (asset_id, starts_at, ends_at, is_blocked) "
            "VALUES ($1, DATE '2026-12-20', DATE '2026-12-25', true)",
            seed.abuja_camera,
        )
    )
    params = {"starts_on": "2026-12-22", "ends_on": "2026-12-22"}
    assert str(seed.abuja_camera) not in _ids(client.get("/api/v1/assets", params=params))


def test_category_rate_filters_and_pagination(client: TestClient, seed: InventorySeed) -> None:
    assert _ids(client.get("/api/v1/assets", params={"category": "lighting"})) == {
        str(seed.abuja_light)
    }
    assert _ids(client.get("/api/v1/assets", params={"min_daily_rate": "50000"})) == {
        str(seed.abuja_camera)
    }
    assert _ids(client.get("/api/v1/assets", params={"max_daily_rate": "50000"})) == {
        str(seed.abuja_light)
    }

    first = client.get("/api/v1/assets", params={"limit": 1}).json()
    cursor = first["pagination"]["next_cursor"]
    assert cursor is not None
    second = client.get("/api/v1/assets", params={"limit": 1, "cursor": cursor}).json()
    assert second["pagination"]["next_cursor"] is None
    seen = {first["items"][0]["id"], second["items"][0]["id"]}
    assert seen == {str(seed.abuja_camera), str(seed.abuja_light)}


def test_public_response_excludes_restricted_fields(
    client: TestClient, seed: InventorySeed
) -> None:
    asyncio.run(
        _execute(
            "UPDATE assets SET serial_number = 'SECRET-SN-001' WHERE id = $1", seed.abuja_camera
        )
    )
    body = client.get(f"/api/v1/assets/{seed.abuja_camera}").json()
    for field in ("serial_number", "vendor_id", "condition_notes", "listing_status"):
        assert field not in body
    assert "SECRET-SN-001" not in str(body)


async def _insert_booking(seed: InventorySeed, status: str) -> None:
    await _execute(
        "INSERT INTO bookings "
        "(asset_id, renter_id, vendor_id, starts_on, ends_on, status, "
        "total_rental_fee, deposit_amount) "
        "VALUES ($1, $2, $3, $4, $5, $6, 225000, 200000)",
        seed.abuja_camera,
        seed.renter_id,
        seed.vendor_id,
        date(2026, 11, 1),
        date(2026, 11, 5),
        status,
    )


# ---------------------------------------------------------------------------
# Service level
# ---------------------------------------------------------------------------


async def _service_search(**kwargs):
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            return await search_public_assets(session, **kwargs)
    finally:
        await engine.dispose()


async def test_search_text_query_matches_make_model_and_aliases(seed: InventorySeed) -> None:
    base = dict(launched_cities=["abuja"], city=None, category=None)

    async def ids(q: str | None) -> set[uuid.UUID]:
        page = await _service_search(q=q, **base)
        return {row.asset.id for row in page.items}

    assert await ids("fx3") == {seed.abuja_camera}
    assert await ids("sony fx3") == {seed.abuja_camera}
    assert await ids("cinema") == {seed.abuja_camera}
    assert await ids("aputure") == {seed.abuja_light}
    assert await ids("hasselblad") == set()
    assert await ids(None) == {seed.abuja_camera, seed.abuja_light}


async def test_service_pagination_cursor(seed: InventorySeed) -> None:
    base = dict(launched_cities=["abuja"], limit=1)
    first = await _service_search(**base)
    assert first.next_cursor is not None
    second = await _service_search(cursor=first.next_cursor, **base)
    assert second.next_cursor is None
    assert {first.items[0].asset.id, second.items[0].asset.id} == {
        seed.abuja_camera,
        seed.abuja_light,
    }


async def test_service_rows_carry_slugs_and_catalogue_names(seed: InventorySeed) -> None:
    page = await _service_search(
        launched_cities=["lagos"],
        city="lagos",
        category="camera",
        min_daily_rate=Decimal("1"),
    )
    assert [row.asset.id for row in page.items] == [seed.lagos_camera]
    row = page.items[0]
    assert (row.city_slug, row.category_slug, row.make, row.model) == (
        "lagos",
        "camera",
        "Sony",
        "FX3",
    )


async def test_service_rejects_unlaunched_city_and_hides_detail(seed: InventorySeed) -> None:
    with pytest.raises(CityNotLaunchedError) as excinfo:
        await _service_search(launched_cities=["abuja"], city="lagos", category=None)
    assert excinfo.value.error_details == {"city": "lagos"}

    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    try:
        async with async_sessionmaker(engine)() as session:
            with pytest.raises(ResourceNotFoundError):
                await get_public_asset(session, seed.lagos_camera, launched_cities=["abuja"])
            row = await get_public_asset(session, seed.lagos_camera, launched_cities=["lagos"])
            assert row.city_slug == "lagos"
    finally:
        await engine.dispose()
