"""API tests for the public inventory endpoints.

Uses the TestClient with a patched service layer so no live PostgreSQL is
required.  Field-restriction and filter logic is asserted through the API
contract; real SQL behaviour is covered by ``tests/postgres/test_inventory_api.py``.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Request
from fastapi.testclient import TestClient

from app.api.dependencies import get_launched_cities
from app.core.config import Settings
from app.db.models.asset import ListingStatus
from app.schemas.assets import PublicAsset
from app.schemas.errors import ResourceNotFoundError
from app.schemas.pagination import encode_cursor
from app.services.inventory import PublicAssetRow, ensure_city_launched

ROOT = Path(__file__).parents[1]

# ---------------------------------------------------------------------------
# Fake asset row helper
# ---------------------------------------------------------------------------


def _fake_row(**overrides: Any) -> PublicAssetRow:
    """Return a ``PublicAssetRow`` around a namespace that looks like an Asset."""
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    defaults = dict(
        id=uuid.uuid4(),
        name=None,
        description="Camera body",
        daily_rate=Decimal("75000.00"),
        deposit_amount=Decimal("200000.00"),
        weekend_rate=Decimal("120000.00"),
        weekly_rate=None,
        condition_grade="excellent",
        is_available=True,
        minimum_trust_tier=1,
        listing_status=ListingStatus.active,
        created_at=now,
        updated_at=now,
        # Restricted fields that must never appear in public output:
        serial_number="SECRET-SN-001",
        vendor_id=uuid.uuid4(),
        condition_notes="Minor mark",
        review_actor_id=None,
        reviewed_at=None,
        review_reason=None,
        deleted_at=None,
    )
    defaults.update(overrides)
    return PublicAssetRow(
        asset=SimpleNamespace(**defaults),
        city_slug="abuja",
        category_slug="camera",
        make="Sony",
        model="FX3",
    )


# ---------------------------------------------------------------------------
# Collection endpoint tests (GET /api/v1/assets)
# ---------------------------------------------------------------------------


def test_list_assets_returns_200(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[row], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    resp = client.get("/api/v1/assets")
    assert resp.status_code == 200


def test_list_assets_returns_items_and_pagination(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[row], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    body = client.get("/api/v1/assets").json()
    assert "items" in body
    assert "pagination" in body
    assert body["pagination"]["next_cursor"] is None


def test_list_assets_response_excludes_restricted_fields(client: TestClient, monkeypatch) -> None:
    """Restricted fields must never appear in public collection output."""
    row = _fake_row()

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[row], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    item = client.get("/api/v1/assets").json()["items"][0]

    restricted = {
        "serial_number",
        "vendor_id",
        "condition_notes",
        "review_actor_id",
        "reviewed_at",
        "review_reason",
        "listing_status",
        "deleted_at",
    }
    for field in restricted:
        assert field not in item, f"Restricted field '{field}' must not appear in public output"


def test_list_assets_includes_required_public_fields(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[row], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    item = client.get("/api/v1/assets").json()["items"][0]

    required = {
        "id",
        "name",
        "display_name",
        "make",
        "model",
        "condition_grade",
        "weekend_rate",
        "weekly_rate",
        "category",
        "daily_rate",
        "deposit_amount",
        "city",
        "is_available",
        "minimum_trust_tier",
        "currency",
        "created_at",
        "updated_at",
    }
    for field in required:
        assert field in item, f"Required public field '{field}' is missing"


def test_list_assets_daily_rate_is_decimal_string(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[row], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    item = client.get("/api/v1/assets").json()["items"][0]
    # Must be a string (e.g. "75000.00"), not a float.
    assert isinstance(item["daily_rate"], str)
    assert "." in item["daily_rate"]


def test_list_assets_empty_result_returns_200(client: TestClient, monkeypatch) -> None:
    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    body = client.get("/api/v1/assets").json()
    assert body["items"] == []


def test_list_assets_returns_next_cursor_when_more_pages(client: TestClient, monkeypatch) -> None:
    cursor = encode_cursor("2026-10-01T12:00:00+00:00", str(uuid.uuid4()))

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[_fake_row()], next_cursor=cursor)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    body = client.get("/api/v1/assets?limit=1").json()
    assert body["pagination"]["next_cursor"] == cursor


def test_list_assets_passes_launched_cities_from_dependency(
    client: TestClient, monkeypatch
) -> None:
    seen: dict[str, Any] = {}

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        seen.update(kwargs)
        return AssetPage(items=[], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    client.app.dependency_overrides[get_launched_cities] = lambda: ["kano", "enugu"]
    try:
        assert client.get("/api/v1/assets?city=kano").status_code == 200
    finally:
        client.app.dependency_overrides.clear()
    assert seen["launched_cities"] == ["kano", "enugu"]
    assert seen["city"] == "kano"


def test_list_assets_unlaunched_city_returns_400_before_db(client: TestClient) -> None:
    """The service rejects the city before it touches the (absent) database."""
    client.app.dependency_overrides[get_launched_cities] = lambda: ["abuja"]
    try:
        resp = client.get("/api/v1/assets?city=lagos")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "city_not_launched"
    assert error["details"] == {"city": "lagos"}


def test_ensure_city_launched_accepts_launched_and_none() -> None:
    ensure_city_launched(None, ["abuja"])
    ensure_city_launched("abuja", ["abuja", "lagos"])


def test_launched_cities_come_from_the_apps_own_settings(settings: Settings) -> None:
    from app.main import create_app

    own = settings.model_copy(update={"launched_cities": ["kano", "enugu"]})
    application = create_app(own)
    request = Request({"type": "http", "app": application, "headers": []})
    assert get_launched_cities(request) == ["kano", "enugu"]


def test_city_filter_is_stripped_and_lowercased(client: TestClient, monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        seen.update(kwargs)
        return AssetPage(items=[], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    client.app.dependency_overrides[get_launched_cities] = lambda: ["abuja"]
    try:
        assert client.get("/api/v1/assets", params={"city": " Abuja "}).status_code == 200
    finally:
        client.app.dependency_overrides.clear()
    assert seen["city"] == "abuja"


def test_display_name_prefers_name_then_make_model() -> None:
    assert PublicAsset.from_row(_fake_row()).display_name == "Sony FX3"
    assert PublicAsset.from_row(_fake_row(name="A-cam")).display_name == "A-cam"


def test_no_city_literals_in_inventory_code() -> None:
    files = (
        ROOT / "src" / "app" / "services" / "inventory.py",
        ROOT / "src" / "app" / "schemas" / "assets.py",
        ROOT / "src" / "app" / "api" / "routes" / "assets.py",
    )
    pattern = re.compile("abuja|lagos", re.IGNORECASE)
    hits = [
        f"{path.name}:{lineno}"
        for path in files
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


def test_list_assets_missing_ends_on_returns_400(client: TestClient, monkeypatch) -> None:
    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    resp = client.get("/api/v1/assets?starts_on=2026-11-01")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_list_assets_reversed_dates_returns_400(client: TestClient, monkeypatch) -> None:
    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    resp = client.get("/api/v1/assets?starts_on=2026-11-05&ends_on=2026-11-01")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_list_assets_malformed_cursor_returns_400(client: TestClient, monkeypatch) -> None:
    async def mock_search(*args, **kwargs):
        # This should raise CursorDecodeError before the service is reached.
        raise RuntimeError("Should not be called")

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    resp = client.get("/api/v1/assets?cursor=not-a-valid-cursor-xyz")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_list_assets_x_request_id_echoed(client: TestClient, monkeypatch) -> None:
    async def mock_search(*args, **kwargs):
        from app.services.inventory import AssetPage

        return AssetPage(items=[], next_cursor=None)

    monkeypatch.setattr("app.api.routes.assets.search_public_assets", mock_search)
    resp = client.get("/api/v1/assets", headers={"X-Request-ID": "req-inv-001"})
    assert resp.headers.get("X-Request-ID") == "req-inv-001"


# ---------------------------------------------------------------------------
# Detail endpoint tests (GET /api/v1/assets/{asset_id})
# ---------------------------------------------------------------------------


def test_get_asset_returns_200(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_get(session, asset_id, **kwargs):
        return row

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    resp = client.get(f"/api/v1/assets/{row.asset.id}")
    assert resp.status_code == 200


def test_get_asset_excludes_restricted_fields(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_get(session, asset_id, **kwargs):
        return row

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    body = client.get(f"/api/v1/assets/{row.asset.id}").json()

    restricted = {
        "serial_number",
        "vendor_id",
        "condition_notes",
        "review_actor_id",
        "reviewed_at",
        "review_reason",
        "listing_status",
        "deleted_at",
    }
    for field in restricted:
        assert field not in body, f"Restricted field '{field}' must not appear in detail output"


def test_get_asset_not_found_returns_404(client: TestClient, monkeypatch) -> None:
    async def mock_get(session, asset_id, **kwargs):
        raise ResourceNotFoundError()

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    resp = client.get(f"/api/v1/assets/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_asset_not_found_uses_safe_code(client: TestClient, monkeypatch) -> None:
    async def mock_get(session, asset_id, **kwargs):
        raise ResourceNotFoundError()

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    body = client.get(f"/api/v1/assets/{uuid.uuid4()}").json()
    assert body["error"]["code"] == "resource_not_found"


def test_get_asset_not_found_exposes_no_listing_state(client: TestClient, monkeypatch) -> None:
    async def mock_get(session, asset_id, **kwargs):
        raise ResourceNotFoundError()

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    body = client.get(f"/api/v1/assets/{uuid.uuid4()}").json()
    body_str = str(body)
    # Ensure no internal state is leaked.
    for word in ("draft", "paused", "rejected", "listing_status", "serial_number"):
        assert word not in body_str.lower(), f"Leaked internal state: {word}"


def test_get_asset_invalid_uuid_returns_422(client: TestClient, monkeypatch) -> None:
    """FastAPI should reject non-UUID path parameters with 422."""

    async def mock_get(session, asset_id, **kwargs):
        raise RuntimeError("Should not be reached")

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    resp = client.get("/api/v1/assets/not-a-uuid")
    assert resp.status_code == 422


def test_get_asset_currency_is_ngn(client: TestClient, monkeypatch) -> None:
    row = _fake_row()

    async def mock_get(session, asset_id, **kwargs):
        return row

    monkeypatch.setattr("app.api.routes.assets.get_public_asset", mock_get)
    body = client.get(f"/api/v1/assets/{row.asset.id}").json()
    assert body["currency"] == "NGN"
