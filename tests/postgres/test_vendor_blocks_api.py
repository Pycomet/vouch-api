"""Live PostgreSQL tests for availability blocks (M3a 4.6, §8; rulings S3-R4, S3-R7).

The app runs in-process on the test's ``db_session`` (the ``test_vendor_assets_api``
pattern): the session override commits before each request (a SAVEPOINT release)
and rolls back after it. Tests keep plain ids and re-read rows with
``populate_existing``. The races (block against request) are in
``test_block_races.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import AuditEvent, AvailabilityWindow, User, VendorProfile, VendorStatus
from app.db.models.asset import ListingStatus, PausedBy
from app.db.models.audit import ActorRole
from app.db.models.booking import LIVE_BOOKING_STATUSES, BookingStatus
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.schemas.errors import DatesBlockedError
from app.services import blocks
from app.services.audit import Actor
from app.services.blocks import create_block
from app.services.notify import RecordingNotifier
from tests.helpers.auth import bearer
from tests.postgres.factories import DEFAULT_NOW, create_asset, create_booking, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/vendor/assets"
# DEFAULT_NOW (09:00 UTC) is 10:00 on the same day in the business zone.
TODAY = date(2026, 11, 2)
START = date(2027, 1, 4)
END = date(2027, 1, 6)


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


async def _listing(
    session: AsyncSession,
    vendor_id: uuid.UUID,
    status: ListingStatus = ListingStatus.active,
    paused_by: PausedBy | None = None,
) -> uuid.UUID:
    vendor = await session.get(User, vendor_id)
    asset = await create_asset(session, vendor=vendor, listing_status=status, paused_by=paused_by)
    return asset.id


def _blocks(asset_id: uuid.UUID) -> str:
    return f"{API}/{asset_id}/blocks"


def _span(starts_on: date = START, ends_on: date = END) -> dict[str, str]:
    return {"starts_on": starts_on.isoformat(), "ends_on": ends_on.isoformat()}


async def _windows(session: AsyncSession, asset_id: uuid.UUID) -> list[AvailabilityWindow]:
    rows = await session.scalars(
        select(AvailabilityWindow)
        .where(AvailabilityWindow.asset_id == asset_id)
        .order_by(AvailabilityWindow.starts_at)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _audits(session: AsyncSession) -> list[AuditEvent]:
    rows = await session.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity == "availability_window")
        .order_by(AuditEvent.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _block(client, headers, asset_id, starts_on=START, ends_on=END) -> dict:
    response = await client.post(_blocks(asset_id), json=_span(starts_on, ends_on), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _error(response) -> dict:
    return response.json()["error"]


# ---------------------------------------------------------------------------
# Auth on every route
# ---------------------------------------------------------------------------

_ID = "00000000-0000-4000-8000-000000000001"
_BLOCK_ID = "00000000-0000-4000-8000-000000000002"
ROUTES = [
    ("post", f"{API}/{_ID}/blocks", _span()),
    ("get", f"{API}/{_ID}/blocks", None),
    ("delete", f"{API}/{_ID}/blocks/{_BLOCK_ID}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_block_route_needs_a_token(client, method, path, body) -> None:
    kwargs = {} if body is None else {"json": body}

    response = await client.request(method.upper(), path, **kwargs)

    assert response.status_code == 401
    assert _error(response)["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize("role", [RenterRole.renter, RenterRole.admin])
async def test_every_block_route_refuses_other_roles(
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
# POST /vendor/assets/{id}/blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "paused_by"),
    [
        (ListingStatus.draft, None),
        (ListingStatus.review, None),
        (ListingStatus.active, None),
        (ListingStatus.paused, PausedBy.vendor),
        (ListingStatus.rejected, None),
    ],
)
async def test_create_block_in_any_listing_status(client, db_session, status, paused_by) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id, status, paused_by)

    response = await client.post(
        _blocks(asset_id), json=_span(), headers={**headers, "X-Request-ID": "req_block_1"}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["asset_id"] == str(asset_id)
    assert (body["starts_on"], body["ends_on"]) == (START.isoformat(), END.isoformat())
    assert body["created_at"]
    [window] = await _windows(db_session, asset_id)
    assert str(window.id) == body["id"]
    assert (window.starts_at, window.ends_at, window.is_blocked) == (START, END, True)
    [audit] = await _audits(db_session)
    assert (audit.action, audit.entity_id, audit.actor_id, audit.actor_role) == (
        "availability.blocked",
        window.id,
        vendor_id,
        "vendor",
    )
    assert audit.request_id == "req_block_1"
    assert audit.before is None
    assert audit.after == {
        "asset_id": str(asset_id),
        "starts_on": START.isoformat(),
        "ends_on": END.isoformat(),
    }


async def test_a_one_day_block_starting_today_is_allowed(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)

    body = await _block(client, headers, asset_id, TODAY, TODAY)

    assert (body["starts_on"], body["ends_on"]) == (TODAY.isoformat(), TODAY.isoformat())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"starts_on": START.isoformat()},
        {"ends_on": END.isoformat()},
        {**_span(), "is_blocked": False},
        {"starts_on": "next-monday", "ends_on": END.isoformat()},
        {"starts_on": START.isoformat(), "ends_on": "2027-02-30"},
    ],
    ids=["empty", "no-end", "no-start", "extra-field", "start-not-a-date", "end-not-a-date"],
)
async def test_create_block_bad_body_is_422(client, db_session, body) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)

    response = await client.post(_blocks(asset_id), json=body, headers=headers)

    assert response.status_code == 422, response.text
    assert "detail" in response.json()
    assert await _windows(db_session, asset_id) == []


@pytest.mark.parametrize(
    ("starts_on", "ends_on"),
    [(END, START), (TODAY - timedelta(days=1), TODAY)],
    ids=["reversed", "in-the-past"],
)
async def test_create_block_bad_dates_is_400(client, db_session, starts_on, ends_on) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)

    response = await client.post(_blocks(asset_id), json=_span(starts_on, ends_on), headers=headers)

    assert response.status_code == 400, response.text
    assert _error(response)["code"] == "invalid_request"
    assert await _windows(db_session, asset_id) == []
    assert await _audits(db_session) == []


async def test_block_on_another_vendors_asset_is_404(client, db_session) -> None:
    owner_id, _ = await _vendor(db_session)
    asset_id = await _listing(db_session, owner_id)
    _, stranger = await _vendor(db_session)

    response = await client.post(_blocks(asset_id), json=_span(), headers=stranger)

    assert response.status_code == 404
    assert _error(response)["code"] == "resource_not_found"
    assert await _windows(db_session, asset_id) == []


async def test_block_on_deleted_or_unknown_asset_is_404(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    deleted = await client.delete(f"{API}/{asset_id}", headers=headers)
    assert deleted.status_code == 204

    for target in (asset_id, uuid.uuid4()):
        response = await client.post(_blocks(target), json=_span(), headers=headers)
        assert response.status_code == 404
        assert _error(response)["code"] == "resource_not_found"
    assert await _windows(db_session, asset_id) == []


@pytest.mark.parametrize("status", [VendorStatus.pending, VendorStatus.suspended, None])
async def test_block_by_non_active_vendor_is_409(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session, status)
    asset_id = await _listing(db_session, vendor_id)

    response = await client.post(_blocks(asset_id), json=_span(), headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "vendor_not_active"
    assert await _windows(db_session, asset_id) == []
    assert await _audits(db_session) == []


@pytest.mark.parametrize(
    ("starts_on", "ends_on"),
    [
        (START, END),
        (START - timedelta(days=3), START),
        (END, END + timedelta(days=3)),
        (START + timedelta(days=1), START + timedelta(days=1)),
        (START - timedelta(days=1), END + timedelta(days=1)),
    ],
    ids=["same", "touches-start", "touches-end", "inside", "around"],
)
async def test_block_overlapping_a_block_is_409_dates_blocked(
    client, db_session, starts_on, ends_on
) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    await _block(client, headers, asset_id)

    response = await client.post(_blocks(asset_id), json=_span(starts_on, ends_on), headers=headers)

    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "dates_blocked"
    assert len(await _windows(db_session, asset_id)) == 1
    assert [audit.action for audit in await _audits(db_session)] == ["availability.blocked"]


async def test_adjacent_blocks_and_other_assets_do_not_conflict(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    other_id = await _listing(db_session, vendor_id)
    await _block(client, headers, asset_id)

    await _block(client, headers, asset_id, END + timedelta(days=1), END + timedelta(days=2))
    await _block(client, headers, asset_id, START - timedelta(days=2), START - timedelta(days=1))
    await _block(client, headers, other_id)

    assert len(await _windows(db_session, asset_id)) == 3


async def test_an_unblocked_window_does_not_count_as_a_block(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    db_session.add(AvailabilityWindow(asset_id=asset_id, starts_at=START, ends_at=END))
    await db_session.flush()

    await _block(client, headers, asset_id)

    listed = await client.get(_blocks(asset_id), headers=headers)
    assert [item["starts_on"] for item in listed.json()["items"]] == [START.isoformat()]
    assert len(await _windows(db_session, asset_id)) == 2


@pytest.mark.parametrize("status", sorted(LIVE_BOOKING_STATUSES, key=lambda s: s.value))
async def test_block_over_a_live_booking_is_409_dates_unavailable(
    client, db_session, status
) -> None:
    vendor_id, headers = await _vendor(db_session)
    vendor = await db_session.get(User, vendor_id)
    asset = await create_asset(db_session, vendor=vendor)
    asset_id = asset.id
    renter = await create_user(db_session, kyc_verified=True)
    await create_booking(db_session, asset=asset, renter=renter, status=status, starts_on=START)

    response = await client.post(
        _blocks(asset_id), json=_span(END, END + timedelta(days=4)), headers=headers
    )

    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "dates_unavailable"
    assert await _windows(db_session, asset_id) == []
    assert await _audits(db_session) == []


@pytest.mark.parametrize(
    "status",
    [BookingStatus.declined, BookingStatus.cancelled_by_renter, BookingStatus.completed],
)
async def test_block_over_a_finished_booking_is_allowed(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session)
    vendor = await db_session.get(User, vendor_id)
    asset = await create_asset(db_session, vendor=vendor)
    asset_id = asset.id
    renter = await create_user(db_session, kyc_verified=True)
    await create_booking(db_session, asset=asset, renter=renter, status=status, starts_on=START)

    await _block(client, headers, asset_id)

    assert len(await _windows(db_session, asset_id)) == 1


async def test_the_exclusion_constraint_is_translated_and_the_session_stays_usable(
    db_session, monkeypatch
) -> None:
    """The backstop: a block the explicit check missed still becomes 409 ``dates_blocked``.

    A block on the same dates is inserted after the explicit check has run (the
    check is patched to see nothing), so only ``ex_availability_windows_no_blocked_overlap``
    refuses it; the savepoint keeps the caller's transaction usable.
    """
    vendor = await create_user(db_session, role=RenterRole.vendor)
    asset = await create_asset(db_session, vendor=vendor)
    asset_id = asset.id
    actor = Actor(user_id=vendor.id, role=ActorRole.vendor)
    await create_block(
        db_session, actor=actor, asset_id=asset_id, starts_on=START, ends_on=END, today=TODAY
    )

    async def nothing(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(blocks, "_overlaps_block", nothing)
    with pytest.raises(DatesBlockedError):
        await create_block(
            db_session, actor=actor, asset_id=asset_id, starts_on=START, ends_on=END, today=TODAY
        )

    assert len(await _windows(db_session, asset_id)) == 1
    assert [audit.action for audit in await _audits(db_session)] == ["availability.blocked"]


# ---------------------------------------------------------------------------
# GET /vendor/assets/{id}/blocks
# ---------------------------------------------------------------------------


async def test_list_blocks_defaults_to_today_and_orders_by_start(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    # Rows written directly: blocks in the past cannot be made through the API.
    for starts_at, ends_at in (
        (TODAY - timedelta(days=10), TODAY - timedelta(days=4)),  # ended: hidden
        (TODAY - timedelta(days=1), TODAY - timedelta(days=1)),  # ended yesterday: hidden
        (TODAY, TODAY),  # ends today: shown
        (START + timedelta(days=20), START + timedelta(days=21)),
        (START, END),
    ):
        db_session.add(
            AvailabilityWindow(
                asset_id=asset_id, starts_at=starts_at, ends_at=ends_at, is_blocked=True
            )
        )
    db_session.add(
        AvailabilityWindow(
            asset_id=asset_id,
            starts_at=START + timedelta(days=5),
            ends_at=START + timedelta(days=6),
            is_blocked=False,
        )
    )
    await db_session.flush()

    response = await client.get(_blocks(asset_id), headers=headers)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(item["starts_on"], item["ends_on"]) for item in items] == [
        (TODAY.isoformat(), TODAY.isoformat()),
        (START.isoformat(), END.isoformat()),
        ((START + timedelta(days=20)).isoformat(), (START + timedelta(days=21)).isoformat()),
    ]
    assert {item["asset_id"] for item in items} == {str(asset_id)}


async def test_list_blocks_from_filters_on_the_end_date(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    await _block(client, headers, asset_id)
    later = await _block(
        client, headers, asset_id, START + timedelta(days=10), START + timedelta(days=12)
    )

    on_end = await client.get(_blocks(asset_id), params={"from": END.isoformat()}, headers=headers)
    after_end = await client.get(
        _blocks(asset_id), params={"from": (END + timedelta(days=1)).isoformat()}, headers=headers
    )
    long_ago = await client.get(_blocks(asset_id), params={"from": "2020-01-01"}, headers=headers)

    assert len(on_end.json()["items"]) == 2
    assert [item["id"] for item in after_end.json()["items"]] == [later["id"]]
    assert len(long_ago.json()["items"]) == 2


async def test_list_blocks_bad_from_is_422(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)

    response = await client.get(_blocks(asset_id), params={"from": "soon"}, headers=headers)

    assert response.status_code == 422


@pytest.mark.parametrize("status", [VendorStatus.pending, VendorStatus.suspended, None])
async def test_list_blocks_works_for_a_non_active_vendor(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session, status)
    asset_id = await _listing(db_session, vendor_id)
    db_session.add(
        AvailabilityWindow(asset_id=asset_id, starts_at=START, ends_at=END, is_blocked=True)
    )
    await db_session.flush()

    response = await client.get(_blocks(asset_id), headers=headers)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_list_blocks_of_another_vendors_asset_is_404(client, db_session) -> None:
    owner_id, owner = await _vendor(db_session)
    asset_id = await _listing(db_session, owner_id)
    await _block(client, owner, asset_id)
    _, stranger = await _vendor(db_session)

    response = await client.get(_blocks(asset_id), headers=stranger)

    assert response.status_code == 404
    assert _error(response)["code"] == "resource_not_found"


# ---------------------------------------------------------------------------
# DELETE /vendor/assets/{id}/blocks/{block_id}
# ---------------------------------------------------------------------------


async def test_delete_block_removes_it_and_audits(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    block = await _block(client, headers, asset_id)

    response = await client.delete(
        f"{_blocks(asset_id)}/{block['id']}", headers={**headers, "X-Request-ID": "req_unblock"}
    )

    assert response.status_code == 204
    assert response.content == b""
    assert await _windows(db_session, asset_id) == []
    blocked, unblocked = await _audits(db_session)
    assert blocked.action == "availability.blocked"
    assert (unblocked.action, unblocked.entity_id, unblocked.actor_id, unblocked.request_id) == (
        "availability.unblocked",
        uuid.UUID(block["id"]),
        vendor_id,
        "req_unblock",
    )
    assert unblocked.before == {
        "asset_id": str(asset_id),
        "starts_on": START.isoformat(),
        "ends_on": END.isoformat(),
    }
    assert unblocked.after is None
    # The dates are free again.
    await _block(client, headers, asset_id)


async def test_delete_block_twice_is_404(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    block = await _block(client, headers, asset_id)
    path = f"{_blocks(asset_id)}/{block['id']}"
    assert (await client.delete(path, headers=headers)).status_code == 204

    response = await client.delete(path, headers=headers)

    assert response.status_code == 404
    assert len(await _audits(db_session)) == 2


async def test_delete_another_vendors_block_is_404(client, db_session) -> None:
    owner_id, owner = await _vendor(db_session)
    asset_id = await _listing(db_session, owner_id)
    block = await _block(client, owner, asset_id)
    stranger_id, stranger = await _vendor(db_session)
    stranger_asset = await _listing(db_session, stranger_id)

    # Through the owner's asset, and through the stranger's own asset.
    for path in (
        f"{_blocks(asset_id)}/{block['id']}",
        f"{_blocks(stranger_asset)}/{block['id']}",
    ):
        response = await client.delete(path, headers=stranger)
        assert response.status_code == 404
        assert _error(response)["code"] == "resource_not_found"
    assert len(await _windows(db_session, asset_id)) == 1
    assert len(await _audits(db_session)) == 1


async def test_delete_block_through_another_of_my_assets_is_404(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    other_id = await _listing(db_session, vendor_id)
    block = await _block(client, headers, asset_id)

    response = await client.delete(f"{_blocks(other_id)}/{block['id']}", headers=headers)

    assert response.status_code == 404
    assert len(await _windows(db_session, asset_id)) == 1


async def test_delete_an_unblocked_window_is_404(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    window = AvailabilityWindow(asset_id=asset_id, starts_at=START, ends_at=END)
    db_session.add(window)
    await db_session.flush()
    window_id = window.id

    response = await client.delete(f"{_blocks(asset_id)}/{window_id}", headers=headers)

    assert response.status_code == 404
    assert len(await _windows(db_session, asset_id)) == 1


@pytest.mark.parametrize("status", [VendorStatus.pending, VendorStatus.suspended])
async def test_delete_block_by_non_active_vendor_is_409(client, db_session, status) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)
    block = await _block(client, headers, asset_id)
    profile = await db_session.get(VendorProfile, vendor_id)
    profile.status = status
    if status == VendorStatus.suspended:
        profile.suspended_at = DEFAULT_NOW
        profile.suspended_reason = "Test suspension"
    await db_session.flush()

    response = await client.delete(f"{_blocks(asset_id)}/{block['id']}", headers=headers)

    assert response.status_code == 409
    assert _error(response)["code"] == "vendor_not_active"
    assert len(await _windows(db_session, asset_id)) == 1
    assert len(await _audits(db_session)) == 1


async def test_delete_block_bad_ids_are_422(client, db_session) -> None:
    vendor_id, headers = await _vendor(db_session)
    asset_id = await _listing(db_session, vendor_id)

    response = await client.delete(f"{_blocks(asset_id)}/not-a-uuid", headers=headers)

    assert response.status_code == 422
