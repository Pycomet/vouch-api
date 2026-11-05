"""Live PostgreSQL tests for suspended vendors (M3a §4.7, ruling S3-R5).

A vendor whose profile is not ``active`` (``pending``, ``suspended`` or missing):

* their listings vanish from ``/assets``, ``/assets/{id}`` and ``/catalogue/search``;
* a booking request on such a listing is 404, like any hidden asset;
* they cannot accept a new request (409 ``vendor_not_active``);
* they can still sign handovers and returns on their live bookings.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import Booking, VendorProfile, VendorStatus
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentType
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.notify import RecordingNotifier
from tests.helpers.auth import bearer
from tests.postgres.factories import (
    DEFAULT_NOW,
    FIRST_START,
    create_asset,
    create_booking,
    create_payment,
    create_user,
    money_snapshot,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1"
EVIDENCE = ["https://cdn.example/handover/1.jpg"]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
async def client(
    db_session: AsyncSession, clock: FrozenClock, notifier: RecordingNotifier
) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_notifier] = lambda: notifier
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _auth(user_id: uuid.UUID, role: ActorRole) -> dict[str, str]:
    return bearer(user_id, role, SETTINGS)


async def _suspend(session: AsyncSession, vendor_id: uuid.UUID) -> None:
    profile = await session.get(VendorProfile, vendor_id)
    profile.status = VendorStatus.suspended
    profile.suspended_at = DEFAULT_NOW
    profile.suspended_reason = "Test suspension"
    await session.flush()


async def _vendor(session: AsyncSession, status: VendorStatus | None):
    return await create_user(
        session, role=RenterRole.vendor, kyc_verified=True, vendor_status=status
    )


HIDDEN_STATES = [VendorStatus.suspended, VendorStatus.pending, None]


async def _ids(client: AsyncClient, path: str) -> set[str]:
    response = await client.get(path)
    assert response.status_code == 200
    return {item["id"] for item in response.json()["items"]}


@pytest.mark.parametrize("status", HIDDEN_STATES, ids=lambda s: getattr(s, "value", "missing"))
async def test_not_active_vendor_listings_vanish_from_public_reads(
    client, db_session, status
) -> None:
    visible = await create_asset(db_session, make="Vanish", model="Shown")
    hidden = await create_asset(
        db_session, vendor=await _vendor(db_session, status), make="Vanish", model="Hidden"
    )
    visible_id, hidden_id = str(visible.id), str(hidden.id)

    assets = await _ids(client, f"{API}/assets")
    search = await _ids(client, f"{API}/catalogue/search?q=Vanish")
    detail = await client.get(f"{API}/assets/{hidden_id}")

    assert visible_id in assets and hidden_id not in assets
    assert visible_id in search and hidden_id not in search
    assert detail.status_code == 404
    assert (await client.get(f"{API}/assets/{visible_id}")).status_code == 200


async def test_suspension_hides_a_listing_that_was_visible(client, db_session) -> None:
    asset = await create_asset(db_session)
    asset_id, vendor_id = str(asset.id), asset.vendor_id
    assert (await client.get(f"{API}/assets/{asset_id}")).status_code == 200

    await _suspend(db_session, vendor_id)

    assert (await client.get(f"{API}/assets/{asset_id}")).status_code == 404
    assert asset_id not in await _ids(client, f"{API}/assets")


async def test_request_on_a_suspended_vendors_listing_is_404(client, db_session) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, vendor=await _vendor(db_session, VendorStatus.suspended))
    body = {
        "asset_id": str(asset.id),
        "starts_on": FIRST_START.isoformat(),
        "ends_on": (FIRST_START + timedelta(days=2)).isoformat(),
        "fulfilment": "pickup",
    }

    response = await client.post(
        f"{API}/bookings", json=body, headers=_auth(renter.id, ActorRole.renter)
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


async def _booking(session: AsyncSession, status: BookingStatus):
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session)
    booking = await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        price_snapshot=money_snapshot(),
    )
    return booking.id, renter.id, asset.vendor_id


async def test_suspended_vendor_cannot_accept(client, db_session) -> None:
    booking_id, _, vendor_id = await _booking(db_session, BookingStatus.requested)
    await _suspend(db_session, vendor_id)

    response = await client.post(
        f"{API}/bookings/{booking_id}/accept", headers=_auth(vendor_id, ActorRole.vendor)
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "vendor_not_active"
    booking = await db_session.get(Booking, booking_id, populate_existing=True)
    assert booking.status == BookingStatus.requested


async def test_pending_vendor_cannot_accept(client, db_session) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, vendor=await _vendor(db_session, VendorStatus.pending))
    booking = await create_booking(
        db_session, asset=asset, renter=renter, price_snapshot=money_snapshot()
    )

    response = await client.post(
        f"{API}/bookings/{booking.id}/accept", headers=_auth(asset.vendor_id, ActorRole.vendor)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vendor_not_active"


async def _system(session, booking_id, event, clock, notifier, payload=None) -> None:
    await transition(
        session,
        booking_id,
        Actor.system(),
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
    )


async def test_suspended_vendor_still_signs_handover_and_return(
    client, db_session, clock, notifier
) -> None:
    booking_id, renter_id, vendor_id = await _booking(db_session, BookingStatus.accepted)
    booking = await db_session.get(Booking, booking_id)
    snapshot = booking.price_snapshot
    total = sum(
        Decimal(snapshot[key]) for key in ("deposit", "damage_contribution", "rental_total")
    )
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=total)
    await _system(
        db_session,
        booking_id,
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": total, "payment_id": payment.id},
    )
    await _suspend(db_session, vendor_id)
    vendor, renter = _auth(vendor_id, ActorRole.vendor), _auth(renter_id, ActorRole.renter)
    sign = {"checklist": {"body": "ok"}, "evidence_urls": EVIDENCE, "sign": True}

    first = await client.post(f"{API}/bookings/{booking_id}/handover", json=sign, headers=vendor)
    second = await client.post(f"{API}/bookings/{booking_id}/handover", json=sign, headers=renter)

    assert first.status_code == 200, first.text
    assert second.json()["status"] == "in_progress"

    back_vendor = await client.post(
        f"{API}/bookings/{booking_id}/return", json=sign, headers=vendor
    )
    back_renter = await client.post(
        f"{API}/bookings/{booking_id}/return", json=sign, headers=renter
    )

    assert back_vendor.status_code == 200, back_vendor.text
    assert back_renter.json()["status"] == "returned"


async def test_suspended_vendor_still_reads_their_booking(client, db_session) -> None:
    booking_id, _, vendor_id = await _booking(db_session, BookingStatus.confirmed)
    await _suspend(db_session, vendor_id)

    response = await client.get(
        f"{API}/bookings/{booking_id}", headers=_auth(vendor_id, ActorRole.vendor)
    )

    assert response.status_code == 200
