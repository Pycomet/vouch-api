"""Live PostgreSQL tests for the vendor's own listings (M3a 4.4, 4.5, §8).

The app runs in-process on the test's ``db_session`` (the ``test_bookings_api``
pattern): the session override commits before each request (a SAVEPOINT release)
and rolls back after it, so a route that forgot to commit loses its writes.
Tests keep plain ids and re-read rows with ``populate_existing``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import (
    Asset,
    AuditEvent,
    Booking,
    CatalogueItem,
    Category,
    User,
    VendorStatus,
)
from app.db.models.asset import ListingStatus, PausedBy
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.services.notify import RecordingNotifier
from tests.helpers.auth import bearer
from tests.postgres.factories import (
    DEFAULT_NOW,
    FIRST_START,
    create_asset,
    create_booking,
    create_user,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/vendor/assets"
CITY = SETTINGS.launched_cities[0]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
async def client(db_session: AsyncSession, clock: FrozenClock) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_notifier] = lambda: RecordingNotifier()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _auth(user_id: uuid.UUID, role: ActorRole = ActorRole.vendor) -> dict[str, str]:
    return bearer(user_id, role, SETTINGS)


async def _vendor(
    session: AsyncSession, status: VendorStatus | None = VendorStatus.active
) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(session, role=RenterRole.vendor, vendor_status=status)
    return user.id, _auth(user.id)


@dataclass(frozen=True)
class ItemRef:
    """Plain ids of a catalogue item (ORM rows expire when a request rolls back)."""

    id: uuid.UUID
    category_id: int


async def _item(
    session: AsyncSession, *, band: str = "B", category: str = "camera", active: bool = True
) -> ItemRef:
    category_id = await session.scalar(select(Category.id).where(Category.slug == category))
    item = CatalogueItem(
        category_id=category_id,
        make="Sony",
        model=f"FX3 {uuid.uuid4().hex[:8]}",
        replacement_value_band=band,
        is_active=active,
    )
    session.add(item)
    await session.flush()
    return ItemRef(item.id, item.category_id)


def _body(item_id: uuid.UUID, **overrides) -> dict:
    body = {
        "catalogue_item_id": str(item_id),
        "city": CITY,
        "serial_number": "SN-0001",
        "condition_grade": "excellent",
        "daily_rate": "50000.00",
        "deposit_amount": "150000.00",
        "included_accessories": ["2 batteries"],
        "fulfilment": {"pickup": True, "delivery": True, "delivery_fee": "5000.00"},
    }
    body.update(overrides)
    return body


async def _create(client, session, headers, **overrides) -> dict:
    item = await _item(session)
    response = await client.post(API, json=_body(item.id, **overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _asset(session: AsyncSession, asset_id) -> Asset:
    return await session.get(Asset, uuid.UUID(str(asset_id)), populate_existing=True)


async def _audits(session: AsyncSession, asset_id) -> list[AuditEvent]:
    rows = await session.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity == "asset", AuditEvent.entity_id == uuid.UUID(str(asset_id)))
        .order_by(AuditEvent.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _listing(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    status: ListingStatus,
    paused_by: PausedBy | None = None,
    serial: str | None = "SN-LIVE-1",
) -> uuid.UUID:
    """A listing in ``status`` (set by the factory, as S3-R1 allows)."""
    vendor = await session.get(User, vendor_id)
    asset = await create_asset(session, vendor=vendor, listing_status=status, paused_by=paused_by)
    asset.serial_number = serial
    await session.flush()
    return asset.id


def _error(response) -> dict:
    return response.json()["error"]


# ---------------------------------------------------------------------------
# Auth on every route
# ---------------------------------------------------------------------------

_ID = "00000000-0000-4000-8000-000000000001"
ROUTES = [
    ("post", API, {}),
    ("get", API, None),
    ("get", f"{API}/{_ID}", None),
    ("patch", f"{API}/{_ID}", {"daily_rate": "1.00"}),
    ("post", f"{API}/{_ID}/submit", None),
    ("post", f"{API}/{_ID}/pause", None),
    ("post", f"{API}/{_ID}/resume", None),
    ("delete", f"{API}/{_ID}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_route_needs_a_token(client, method, path, body) -> None:
    kwargs = {} if body is None else {"json": body}

    response = await client.request(method.upper(), path, **kwargs)

    assert response.status_code == 401
    assert _error(response)["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize("role", [RenterRole.renter, RenterRole.admin])
async def test_every_route_refuses_other_roles(
    client, db_session, method, path, body, role
) -> None:
    user = await create_user(db_session, role=role)
    kwargs = {} if body is None else {"json": body}

    response = await client.request(
        method.upper(), path, headers=_auth(user.id, ActorRole(role.value)), **kwargs
    )

    assert response.status_code == 403
    assert _error(response)["code"] == "forbidden"


# ---------------------------------------------------------------------------
# POST /vendor/assets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("band", "tier"), [("A", 1), ("B", 2), ("C", 3)])
async def test_create_is_a_draft_with_the_band_default_tier(client, db_session, band, tier) -> None:
    vendor_id, headers = await _vendor(db_session)
    item = await _item(db_session, band=band, category="lens")
    item_id, category_id = item.id, item.category_id

    response = await client.post(
        API, json=_body(item_id), headers={**headers, "X-Request-ID": "req_list_1"}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["listing_status"] == "draft"
    assert body["minimum_trust_tier"] == tier
    assert body["category"] == "lens"
    assert body["city"] == CITY
    assert body["serial_number"] == "SN-0001"
    assert body["daily_rate"] == "50000.00"
    assert body["weekend_rate"] is None
    assert body["fulfilment"] == {"pickup": True, "delivery": True, "delivery_fee": "5000.00"}
    assert body["paused_by"] is None and body["review_reason"] is None
    asset = await _asset(db_session, body["id"])
    assert asset.vendor_id == vendor_id
    assert asset.category_id == category_id
    assert asset.fulfilment == {"pickup": True, "delivery": True, "delivery_fee": "5000.00"}
    [audit] = await _audits(db_session, body["id"])
    assert (audit.action, audit.actor_id, audit.actor_role, audit.request_id) == (
        "asset.created",
        vendor_id,
        "vendor",
        "req_list_1",
    )
    assert audit.after["daily_rate"] == "50000.00"
    assert audit.after["minimum_trust_tier"] == tier


async def test_create_minimal_body_defaults(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session)
    body = {
        "catalogue_item_id": str(item.id),
        "city": CITY.upper(),
        "daily_rate": "1000",
        "deposit_amount": "0",
    }

    response = await client.post(API, json=body, headers=headers)

    assert response.status_code == 201, response.text
    out = response.json()
    assert out["serial_number"] is None and out["condition_grade"] is None
    assert out["fulfilment"] == {"pickup": True, "delivery": False, "delivery_fee": None}
    assert out["included_accessories"] == []
    assert (out["daily_rate"], out["deposit_amount"]) == ("1000.00", "0.00")


async def test_create_may_raise_the_tier(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session, band="A")

    response = await client.post(API, json=_body(item.id, minimum_trust_tier=3), headers=headers)

    assert response.status_code == 201
    assert response.json()["minimum_trust_tier"] == 3


async def test_create_below_the_band_default_is_422(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session, band="C")

    response = await client.post(API, json=_body(item.id, minimum_trust_tier=2), headers=headers)

    assert response.status_code == 422
    [issue] = response.json()["detail"]
    assert issue["loc"] == ["body", "minimum_trust_tier"]
    assert issue["type"] == "below_band_default"
    assert (
        await db_session.scalar(select(Asset.id).where(Asset.catalogue_item_id == item.id)) is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimum_trust_tier": 4},
        {"category_id": 1},
        {"category": "camera"},
        {"daily_rate": 50000},
        {"daily_rate": "0.00"},
        {"daily_rate": "-1.00"},
        {"deposit_amount": "1.234"},
        {"weekend_rate": "0"},
        {"condition_grade": "mint"},
        {"serial_number": "  "},
        {"fulfilment": {"pickup": True}},
        {"fulfilment": {"pickup": True, "delivery": True, "delivery_fee": 5000}},
        {"included_accessories": ["x"] * 51},
        {"listing_status": "active"},
        {"vendor_id": _ID},
    ],
)
async def test_create_bad_body_is_422(client, db_session, overrides) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session)

    response = await client.post(API, json=_body(item.id, **overrides), headers=headers)

    assert response.status_code == 422, response.text


async def test_create_unknown_item_is_422(client, db_session) -> None:
    _, headers = await _vendor(db_session)

    response = await client.post(API, json=_body(uuid.uuid4()), headers=headers)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "catalogue_item_id"]


async def test_create_inactive_item_is_409(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session, active=False)

    response = await client.post(API, json=_body(item.id), headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "catalogue_item_inactive"


@pytest.mark.parametrize("city", ["no-such-city", "lagos" if CITY != "lagos" else "abuja"])
async def test_create_city_must_be_launched(client, db_session, city) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session)

    response = await client.post(API, json=_body(item.id, city=city), headers=headers)

    assert response.status_code == 400
    assert _error(response)["code"] == "city_not_launched"


async def test_create_duplicate_serial_is_409(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    other_id, other = await _vendor(db_session)
    item = await _item(db_session)
    first = await client.post(API, json=_body(item.id, serial_number="ab-1"), headers=headers)
    assert first.status_code == 201

    again = await client.post(API, json=_body(item.id, serial_number="AB-1"), headers=headers)
    elsewhere = await client.post(API, json=_body(item.id, serial_number="AB-1"), headers=other)

    assert again.status_code == 409
    assert _error(again)["code"] == "duplicate_serial"
    assert elsewhere.status_code == 201
    # The failed insert rolled back to its savepoint: one audit row per listing.
    assert len(await _audits(db_session, first.json()["id"])) == 1


@pytest.mark.parametrize("status", [VendorStatus.suspended, VendorStatus.pending, None])
async def test_create_by_a_vendor_not_active_is_409(client, db_session, status) -> None:
    _, headers = await _vendor(db_session, status)
    item = await _item(db_session)

    response = await client.post(API, json=_body(item.id), headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "vendor_not_active"


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


async def test_list_is_own_not_deleted_newest_first(client, db_session, clock) -> None:
    vendor_id, headers = await _vendor(db_session)
    _, other = await _vendor(db_session)
    first = await _create(client, db_session, headers, serial_number="S1")
    second = await _create(client, db_session, headers, serial_number="S2")
    gone = await _create(client, db_session, headers, serial_number="S3")
    await _create(client, db_session, other)
    assert (await client.delete(f"{API}/{gone['id']}", headers=headers)).status_code == 204
    # Distinct creation times, so "newest first" is not decided by the id tiebreak.
    for offset, asset_id in ((1, first["id"]), (2, second["id"])):
        asset = await _asset(db_session, asset_id)
        asset.created_at = DEFAULT_NOW + timedelta(minutes=offset)
    await db_session.flush()

    page1 = await client.get(API, params={"limit": 1}, headers=headers)
    page2 = await client.get(
        API,
        params={"limit": 1, "cursor": page1.json()["pagination"]["next_cursor"]},
        headers=headers,
    )

    assert [item["id"] for item in page1.json()["items"]] == [second["id"]]
    assert [item["id"] for item in page2.json()["items"]] == [first["id"]]
    assert page2.json()["pagination"]["next_cursor"] is None


async def test_list_filters_by_status(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    draft = await _create(client, db_session, headers)
    active_id = await _listing(db_session, vendor_id, ListingStatus.active)

    drafts = await client.get(API, params={"status": "draft"}, headers=headers)
    actives = await client.get(API, params={"status": "active"}, headers=headers)
    bad = await client.get(API, params={"status": "deleted"}, headers=headers)

    assert [item["id"] for item in drafts.json()["items"]] == [draft["id"]]
    assert [item["id"] for item in actives.json()["items"]] == [str(active_id)]
    assert bad.status_code == 422


async def test_list_bad_cursor_is_400(client, db_session) -> None:
    _, headers = await _vendor(db_session)

    response = await client.get(API, params={"cursor": "not-a-cursor"}, headers=headers)

    assert response.status_code == 400
    assert _error(response)["code"] == "invalid_cursor"


async def test_get_shows_the_vendor_everything(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.paused, PausedBy.vendor)
    asset = await _asset(db_session, asset_id)
    asset.condition_notes = "Dent on the cage"
    asset.review_reason = "Earlier photos were blurry"
    await db_session.flush()

    response = await client.get(f"{API}/{asset_id}", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["serial_number"] == "SN-LIVE-1"
    assert body["condition_notes"] == "Dent on the cage"
    assert body["review_reason"] == "Earlier photos were blurry"
    assert (body["listing_status"], body["paused_by"]) == ("paused", "vendor")


async def test_reads_work_for_a_suspended_vendor(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session, VendorStatus.suspended)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)

    listed = await client.get(API, headers=headers)
    detail = await client.get(f"{API}/{asset_id}", headers=headers)

    assert [item["id"] for item in listed.json()["items"]] == [str(asset_id)]
    assert detail.status_code == 200


ACTIONS = [
    ("get", "", None),
    ("patch", "", {"description": "mine now"}),
    ("post", "/submit", None),
    ("post", "/pause", None),
    ("post", "/resume", None),
    ("delete", "", None),
]


@pytest.mark.parametrize(("method", "suffix", "body"), ACTIONS)
async def test_another_vendors_listing_is_404_everywhere(
    client, db_session, method, suffix, body
) -> None:
    owner_id, _ = await _vendor(db_session)
    _, intruder = await _vendor(db_session)
    asset_id = await _listing(db_session, owner_id, ListingStatus.active)
    kwargs = {} if body is None else {"json": body}

    theirs = await client.request(
        method.upper(), f"{API}/{asset_id}{suffix}", headers=intruder, **kwargs
    )
    unknown = await client.request(
        method.upper(), f"{API}/{uuid.uuid4()}{suffix}", headers=intruder, **kwargs
    )

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() | {"error": {}} == unknown.json() | {"error": {}}
    assert _error(theirs)["code"] == _error(unknown)["code"] == "resource_not_found"
    asset = await _asset(db_session, asset_id)
    assert (asset.listing_status, asset.description, asset.deleted_at) == (
        ListingStatus.active,
        None,
        None,
    )
    assert await _audits(db_session, asset_id) == []


@pytest.mark.parametrize(("method", "suffix", "body"), ACTIONS)
async def test_a_deleted_listing_is_404(client, db_session, method, suffix, body) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)
    assert (await client.delete(f"{API}/{asset_id}", headers=headers)).status_code == 204
    kwargs = {} if body is None else {"json": body}

    response = await client.request(
        method.upper(), f"{API}/{asset_id}{suffix}", headers=headers, **kwargs
    )

    assert response.status_code == 404


@pytest.mark.parametrize("path", [f"{API}/not-a-uuid", f"{API}/not-a-uuid/submit"])
async def test_bad_id_is_422(client, db_session, path) -> None:
    _, headers = await _vendor(db_session)
    method = "GET" if path.endswith("uuid") else "POST"

    response = await client.request(method, path, headers=headers)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


async def test_patch_changes_only_what_is_sent_and_audits_the_diff(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers)

    response = await client.patch(
        f"{API}/{created['id']}",
        json={"daily_rate": "55000.00", "description": "New text", "condition_grade": "excellent"},
        headers={**headers, "X-Request-ID": "req_patch_1"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["daily_rate"] == "55000.00"
    assert body["description"] == "New text"
    assert body["deposit_amount"] == created["deposit_amount"]
    _, update = await _audits(db_session, created["id"])
    assert update.action == "asset.updated"
    assert (update.actor_id, update.request_id) == (vendor_id, "req_patch_1")
    # condition_grade was already "excellent": not a change, not audited.
    assert update.before == {"daily_rate": "50000.00", "description": None}
    assert update.after == {"daily_rate": "55000.00", "description": "New text"}


async def test_patch_with_no_change_writes_no_audit(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers)

    same = await client.patch(
        f"{API}/{created['id']}",
        json={
            "daily_rate": "50000",
            "serial_number": "SN-0001",
            "fulfilment": {"pickup": True, "delivery": True, "delivery_fee": "5000"},
        },
        headers=headers,
    )
    empty = await client.patch(f"{API}/{created['id']}", json={}, headers=headers)

    assert same.status_code == empty.status_code == 200
    assert [audit.action for audit in await _audits(db_session, created["id"])] == ["asset.created"]


async def test_patch_in_review_is_locked(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.review)

    response = await client.patch(f"{API}/{asset_id}", json={"daily_rate": "1.00"}, headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "listing_locked"
    assert _error(response)["details"] == {"status": "review"}
    assert (await _asset(db_session, asset_id)).daily_rate == Decimal("50000.00")


@pytest.mark.parametrize("status", [ListingStatus.active, ListingStatus.paused])
@pytest.mark.parametrize(
    ("field", "value"),
    [("serial_number", "SN-NEW"), ("city", "no-matter"), ("catalogue_item_id", None)],
)
async def test_patch_identity_outside_draft_is_locked(
    client, db_session, status, field, value
) -> None:
    vendor_id, headers = await _vendor(db_session)
    paused_by = PausedBy.vendor if status == ListingStatus.paused else None
    asset_id = await _listing(db_session, vendor_id, status, paused_by)
    if field == "catalogue_item_id":
        value = str((await _item(db_session)).id)

    response = await client.patch(
        f"{API}/{asset_id}", json={field: value, "description": "x"}, headers=headers
    )

    assert response.status_code == 409
    assert _error(response)["code"] == "listing_locked"
    assert _error(response)["details"] == {"status": status.value, "fields": [field]}
    assert (await _asset(db_session, asset_id)).description is None


@pytest.mark.parametrize("status", [ListingStatus.active, ListingStatus.paused])
async def test_patch_commercial_fields_while_live(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session)
    paused_by = PausedBy.vendor if status == ListingStatus.paused else None
    asset_id = await _listing(db_session, vendor_id, status, paused_by)
    body = {
        "daily_rate": "60000.00",
        "weekend_rate": "100000.00",
        "weekly_rate": "300000.00",
        "deposit_amount": "200000.00",
        "included_accessories": ["cage"],
        "fulfilment": {"pickup": False, "delivery": True, "delivery_fee": "0"},
        "description": "Updated",
        "condition_notes": "Fine",
        "condition_grade": "fair",
        # The same serial is not a change, so it is not refused.
        "serial_number": "SN-LIVE-1",
    }

    response = await client.patch(f"{API}/{asset_id}", json=body, headers=headers)

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["listing_status"] == status.value
    assert out["fulfilment"] == {"pickup": False, "delivery": True, "delivery_fee": "0.00"}
    assert (out["weekly_rate"], out["condition_grade"]) == ("300000.00", "fair")


async def test_patch_identity_in_rejected(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.rejected)

    response = await client.patch(
        f"{API}/{asset_id}", json={"serial_number": "SN-FIXED", "city": CITY}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["serial_number"] == "SN-FIXED"


@pytest.mark.parametrize(
    "body",
    [
        {"minimum_trust_tier": 3},
        {"daily_rate": None},
        {"deposit_amount": None},
        {"fulfilment": None},
        {"catalogue_item_id": None},
        {"city": None},
        {"included_accessories": None},
        {"daily_rate": 60000},
        {"category_id": 2},
        {"listing_status": "active"},
        {"paused_by": "vendor"},
    ],
)
async def test_patch_bad_body_is_422(client, db_session, body) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers)

    response = await client.patch(f"{API}/{created['id']}", json=body, headers=headers)

    assert response.status_code == 422, response.text


async def test_patch_may_clear_optional_fields(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers, weekend_rate="90000.00")

    response = await client.patch(
        f"{API}/{created['id']}",
        json={"weekend_rate": None, "serial_number": None},
        headers=headers,
    )

    assert response.status_code == 200
    assert (response.json()["weekend_rate"], response.json()["serial_number"]) == (None, None)


async def test_patch_item_rederives_category_and_raises_the_tier(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers)  # band B: tier 2
    lens = await _item(db_session, band="C", category="lens")
    lens_id, lens_category = lens.id, lens.category_id

    response = await client.patch(
        f"{API}/{created['id']}", json={"catalogue_item_id": str(lens_id)}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["category"], body["minimum_trust_tier"]) == ("lens", 3)
    _, update = await _audits(db_session, created["id"])
    assert update.after == {
        "catalogue_item_id": str(lens_id),
        "category_id": lens_category,
        "minimum_trust_tier": 3,
    }


async def test_patch_item_keeps_a_tier_already_above_the_floor(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers, minimum_trust_tier=3)
    band_a = await _item(db_session, band="A")

    response = await client.patch(
        f"{API}/{created['id']}", json={"catalogue_item_id": str(band_a.id)}, headers=headers
    )

    assert response.json()["minimum_trust_tier"] == 3


async def test_patch_item_unknown_or_inactive(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers)
    inactive = await _item(db_session, active=False)

    unknown = await client.patch(
        f"{API}/{created['id']}", json={"catalogue_item_id": str(uuid.uuid4())}, headers=headers
    )
    retired = await client.patch(
        f"{API}/{created['id']}", json={"catalogue_item_id": str(inactive.id)}, headers=headers
    )

    assert unknown.status_code == 422
    assert retired.status_code == 409
    assert _error(retired)["code"] == "catalogue_item_inactive"


async def test_patch_city_must_be_launched(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    created = await _create(client, db_session, headers)

    response = await client.patch(
        f"{API}/{created['id']}", json={"city": "no-such-city"}, headers=headers
    )

    assert response.status_code == 400
    assert _error(response)["code"] == "city_not_launched"


async def test_patch_city_moves_a_draft_between_launched_cities(db_session) -> None:
    """With two launched cities, a draft moves; a launched slug with no city row is 400."""
    other = "lagos" if CITY != "lagos" else "abuja"
    settings = SETTINGS.model_copy(update={"launched_cities": [CITY, other, "ghost-town"]})
    app = create_app(settings)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    vendor_id, _ = await _vendor(db_session)
    headers = bearer(vendor_id, ActorRole.vendor, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        created = await _create(http, db_session, headers)
        moved = await http.patch(f"{API}/{created['id']}", json={"city": other}, headers=headers)
        ghost = await http.patch(
            f"{API}/{created['id']}", json={"city": "ghost-town"}, headers=headers
        )

    assert moved.status_code == 200, moved.text
    assert moved.json()["city"] == other
    assert ghost.status_code == 400
    assert _error(ghost)["code"] == "city_not_launched"
    _, update = await _audits(db_session, created["id"])
    assert (update.before, update.after) == ({"city": CITY}, {"city": other})


async def test_patch_to_a_duplicate_serial_is_409(client, db_session) -> None:
    _, headers = await _vendor(db_session)
    item = await _item(db_session)
    await client.post(API, json=_body(item.id, serial_number="ONE"), headers=headers)
    second = await client.post(API, json=_body(item.id, serial_number="TWO"), headers=headers)

    response = await client.patch(
        f"{API}/{second.json()['id']}", json={"serial_number": "one"}, headers=headers
    )

    assert response.status_code == 409
    assert _error(response)["code"] == "duplicate_serial"
    assert (await _asset(db_session, second.json()["id"])).serial_number == "TWO"


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("patch", "", {"description": "x"}),
        ("post", "/submit", None),
        ("post", "/pause", None),
        ("post", "/resume", None),
        ("delete", "", None),
    ],
)
async def test_writes_by_a_suspended_vendor_are_409(
    client, db_session, method, suffix, body
) -> None:
    vendor_id, headers = await _vendor(db_session, VendorStatus.suspended)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.paused, PausedBy.vendor)
    kwargs = {} if body is None else {"json": body}

    response = await client.request(
        method.upper(), f"{API}/{asset_id}{suffix}", headers=headers, **kwargs
    )

    assert response.status_code == 409
    assert _error(response)["code"] == "vendor_not_active"
    assert await _audits(db_session, asset_id) == []


async def test_a_price_change_leaves_existing_bookings_alone(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    renter = await create_user(db_session, kyc_verified=True)
    renter_id = renter.id
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)
    booking_body = {
        "asset_id": str(asset_id),
        "starts_on": FIRST_START.isoformat(),
        "ends_on": (FIRST_START + timedelta(days=2)).isoformat(),
        "fulfilment": "pickup",
    }
    created = await client.post(
        "/api/v1/bookings", json=booking_body, headers=_auth(renter_id, ActorRole.renter)
    )
    assert created.status_code == 201, created.text
    booking_id = uuid.UUID(created.json()["id"])
    before = (await db_session.get(Booking, booking_id, populate_existing=True)).price_snapshot

    patched = await client.patch(
        f"{API}/{asset_id}",
        json={"daily_rate": "99000.00", "deposit_amount": "500000.00"},
        headers=headers,
    )

    assert patched.status_code == 200
    booking = await db_session.get(Booking, booking_id, populate_existing=True)
    assert booking.price_snapshot == before
    assert booking.deposit_amount == Decimal("150000.00")
    detail = await client.get(
        f"/api/v1/bookings/{booking_id}", headers=_auth(renter_id, ActorRole.renter)
    )
    assert detail.json()["price"]["deposit"] == "150000.00"


# ---------------------------------------------------------------------------
# submit / pause / resume
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [ListingStatus.draft, ListingStatus.rejected])
async def test_submit_moves_to_review(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, status)

    response = await client.post(
        f"{API}/{asset_id}/submit", headers={**headers, "X-Request-ID": "req_submit"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["listing_status"] == "review"
    [audit] = await _audits(db_session, asset_id)
    assert (audit.action, audit.request_id, audit.actor_id) == (
        "asset.submitted",
        "req_submit",
        vendor_id,
    )
    assert audit.before == {"listing_status": status.value, "paused_by": None}
    assert audit.after == {"listing_status": "review", "paused_by": None}


async def test_submit_incomplete_lists_what_is_missing(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.draft, serial=None)
    asset = await _asset(db_session, asset_id)
    asset.condition_grade = None
    asset.fulfilment = {"pickup": False, "delivery": False}
    await db_session.flush()

    first = await client.post(f"{API}/{asset_id}/submit", headers=headers)
    asset = await _asset(db_session, asset_id)
    asset.fulfilment = {"pickup": False, "delivery": True}
    await db_session.flush()
    second = await client.post(f"{API}/{asset_id}/submit", headers=headers)

    assert first.status_code == second.status_code == 409
    assert _error(first)["code"] == "listing_incomplete"
    assert _error(first)["details"] == {
        "missing": ["serial_number", "condition_grade", "fulfilment"]
    }
    assert _error(second)["details"] == {
        "missing": ["serial_number", "condition_grade", "delivery_fee"]
    }
    assert (await _asset(db_session, asset_id)).listing_status == ListingStatus.draft
    assert await _audits(db_session, asset_id) == []


async def test_submit_with_a_retired_item_is_409(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.draft)
    item = await db_session.get(
        CatalogueItem, (await _asset(db_session, asset_id)).catalogue_item_id
    )
    item.is_active = False
    await db_session.flush()

    response = await client.post(f"{API}/{asset_id}/submit", headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "catalogue_item_inactive"


@pytest.mark.parametrize(
    ("status", "event", "paused_by"),
    [
        (ListingStatus.review, "submit", None),
        (ListingStatus.active, "submit", None),
        (ListingStatus.paused, "submit", PausedBy.vendor),
        (ListingStatus.draft, "pause", None),
        (ListingStatus.review, "pause", None),
        (ListingStatus.paused, "pause", PausedBy.vendor),
        (ListingStatus.rejected, "pause", None),
        (ListingStatus.active, "resume", None),
        (ListingStatus.draft, "resume", None),
        (ListingStatus.paused, "resume", PausedBy.admin),
    ],
)
async def test_illegal_listing_events_are_invalid_transitions(
    client, db_session, status, event, paused_by
) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, status, paused_by)

    response = await client.post(f"{API}/{asset_id}/{event}", headers=headers)

    assert response.status_code == 409
    error = _error(response)
    assert error["code"] == "invalid_transition"
    assert error["details"] == {"from": status.value, "event": event}
    assert error["message"] == "This listing cannot make that transition."
    asset = await _asset(db_session, asset_id)
    assert (asset.listing_status, asset.paused_by) == (
        status,
        None if paused_by is None else paused_by.value,
    )


async def test_pause_and_resume(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)
    assert (await client.get(f"/api/v1/assets/{asset_id}")).status_code == 200

    paused = await client.post(f"{API}/{asset_id}/pause", headers=headers)
    hidden = await client.get(f"/api/v1/assets/{asset_id}")
    resumed = await client.post(f"{API}/{asset_id}/resume", headers=headers)

    assert (paused.json()["listing_status"], paused.json()["paused_by"]) == ("paused", "vendor")
    assert hidden.status_code == 404
    assert (resumed.json()["listing_status"], resumed.json()["paused_by"]) == ("active", None)
    assert (await client.get(f"/api/v1/assets/{asset_id}")).status_code == 200
    audits = await _audits(db_session, asset_id)
    assert [(a.action, a.before, a.after) for a in audits] == [
        (
            "asset.paused",
            {"listing_status": "active", "paused_by": None},
            {"listing_status": "paused", "paused_by": "vendor"},
        ),
        (
            "asset.resumed",
            {"listing_status": "paused", "paused_by": "vendor"},
            {"listing_status": "active", "paused_by": None},
        ),
    ]


async def test_pausing_keeps_live_bookings(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)
    renter = await create_user(db_session, kyc_verified=True)
    booking = await create_booking(
        db_session,
        asset=await _asset(db_session, asset_id),
        renter=renter,
        status=BookingStatus.confirmed,
    )
    booking_id = booking.id

    response = await client.post(f"{API}/{asset_id}/pause", headers=headers)

    assert response.status_code == 200
    booking = await db_session.get(Booking, booking_id, populate_existing=True)
    assert booking.status == BookingStatus.confirmed


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


async def test_delete_is_soft_and_frees_the_serial(client, db_session, clock) -> None:
    vendor_id, headers = await _vendor(db_session)
    item = await _item(db_session)
    created = await client.post(API, json=_body(item.id), headers=headers)
    asset_id = created.json()["id"]

    response = await client.delete(
        f"{API}/{asset_id}", headers={**headers, "X-Request-ID": "req_delete"}
    )

    assert response.status_code == 204
    assert response.content == b""
    asset = await _asset(db_session, asset_id)
    assert asset.deleted_at == DEFAULT_NOW
    _, deleted = await _audits(db_session, asset_id)
    assert (deleted.action, deleted.request_id, deleted.actor_id) == (
        "asset.deleted",
        "req_delete",
        vendor_id,
    )
    again = await client.post(API, json=_body(item.id), headers=headers)
    assert again.status_code == 201


@pytest.mark.parametrize(
    "status",
    [
        BookingStatus.requested,
        BookingStatus.accepted,
        BookingStatus.deposit_paid,
        BookingStatus.confirmed,
        BookingStatus.in_progress,
        BookingStatus.returned,
        BookingStatus.disputed,
    ],
)
async def test_delete_with_a_live_or_disputed_booking_is_409(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)
    renter = await create_user(db_session, kyc_verified=True)
    await create_booking(
        db_session, asset=await _asset(db_session, asset_id), renter=renter, status=status
    )

    response = await client.delete(f"{API}/{asset_id}", headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "has_live_bookings"
    assert (await _asset(db_session, asset_id)).deleted_at is None


@pytest.mark.parametrize(
    "status", [BookingStatus.completed, BookingStatus.declined, BookingStatus.cancelled_by_renter]
)
async def test_delete_after_closed_bookings_is_allowed(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active)
    renter = await create_user(db_session, kyc_verified=True)
    await create_booking(
        db_session, asset=await _asset(db_session, asset_id), renter=renter, status=status
    )

    response = await client.delete(f"{API}/{asset_id}", headers=headers)

    assert response.status_code == 204


# ---------------------------------------------------------------------------
# The Step 3 end to end (S3-R1)
# ---------------------------------------------------------------------------


async def test_step3_end_to_end_catalogue_to_review(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    item = await _item(db_session, band="C")
    item_id = item.id

    items = await client.get("/api/v1/catalogue/items", params={"q": "Sony"})
    assert str(item_id) in [entry["id"] for entry in items.json()["items"]]
    picked = next(entry for entry in items.json()["items"] if entry["id"] == str(item_id))

    created = await client.post(
        API,
        json=_body(
            picked["id"], serial_number=None, fulfilment={"pickup": True, "delivery": False}
        ),
        headers=headers,
    )
    asset_id = created.json()["id"]
    assert created.json()["minimum_trust_tier"] == 3

    too_soon = await client.post(f"{API}/{asset_id}/submit", headers=headers)
    assert _error(too_soon)["details"] == {"missing": ["serial_number"]}

    patched = await client.patch(
        f"{API}/{asset_id}", json={"serial_number": "SN-E2E-1"}, headers=headers
    )
    assert patched.status_code == 200
    submitted = await client.post(f"{API}/{asset_id}/submit", headers=headers)

    assert submitted.status_code == 200
    assert submitted.json()["listing_status"] == "review"
    locked = await client.patch(f"{API}/{asset_id}", json={"description": "x"}, headers=headers)
    assert _error(locked)["code"] == "listing_locked"
    audits = await _audits(db_session, asset_id)
    assert [(a.action, a.actor_id, a.actor_role) for a in audits] == [
        ("asset.created", vendor_id, "vendor"),
        ("asset.updated", vendor_id, "vendor"),
        ("asset.submitted", vendor_id, "vendor"),
    ]
    assert audits[1].before == {"serial_number": None}
    assert audits[1].after == {"serial_number": "SN-E2E-1"}
    # Still not public: review is not active.
    assert (await client.get(f"/api/v1/assets/{asset_id}")).status_code == 404


async def test_public_asset_still_hides_the_serial(client, db_session) -> None:
    vendor_id, _ = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, ListingStatus.active, serial="SN-SECRET-9")
    asset = await _asset(db_session, asset_id)
    asset.condition_notes = "private note"
    asset.review_reason = "private reason"
    await db_session.flush()

    detail = await client.get(f"/api/v1/assets/{asset_id}")
    listed = await client.get("/api/v1/assets")

    assert detail.status_code == 200
    for text in (detail.text, listed.text):
        assert "SN-SECRET-9" not in text
        assert "private note" not in text and "private reason" not in text
        assert str(vendor_id) not in text
    assert "serial_number" not in detail.json()
