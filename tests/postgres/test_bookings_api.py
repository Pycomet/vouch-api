"""Live PostgreSQL tests for the booking endpoints (spec 3.3, Task 4.2).

The app runs in-process (httpx ``ASGITransport``) on the test's ``db_session``:
``get_session`` is overridden to hand every request that session. Before each
request the override commits (a SAVEPOINT release, so the test's own setup rows
stay), and after it the override rolls back, so a route that forgot to commit
would lose its writes and the next request would not see them. Nothing reaches
the database for real, so these tests need no ``migrated_db`` rebuild.

The rollback expires the session's ORM objects, so tests keep plain ids and
re-read rows with ``populate_existing``.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import (
    Asset,
    AuditEvent,
    Booking,
    HandoverRecord,
    LedgerAccount,
    LedgerEntry,
    User,
)
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection, HandoverStatus
from app.db.models.payment import PaymentType
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import account_balance
from app.services.notify import RecordingNotifier
from tests.helpers.auth import bearer
from tests.postgres.factories import (
    DEFAULT_NOW,
    FIRST_START,
    create_asset,
    create_booking,
    create_handover,
    create_payment,
    create_user,
    money_snapshot,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

D = Decimal
# One Settings for the app and the tokens, with a secret long enough for HS256.
SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/bookings"
DEPOSIT = D("150000.00")
CONTRIBUTION = D("7500.00")
RENTAL = D("150000.00")
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL
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


def auth(user_id: uuid.UUID, role: ActorRole) -> dict[str, str]:
    return bearer(user_id, role, SETTINGS)


class Parties:
    """Plain ids (and their auth headers) for one booking's people."""

    def __init__(self, renter_id: uuid.UUID, vendor_id: uuid.UUID) -> None:
        self.renter_id = renter_id
        self.vendor_id = vendor_id
        self.renter = auth(renter_id, ActorRole.renter)
        self.vendor = auth(vendor_id, ActorRole.vendor)


async def _asset(session: AsyncSession, **kwargs) -> tuple[uuid.UUID, Parties]:
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, **kwargs)
    return asset.id, Parties(renter.id, asset.vendor_id)


async def _booking(session: AsyncSession, status: BookingStatus) -> tuple[uuid.UUID, Parties]:
    """A booking in ``status`` with a full money snapshot (built by factories)."""
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=DEPOSIT)
    booking = await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        deposit_amount=DEPOSIT,
        total_rental_fee=RENTAL,
        price_snapshot=money_snapshot(
            deposit=str(DEPOSIT), damage_contribution=str(CONTRIBUTION), rental=str(RENTAL)
        ),
    )
    return booking.id, Parties(renter.id, asset.vendor_id)


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


async def _load(session: AsyncSession, booking_id: uuid.UUID) -> Booking:
    return await session.get(Booking, booking_id, populate_existing=True)


async def _pay(session, booking_id, clock, notifier) -> None:
    """The payment provider's capture (no HTTP route yet: webhooks are Step 5)."""
    booking = await _load(session, booking_id)
    snapshot = booking.price_snapshot
    total = sum(D(snapshot[key]) for key in ("deposit", "damage_contribution", "rental_total")) + D(
        snapshot.get("delivery_fee", "0")
    )
    payment = await create_payment(session, booking, type=PaymentType.booking, amount=total)
    await _system(
        session,
        booking_id,
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": total, "payment_id": payment.id},
    )


async def _confirmed(session, clock, notifier) -> tuple[uuid.UUID, Parties]:
    booking_id, parties = await _booking(session, BookingStatus.accepted)
    await _pay(session, booking_id, clock, notifier)
    return booking_id, parties


async def _returned(session, clock, notifier) -> tuple[uuid.UUID, Parties]:
    booking_id, parties = await _confirmed(session, clock, notifier)
    booking = await _load(session, booking_id)
    await create_handover(session, booking, direction=HandoverDirection.pickup)
    await _system(session, booking_id, BookingEvent.handover_completed, clock, notifier)
    await create_handover(session, booking, direction=HandoverDirection.return_)
    await _system(session, booking_id, BookingEvent.return_completed, clock, notifier)
    return booking_id, parties


async def _admin(session: AsyncSession) -> dict[str, str]:
    admin = await create_user(session, role=RenterRole.admin)
    return auth(admin.id, ActorRole.admin)


def _create_body(asset_id: uuid.UUID, **overrides) -> dict:
    body = {
        "asset_id": str(asset_id),
        "starts_on": FIRST_START.isoformat(),
        "ends_on": (FIRST_START + timedelta(days=2)).isoformat(),
        "fulfilment": "pickup",
    }
    body.update(overrides)
    return body


def _handover(sign: bool = True, **checklist) -> dict:
    return {"checklist": checklist or {"body": "ok"}, "evidence_urls": EVIDENCE, "sign": sign}


async def _balance(session, account: LedgerAccount, booking_id: uuid.UUID) -> Decimal:
    return await account_balance(session, account, booking_id=booking_id)


def _error(response) -> dict:
    return response.json()["error"]


# ---------------------------------------------------------------------------
# POST /bookings
# ---------------------------------------------------------------------------


async def test_create_booking_201_requested(client, db_session, notifier) -> None:
    asset_id, parties = await _asset(db_session)

    response = await client.post(API, json=_create_body(asset_id), headers=parties.renter)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "requested"
    assert body["asset_id"] == str(asset_id)
    assert body["renter_id"] == str(parties.renter_id)
    assert body["vendor_id"] == str(parties.vendor_id)
    assert body["fulfilment_choice"] == "pickup"
    assert body["hold_expires_at"].startswith("2026-11-02T13:00:00")
    assert body["deposit_due_at"] is None
    assert body["price"] == {
        "rental_total": "150000.00",
        "delivery_fee": "0.00",
        "deposit": "150000.00",
        "damage_contribution": "0.00",
        "currency": "NGN",
    }
    assert body["money_line"] == {
        "deposit": {"amount": "150000.00", "status": "due", "held_by": "platform"},
        "rental": {"amount": "150000.00", "status": "due"},
        "next": {"label": "Awaiting vendor response", "amount_due": "0.00"},
    }
    assert notifier.templates() == ["booking.requested"]
    # Committed: a fresh read through the API still finds it.
    again = await client.get(f"{API}/{body['id']}", headers=parties.renter)
    assert again.status_code == 200
    assert again.json()["status"] == "requested"


async def test_create_booking_needs_the_renter_role(client, db_session) -> None:
    asset_id, parties = await _asset(db_session)

    response = await client.post(API, json=_create_body(asset_id), headers=parties.vendor)

    assert response.status_code == 403
    assert _error(response)["code"] == "forbidden"


@pytest.mark.parametrize(
    "overrides",
    [
        {"fulfilment": "drone-drop"},
        {"starts_on": "not-a-date"},
        {"waiver": True},  # the old field name is refused, not ignored
    ],
)
async def test_create_booking_validation_422(client, db_session, overrides) -> None:
    asset_id, parties = await _asset(db_session)

    response = await client.post(
        API, json=_create_body(asset_id, **overrides), headers=parties.renter
    )

    assert response.status_code == 422


async def test_create_booking_unknown_asset_404(client, db_session) -> None:
    _, parties = await _asset(db_session)

    response = await client.post(API, json=_create_body(uuid.uuid4()), headers=parties.renter)

    assert response.status_code == 404
    assert _error(response)["code"] == "resource_not_found"


async def test_create_booking_overlap_409(client, db_session) -> None:
    asset_id, parties = await _asset(db_session)
    first = await client.post(API, json=_create_body(asset_id), headers=parties.renter)
    assert first.status_code == 201
    other = await create_user(db_session, kyc_verified=True)

    response = await client.post(
        API, json=_create_body(asset_id), headers=auth(other.id, ActorRole.renter)
    )

    assert response.status_code == 409
    assert _error(response)["code"] == "dates_unavailable"


# ---------------------------------------------------------------------------
# accept / decline / approve
# ---------------------------------------------------------------------------


async def test_accept_sets_payment_due(client, db_session, notifier) -> None:
    asset_id, parties = await _asset(db_session)
    created = await client.post(API, json=_create_body(asset_id), headers=parties.renter)
    booking_id = created.json()["id"]

    response = await client.post(f"{API}/{booking_id}/accept", headers=parties.vendor)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["deposit_due_at"].startswith("2026-11-02T21:00:00")
    # 21:00 UTC is 22:00 on the business clock.
    assert body["money_line"]["next"] == {
        "label": "Payment due Mon 2 Nov 22:00",
        "amount_due": "300000.00",
    }
    assert notifier.templates()[-1] == "booking.accepted"


async def test_accept_after_hold_409(client, db_session, clock) -> None:
    asset_id, parties = await _asset(db_session)
    created = await client.post(API, json=_create_body(asset_id), headers=parties.renter)
    clock.advance(timedelta(hours=4, seconds=1))

    response = await client.post(f"{API}/{created.json()['id']}/accept", headers=parties.vendor)

    assert response.status_code == 409
    assert _error(response)["code"] == "hold_expired"


async def test_accept_twice_is_an_invalid_transition(client, db_session) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.accepted)

    response = await client.post(f"{API}/{booking_id}/accept", headers=parties.vendor)

    assert response.status_code == 409
    error = _error(response)
    assert error["code"] == "invalid_transition"
    assert error["details"] == {"from": "accepted", "event": "accept"}


async def test_decline_with_reason(client, db_session, notifier) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.requested)

    response = await client.post(
        f"{API}/{booking_id}/decline", json={"reason": "Booked offline"}, headers=parties.vendor
    )

    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    assert response.json()["money_line"]["next"] == {"label": "Closed", "amount_due": "0.00"}
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_id == booking_id, AuditEvent.action == "booking.declined"
        )
    )
    assert audit.reason == "Booked offline"


async def test_approve_tier_three_renter(client, db_session) -> None:
    asset_id, parties = await _asset(db_session, minimum_trust_tier=3)
    booking_id = await _requested_booking(db_session, asset_id, parties)

    response = await client.post(f"{API}/{booking_id}/approve", headers=parties.vendor)

    assert response.status_code == 200
    assert response.json()["vendor_approved_at"].startswith("2026-11-02T09:00:00")
    assert response.json()["status"] == "requested"


async def _requested_booking(session, asset_id: uuid.UUID, parties: Parties) -> uuid.UUID:
    """A requested booking of ``asset_id`` by the parties' renter (factory-built)."""
    asset = await session.get(Asset, asset_id)
    renter = await session.get(User, parties.renter_id)
    booking = await create_booking(session, asset=asset, renter=renter)
    return booking.id


async def test_approve_not_required_409(client, db_session) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.requested)

    response = await client.post(f"{API}/{booking_id}/approve", headers=parties.vendor)

    assert response.status_code == 409
    assert _error(response)["code"] == "approval_not_required"


# ---------------------------------------------------------------------------
# Auth and visibility (R19)
# ---------------------------------------------------------------------------


async def test_unauthenticated_401(client, db_session) -> None:
    booking_id, _ = await _booking(db_session, BookingStatus.requested)

    for method, path in (
        ("GET", f"{API}/{booking_id}"),
        ("POST", f"{API}/{booking_id}/accept"),
        ("POST", f"{API}/{booking_id}/cancel"),
        ("POST", API),
    ):
        response = await client.request(method, path)
        assert response.status_code == 401, path
        assert _error(response)["code"] == "unauthenticated"
        assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_party_isolation(client, db_session) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.requested)
    stranger_renter = auth((await create_user(db_session)).id, ActorRole.renter)
    stranger_vendor = auth(
        (await create_user(db_session, role=RenterRole.vendor)).id, ActorRole.vendor
    )
    unknown = await client.get(f"{API}/{uuid.uuid4()}", headers=stranger_renter)
    assert unknown.status_code == 404

    def same_as_unknown(response) -> None:
        assert response.status_code == 404
        body = _error(response)
        expected = _error(unknown)
        body.pop("request_id")
        expected.pop("request_id")
        assert body == expected

    # A stranger sees nothing, whatever they try.
    same_as_unknown(await client.get(f"{API}/{booking_id}", headers=stranger_renter))
    same_as_unknown(await client.get(f"{API}/{booking_id}", headers=stranger_vendor))
    same_as_unknown(await client.post(f"{API}/{booking_id}/accept", headers=stranger_vendor))
    same_as_unknown(await client.post(f"{API}/{booking_id}/cancel", headers=stranger_renter))
    same_as_unknown(
        await client.post(f"{API}/{booking_id}/handover", json=_handover(), headers=stranger_renter)
    )
    # The renter's own id under the vendor role is not this booking's vendor.
    same_as_unknown(
        await client.get(f"{API}/{booking_id}", headers=auth(parties.renter_id, ActorRole.vendor))
    )

    # A party with the wrong role for the action: 403.
    for path in ("accept", "decline", "approve", "claim", "resolve"):
        response = await client.post(f"{API}/{booking_id}/{path}", headers=parties.renter)
        assert response.status_code == 403, path
        assert _error(response)["code"] == "forbidden"
    response = await client.post(
        f"{API}/{booking_id}/resolve",
        json={"decision": "release", "reason": "x"},
        headers=parties.vendor,
    )
    assert response.status_code == 403

    # Admins see every booking but act only where an admin may.
    admin = await _admin(db_session)
    assert (await client.get(f"{API}/{booking_id}", headers=admin)).status_code == 200
    assert (await client.post(f"{API}/{booking_id}/accept", headers=admin)).status_code == 403

    # Both parties can read it; nothing above changed it.
    for headers in (parties.renter, parties.vendor):
        response = await client.get(f"{API}/{booking_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "requested"


async def test_bad_booking_id_422(client, db_session) -> None:
    _, parties = await _booking(db_session, BookingStatus.requested)

    response = await client.get(f"{API}/not-a-uuid", headers=parties.renter)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# cancel (A13, R14, R15)
# ---------------------------------------------------------------------------


async def test_withdraw_requested_via_api(client, db_session, notifier) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.requested)

    response = await client.post(
        f"{API}/{booking_id}/cancel", json={"reason": "Found another"}, headers=parties.renter
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled_by_renter"
    assert body["money_line"]["next"] == {"label": "Closed", "amount_due": "0.00"}
    rows = await db_session.scalars(select(LedgerEntry).where(LedgerEntry.booking_id == booking_id))
    assert rows.all() == []
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (parties.vendor_id, "booking.withdrawn")
    ]


async def test_withdraw_without_body(client, db_session) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.accepted)

    response = await client.post(f"{API}/{booking_id}/cancel", headers=parties.renter)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_renter"


async def test_vendor_cancel_needs_a_reason(client, db_session) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.accepted)

    response = await client.post(f"{API}/{booking_id}/cancel", headers=parties.vendor)

    assert response.status_code == 409
    assert _error(response)["code"] == "reason_required"


async def test_cancel_confirmed_fee_via_api(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)

    response = await client.post(f"{API}/{booking_id}/cancel", headers=parties.renter)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled_by_renter"
    fee = (RENTAL * D("0.10")).quantize(D("0.01"))
    vendor_part = (fee * D("0.50")).quantize(D("0.01"))
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking_id) == vendor_part
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking_id) == (
        fee - vendor_part
    )
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking_id) == 0
    # Net cash kept by the platform is the fee: everything else went back.
    assert await _balance(db_session, LedgerAccount.platform_cash, booking_id) == -fee


# ---------------------------------------------------------------------------
# handover / return
# ---------------------------------------------------------------------------


async def _record(session, booking_id, direction: HandoverDirection) -> HandoverRecord:
    return await session.scalar(
        select(HandoverRecord)
        .where(HandoverRecord.booking_id == booking_id, HandoverRecord.direction == direction)
        .execution_options(populate_existing=True)
    )


async def test_handover_both_sign_starts_rental(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)

    first = await client.post(
        f"{API}/{booking_id}/handover", json=_handover(body="clean"), headers=parties.vendor
    )
    assert first.status_code == 200
    assert first.json()["status"] == "confirmed"
    record = await _record(db_session, booking_id, HandoverDirection.pickup)
    assert record.status == HandoverStatus.vendor_signed
    assert record.vendor_signer_id == parties.vendor_id
    assert record.renter_signer_id is None

    second = await client.post(
        f"{API}/{booking_id}/handover",
        json={
            "checklist": {"body": "small scuff"},
            "evidence_urls": ["https://cdn.example/handover/2.jpg"],
            "sign": True,
        },
        headers=parties.renter,
    )

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "in_progress"
    assert body["money_line"]["next"]["label"].startswith("Return by ")
    record = await _record(db_session, booking_id, HandoverDirection.pickup)
    assert record.status == HandoverStatus.completed
    assert (record.vendor_signer_id, record.renter_signer_id) == (
        parties.vendor_id,
        parties.renter_id,
    )
    assert record.vendor_signed_at == record.renter_signed_at == DEFAULT_NOW
    assert record.condition_checklist == {
        "vendor": {"body": "clean"},
        "renter": {"body": "small scuff"},
    }
    assert record.evidence_urls == [EVIDENCE[0], "https://cdn.example/handover/2.jpg"]


async def test_handover_without_signing_stays_draft(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)

    response = await client.post(
        f"{API}/{booking_id}/handover", json=_handover(sign=False), headers=parties.renter
    )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    record = await _record(db_session, booking_id, HandoverDirection.pickup)
    assert record.status == HandoverStatus.draft

    # Then signing it marks the renter's side.
    response = await client.post(
        f"{API}/{booking_id}/handover", json=_handover(), headers=parties.renter
    )
    assert response.status_code == 200
    record = await _record(db_session, booking_id, HandoverDirection.pickup)
    assert record.status == HandoverStatus.renter_signed
    audits = await db_session.scalars(
        select(AuditEvent.action)
        .where(AuditEvent.entity == "handover_record")
        .order_by(AuditEvent.id)
    )
    assert audits.all() == ["handover.updated", "handover.signed"]


async def test_handover_signed_side_is_final(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)
    await client.post(f"{API}/{booking_id}/handover", json=_handover(), headers=parties.vendor)

    response = await client.post(
        f"{API}/{booking_id}/handover", json=_handover(body="changed"), headers=parties.vendor
    )

    assert response.status_code == 409
    assert _error(response)["code"] == "handover_already_signed"


async def test_handover_needs_a_confirmed_booking(client, db_session) -> None:
    booking_id, parties = await _booking(db_session, BookingStatus.accepted)

    response = await client.post(
        f"{API}/{booking_id}/handover", json=_handover(), headers=parties.vendor
    )

    assert response.status_code == 409
    assert _error(response)["details"] == {"from": "accepted", "event": "handover_completed"}
    assert await _record(db_session, booking_id, HandoverDirection.pickup) is None


@pytest.mark.parametrize(
    "body",
    [
        {"checklist": {}, "evidence_urls": [], "sign": True},
        {"checklist": {}, "evidence_urls": ["not a url"], "sign": True},
        {"checklist": {}, "evidence_urls": EVIDENCE},
    ],
)
async def test_handover_validation_422(client, db_session, clock, notifier, body) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)

    response = await client.post(f"{API}/{booking_id}/handover", json=body, headers=parties.vendor)

    assert response.status_code == 422


async def test_return_both_sign_opens_claim_window(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)
    booking = await _load(db_session, booking_id)
    await create_handover(db_session, booking, direction=HandoverDirection.pickup)
    await _system(db_session, booking_id, BookingEvent.handover_completed, clock, notifier)

    await client.post(f"{API}/{booking_id}/return", json=_handover(), headers=parties.renter)
    response = await client.post(
        f"{API}/{booking_id}/return", json=_handover(), headers=parties.vendor
    )

    assert response.status_code == 200
    assert response.json()["status"] == "returned"
    assert response.json()["claim_window_ends_at"].startswith("2026-11-03T09:00:00")
    record = await _record(db_session, booking_id, HandoverDirection.return_)
    assert record.status == HandoverStatus.completed


# ---------------------------------------------------------------------------
# claim / resolve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "status"),
    [
        (1000, 422),
        ("1e3", 422),
        ("-5", 422),
        ("10.001", 422),
        ("\u0661\u0660.\u0665\u0660", 422),
        ("12345678901.00", 422),
        (10.5, 422),
        ("10.50", 200),
    ],
)
async def test_claim_amount_validation(client, db_session, clock, notifier, amount, status) -> None:
    booking_id, parties = await _returned(db_session, clock, notifier)

    response = await client.post(
        f"{API}/{booking_id}/claim",
        json={"amount": amount, "description": "Cracked screen", "evidence_urls": EVIDENCE},
        headers=parties.vendor,
    )

    assert response.status_code == status
    if status == 200:
        assert response.json()["status"] == "disputed"
        booking = await _load(db_session, booking_id)
        assert booking.claim_amount == D("10.50")
        assert booking.claim_evidence == EVIDENCE
    else:
        assert response.json()["detail"][0]["loc"][-1] == "amount"


async def test_claim_after_window_409(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _returned(db_session, clock, notifier)
    clock.advance(timedelta(hours=25))

    response = await client.post(
        f"{API}/{booking_id}/claim",
        json={"amount": "100.00", "description": "Late", "evidence_urls": EVIDENCE},
        headers=parties.vendor,
    )

    assert response.status_code == 409
    assert _error(response)["code"] == "claim_window_closed"


async def test_resolve_deducts(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _returned(db_session, clock, notifier)
    claim = await client.post(
        f"{API}/{booking_id}/claim",
        json={"amount": "20000.00", "description": "Cracked screen", "evidence_urls": EVIDENCE},
        headers=parties.vendor,
    )
    assert claim.json()["money_line"]["next"]["label"] == "Under review"
    admin = await _admin(db_session)

    response = await client.post(
        f"{API}/{booking_id}/resolve",
        json={"decision": "deduct", "reason": "Photos agree", "amount": "20000.00"},
        headers=admin,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["money_line"]["deposit"]["status"] == "deducted"
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking_id) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "keep", "reason": "x"},
        {"decision": "release", "reason": ""},
        {"decision": "deduct", "reason": "x", "amount": 20000},
    ],
)
async def test_resolve_validation_422(client, db_session, body) -> None:
    booking_id, _ = await _booking(db_session, BookingStatus.disputed)
    admin = await _admin(db_session)

    response = await client.post(f"{API}/{booking_id}/resolve", json=body, headers=admin)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The money line through the API
# ---------------------------------------------------------------------------


async def test_money_line_through_lifecycle(client, db_session, clock, notifier) -> None:
    asset_id, parties = await _asset(db_session, deposit_amount=DEPOSIT)

    def check(body: dict, deposit: str, rental: str, label: str, due: Decimal) -> None:
        line = body["money_line"]
        assert line["deposit"]["status"] == deposit, body["status"]
        assert line["rental"]["status"] == rental, body["status"]
        assert line["next"]["label"] == label, body["status"]
        assert line["next"]["amount_due"] == f"{due:.2f}", body["status"]
        assert D(line["next"]["amount_due"]) == due
        assert line["deposit"]["amount"] == body["price"]["deposit"]
        assert D(line["rental"]["amount"]) == D(body["price"]["rental_total"]) + D(
            body["price"]["delivery_fee"]
        )

    created = await client.post(
        API, json=_create_body(asset_id, damage_contribution=True), headers=parties.renter
    )
    body = created.json()
    booking_id = body["id"]
    price = body["price"]
    total = sum(D(price[key]) for key in ("deposit", "damage_contribution", "rental_total"))
    total += D(price["delivery_fee"])
    assert total == DEPOSIT + D("7500.00") + RENTAL  # 5% contribution on the rental
    check(body, "due", "due", "Awaiting vendor response", D("0"))

    body = (await client.post(f"{API}/{booking_id}/accept", headers=parties.vendor)).json()
    check(body, "due", "due", "Payment due Mon 2 Nov 22:00", total)

    await _pay(db_session, uuid.UUID(booking_id), clock, notifier)
    body = (await client.get(f"{API}/{booking_id}", headers=parties.renter)).json()
    check(body, "held", "received", "Handover Mon 4 Jan", D("0"))

    await client.post(f"{API}/{booking_id}/handover", json=_handover(), headers=parties.vendor)
    body = (
        await client.post(f"{API}/{booking_id}/handover", json=_handover(), headers=parties.renter)
    ).json()
    assert body["status"] == "in_progress"
    check(body, "held", "received", "Return by Wed 6 Jan", D("0"))

    await client.post(f"{API}/{booking_id}/return", json=_handover(), headers=parties.renter)
    body = (
        await client.post(f"{API}/{booking_id}/return", json=_handover(), headers=parties.vendor)
    ).json()
    assert body["status"] == "returned"
    check(body, "held", "received", "Claim window closes Tue 3 Nov 10:00", D("0"))

    clock.advance(timedelta(hours=25))
    await _system(db_session, uuid.UUID(booking_id), BookingEvent.complete, clock, notifier)
    body = (await client.get(f"{API}/{booking_id}", headers=parties.vendor)).json()
    assert body["status"] == "completed"
    check(body, "released", "received", "Closed", D("0"))


async def test_money_line_after_renter_cancel_with_fee(client, db_session, clock, notifier) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)
    before = (await client.get(f"{API}/{booking_id}", headers=parties.renter)).json()
    assert before["money_line"]["deposit"]["status"] == "held"
    assert before["money_line"]["rental"]["status"] == "received"
    assert before["money_line"]["next"] == {"label": "Handover Mon 4 Jan", "amount_due": "0.00"}

    response = await client.post(f"{API}/{booking_id}/cancel", headers=parties.renter)

    line = response.json()["money_line"]
    assert line == {
        "deposit": {"amount": "150000.00", "status": "released", "held_by": "platform"},
        "rental": {"amount": "150000.00", "status": "refunded"},
        "next": {"label": "Closed", "amount_due": "0.00"},
    }


# ---------------------------------------------------------------------------
# Request id (R16)
# ---------------------------------------------------------------------------


async def test_request_id_recorded_on_audit(client, db_session) -> None:
    asset_id, parties = await _asset(db_session)

    created = await client.post(
        API,
        json=_create_body(asset_id),
        headers={**parties.renter, "X-Request-ID": "req_create_1"},
    )
    booking_id = created.json()["id"]
    await client.post(
        f"{API}/{booking_id}/accept",
        headers={**parties.vendor, "X-Request-ID": "req_accept_1"},
    )

    rows = await db_session.execute(
        select(AuditEvent.action, AuditEvent.request_id)
        .where(AuditEvent.entity_id == uuid.UUID(booking_id))
        .order_by(AuditEvent.id)
    )
    assert rows.all() == [
        ("booking.requested", "req_create_1"),
        ("booking.accepted", "req_accept_1"),
    ]


async def test_money_line_after_vendor_cancel_of_paid_booking(
    client, db_session, clock, notifier
) -> None:
    booking_id, parties = await _confirmed(db_session, clock, notifier)

    response = await client.post(
        f"{API}/{booking_id}/cancel", json={"reason": "Kit unavailable"}, headers=parties.vendor
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled_by_vendor"
    assert body["money_line"] == {
        "deposit": {"amount": "150000.00", "status": "released", "held_by": "platform"},
        "rental": {"amount": "150000.00", "status": "refunded"},
        "next": {"label": "Closed", "amount_due": "0.00"},
    }
