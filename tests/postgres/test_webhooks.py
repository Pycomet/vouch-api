"""Live PostgreSQL tests for ``POST /webhooks/{provider}`` (R22-R24, Task 5.4).

The app runs on the test's ``db_session`` (as in ``test_bookings_api.py``): the
override commits before each request (a SAVEPOINT release) and rolls back after
it, so only what the route committed survives. The fake provider signs every
body with :data:`SECRET`; ``app.state.payment_provider`` is that provider, so the
accept (R21) and the webhook use the same one.

The race "renter withdraws while the payment is in flight" needs two real
transactions, so it runs on ``session_factory`` against the service directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import AuditEvent, Booking, LedgerAccount, LedgerEntry, Payment, WebhookEvent
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentStatus, PaymentType, PayoutStatus
from app.db.session import get_session
from app.main import create_app
from app.providers.payment import FakePaymentProvider
from app.schemas.errors import InvalidTransition
from app.services import webhooks
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import account_balance
from app.services.notify import RecordingNotifier
from app.services.webhooks import handle_webhook
from tests.postgres.factories import (
    DEFAULT_NOW,
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

D = Decimal
DEPOSIT = D("150000.00")
CONTRIBUTION = D("7500.00")
RENTAL = D("150000.00")
DELIVERY = D("5000.00")
COMMISSION = D("22500.00")  # 15% of the rental
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL + DELIVERY
SECRET = "webhook-secret-for-tests-that-is-long-enough"
SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
URL = "/api/v1/webhooks/fake"
BOOKING_ACCOUNTS = (
    LedgerAccount.renter_deposit_liability,
    LedgerAccount.protection_pool,
    LedgerAccount.vendor_payable,
    LedgerAccount.platform_revenue,
    LedgerAccount.platform_cash,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider(SECRET)


@pytest.fixture
async def client(
    db_session: AsyncSession,
    clock: FrozenClock,
    notifier: RecordingNotifier,
    provider: FakePaymentProvider,
) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)
    app.state.payment_provider = provider

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _move(session, booking_id, actor, event, clock, notifier, provider=None, **payload):
    return await transition(
        session,
        booking_id,
        actor,
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
        payment_provider=provider,
        request_id="req_setup",
    )


async def _accepted(session: AsyncSession, clock, notifier, provider) -> tuple[uuid.UUID, str]:
    """An accepted booking with its pending payment (R21); returns (id, reference)."""
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=DEPOSIT)
    booking = await create_booking(
        session,
        asset=asset,
        renter=renter,
        deposit_amount=DEPOSIT,
        total_rental_fee=RENTAL,
        price_snapshot=money_snapshot(
            deposit=str(DEPOSIT),
            damage_contribution=str(CONTRIBUTION),
            rental=str(RENTAL),
            delivery_fee=str(DELIVERY),
        ),
    )
    vendor = Actor(user_id=booking.vendor_id, role=ActorRole.vendor)
    await _move(session, booking.id, vendor, BookingEvent.accept, clock, notifier, provider)
    await session.commit()
    return booking.id, f"fake-booking-{booking.id.hex}"


def _event(provider, booking_id, reference, amount=TOTAL, type="payment_captured", **kw) -> bytes:
    return provider.make_event(
        type, booking_id=booking_id, payment_reference=reference, amount=amount, **kw
    )


async def _send(client: AsyncClient, provider, body: bytes, signature: str | None = "sign"):
    headers = {"Content-Type": "application/json", "X-Request-ID": "req_webhook"}
    if signature == "sign":
        signature = provider.sign(body)
    if signature is not None:
        headers["X-Vouch-Signature"] = signature
    return await client.post(URL, content=body, headers=headers)


async def _booking(session: AsyncSession, booking_id: uuid.UUID) -> Booking:
    return await session.get(Booking, booking_id, populate_existing=True)


async def _payments(session, booking_id, type: PaymentType | None = None) -> list[Payment]:
    query = select(Payment).where(Payment.booking_id == booking_id)
    if type is not None:
        query = query.where(Payment.type == type)
    rows = await session.scalars(
        query.order_by(Payment.created_at, Payment.id).execution_options(populate_existing=True)
    )
    return list(rows)


async def _paid_and_refund(session, booking_id) -> tuple[Payment, Payment]:
    """The booking's one upfront payment and its one refund (rows of one
    transaction share ``created_at``, so they are told apart by type)."""
    [paid] = await _payments(session, booking_id, PaymentType.booking)
    [refund] = await _payments(session, booking_id, PaymentType.refund)
    return paid, refund


async def _balances(session, booking_id) -> dict[LedgerAccount, Decimal]:
    return {
        account: await account_balance(session, account, booking_id=booking_id)
        for account in BOOKING_ACCOUNTS
    }


async def _memos(session, booking_id) -> list[str]:
    rows = await session.execute(
        select(LedgerEntry.memo, func.min(LedgerEntry.id))
        .where(LedgerEntry.booking_id == booking_id)
        .group_by(LedgerEntry.txn_group, LedgerEntry.memo)
        .order_by(func.min(LedgerEntry.id))
    )
    return [memo for memo, _ in rows.all()]


async def _counts(session) -> tuple[int, int, int, int]:
    return (
        await session.scalar(select(func.count()).select_from(LedgerEntry)),
        await session.scalar(select(func.count()).select_from(AuditEvent)),
        await session.scalar(select(func.count()).select_from(WebhookEvent)),
        await session.scalar(select(func.count()).select_from(Payment)),
    )


async def _webhook_rows(session) -> list[WebhookEvent]:
    rows = await session.scalars(
        select(WebhookEvent).order_by(WebhookEvent.id).execution_options(populate_existing=True)
    )
    return list(rows)


async def _audits(session, entity_id, action) -> list[AuditEvent]:
    rows = await session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == entity_id, AuditEvent.action == action)
    )
    return list(rows)


async def _whole_ledger_balanced(session) -> bool:
    rows = await session.execute(
        select(LedgerEntry.direction, func.coalesce(func.sum(LedgerEntry.amount), 0)).group_by(
            LedgerEntry.direction
        )
    )
    sums = {direction: D(total) for direction, total in rows.all()}
    return sums.get("dr", D(0)) == sums.get("cr", D(0))


ZEROS = dict.fromkeys(BOOKING_ACCOUNTS, D("0.00"))


# ---------------------------------------------------------------------------
# payment_captured
# ---------------------------------------------------------------------------


async def test_payment_captured_confirms_booking(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)

    response = await _send(client, provider, _event(provider, booking_id, reference))

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "processed", "processing_error": None}
    assert (await _booking(db_session, booking_id)).status == BookingStatus.confirmed
    [payment] = await _payments(db_session, booking_id)
    assert payment.status == PaymentStatus.captured
    assert payment.captured_at == DEFAULT_NOW
    assert await _balances(db_session, booking_id) == {
        LedgerAccount.renter_deposit_liability: DEPOSIT,
        LedgerAccount.protection_pool: CONTRIBUTION,
        LedgerAccount.vendor_payable: RENTAL + DELIVERY - COMMISSION,
        LedgerAccount.platform_revenue: COMMISSION,
        LedgerAccount.platform_cash: -TOTAL,
    }
    assert await _memos(db_session, booking_id) == ["payment_captured"]
    [row] = await _webhook_rows(db_session)
    assert (row.provider, row.provider_event_id) == ("fake", f"evt_payment_captured_{reference}")
    assert row.signature_ok is True
    assert row.processed_at == DEFAULT_NOW
    assert row.processing_error is None
    assert row.payload["data"]["amount"] == str(TOTAL)
    confirmed = await _audits(db_session, booking_id, "booking.confirmed")
    assert [audit.request_id for audit in confirmed] == ["req_webhook"]
    assert confirmed[0].actor_role == "system"
    assert await _whole_ledger_balanced(db_session)


async def test_webhook_replay_is_noop(client, db_session, clock, notifier, provider) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    body = _event(provider, booking_id, reference)
    first = await _send(client, provider, body)
    assert first.json()["status"] == "processed"
    before = await _counts(db_session)
    sent = len(notifier.calls)

    again = await _send(client, provider, body)

    assert again.status_code == 200
    assert again.json() == {"status": "duplicate", "processing_error": None}
    assert await _counts(db_session) == before
    assert len(notifier.calls) == sent
    assert (await _booking(db_session, booking_id)).status == BookingStatus.confirmed


async def test_bad_signature_401_and_recorded(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    body = _event(provider, booking_id, reference)

    forged = await _send(client, provider, body, signature="0" * 64)
    unsigned = await _send(client, provider, body, signature=None)

    for response in (forged, unsigned):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_signature"
    [row] = await _webhook_rows(db_session)  # one body, recorded once
    assert row.signature_ok is False
    assert row.processed_at is None
    assert row.payload["id"] == f"evt_payment_captured_{reference}"
    # Stored under the body's hash: a forged body cannot take the real event's id.
    assert row.provider_event_id.startswith("unverified:")
    assert (await _booking(db_session, booking_id)).status == BookingStatus.accepted
    [payment] = await _payments(db_session, booking_id)
    assert payment.status == PaymentStatus.pending
    assert await _memos(db_session, booking_id) == []

    # The genuine event with the same id is still processed.
    genuine = await _send(client, provider, body)
    assert genuine.json() == {"status": "processed", "processing_error": None}
    assert (await _booking(db_session, booking_id)).status == BookingStatus.confirmed


async def test_bad_signature_on_a_body_that_is_not_json_is_recorded(client, db_session) -> None:
    # Not JSON, not an object, and JSON that JSONB cannot hold (NaN, a NUL).
    bodies = [b"\xff not json", b"[1, 2]", b'{"amount": NaN}', b'{"id": "a\\u0000b"}']
    for body in bodies:
        response = await _send(client, None, body, signature="abc")
        assert response.status_code == 401

    rows = await _webhook_rows(db_session)
    assert len(rows) == len(bodies)
    for row in rows:
        assert row.signature_ok is False
        assert set(row.payload) == {"body_sha256"}


async def test_unknown_provider_is_404_and_records_nothing(client, db_session, provider) -> None:
    body = _event(provider, uuid.uuid4(), "fake-booking-x")

    response = await client.post(
        "/api/v1/webhooks/paystack",
        content=body,
        headers={"X-Vouch-Signature": provider.sign(body)},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert await _webhook_rows(db_session) == []


async def test_unsupported_event_type_is_200_with_processing_error(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)

    response = await _send(
        client, provider, _event(provider, booking_id, reference, type="charge.disputed")
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "processing_error": "unsupported_event"}
    [row] = await _webhook_rows(db_session)
    assert row.processing_error == "unsupported_event"
    assert row.processed_at == DEFAULT_NOW
    assert (await _booking(db_session, booking_id)).status == BookingStatus.accepted


async def test_malformed_signed_body_is_200_with_processing_error(
    client, db_session, provider
) -> None:
    bodies = [
        b"not json at all",
        json.dumps({"id": "evt_bad", "type": "payment_captured", "data": {}}).encode(),
    ]
    for body in bodies:
        response = await _send(client, provider, body)
        assert response.status_code == 200
        assert response.json()["processing_error"] == "malformed_event"

    rows = await _webhook_rows(db_session)
    assert [row.processing_error for row in rows] == ["malformed_event"] * 2
    assert rows[0].provider_event_id.startswith("sha256:")
    assert rows[0].payload == {"body_sha256": rows[0].provider_event_id.removeprefix("sha256:")}
    assert rows[1].provider_event_id == "evt_bad"


async def test_unknown_booking_or_payment_is_a_processing_error(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    other_id, other_reference = await _accepted(db_session, clock, notifier, provider)

    cases = [
        (_event(provider, uuid.uuid4(), reference), "booking_not_found"),
        (_event(provider, booking_id, "fake-booking-unknown"), "payment_not_found"),
        # Another booking's reference never pays this booking.
        (_event(provider, booking_id, other_reference), "payment_not_found"),
    ]
    for body, code in cases:
        response = await _send(client, provider, body)
        assert response.status_code == 200
        assert response.json()["processing_error"] == code

    for booking in (booking_id, other_id):
        assert (await _booking(db_session, booking)).status == BookingStatus.accepted
        assert await _memos(db_session, booking) == []


async def _odd_payment_booking(db_session) -> uuid.UUID:
    """An accepted booking whose pending payment (1000.00, ``fake-booking-odd``)
    disagrees with its price (D + R = 300000.00)."""
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session, deposit_amount=DEPOSIT)
    booking = await create_booking(
        db_session,
        asset=asset,
        renter=renter,
        status=BookingStatus.accepted,
        deposit_amount=DEPOSIT,
        price_snapshot=money_snapshot(deposit=str(DEPOSIT)),
    )
    payment = await create_payment(
        db_session,
        booking,
        type=PaymentType.booking,
        status=PaymentStatus.pending,
        amount=D("1000.00"),
    )
    payment.provider_reference = "fake-booking-odd"
    booking_id = booking.id
    await db_session.commit()
    return booking_id


async def test_capture_refused_by_the_booking_is_ledgered_and_refunded(
    client, db_session, provider
) -> None:
    """The payment row's amount matches, but the transition refuses it against the
    booking's price: the money still arrived, so it is held and returned."""
    booking_id = await _odd_payment_booking(db_session)

    response = await _send(
        client, provider, _event(provider, booking_id, "fake-booking-odd", amount=D("1000.00"))
    )

    assert response.status_code == 200
    assert response.json()["processing_error"] == "amount_mismatch"
    [row] = await _webhook_rows(db_session)
    assert row.processing_error == "amount_mismatch"
    assert row.processed_at is not None
    paid, refund = await _paid_and_refund(db_session, booking_id)
    assert paid.status == PaymentStatus.captured
    assert paid.amount == D("1000.00")
    assert (refund.status, refund.amount) == (PaymentStatus.pending, D("1000.00"))
    assert refund.metadata_["reason"] == "amount_mismatch"
    assert await _memos(db_session, booking_id) == ["unapplied_payment", "unapplied_payment_refund"]
    assert await _balances(db_session, booking_id) == ZEROS
    assert len(await _audits(db_session, paid.id, "payment.refunded_mismatch")) == 1
    assert (await _booking(db_session, booking_id)).status == BookingStatus.accepted
    # The refused transition wrote nothing of its own.
    assert await _audits(db_session, booking_id, "booking.deposit_paid") == []


async def test_domain_error_rolls_back_but_keeps_the_event(
    client, db_session, provider, monkeypatch
) -> None:
    """A handler that fails with a domain error after writing: its writes are
    rolled back with the savepoint, and the event row is kept with the code."""
    booking_id = await _odd_payment_booking(db_session)

    async def failing(ctx, event):
        payment = await webhooks._locked_payment(ctx, event.payment_reference)
        payment.status = PaymentStatus.captured
        await ctx.session.flush()
        raise InvalidTransition("accepted", "payment_captured")

    monkeypatch.setitem(webhooks._HANDLERS, "payment_captured", failing)

    response = await _send(
        client, provider, _event(provider, booking_id, "fake-booking-odd", amount=D("1000.00"))
    )

    assert response.json() == {"status": "processed", "processing_error": "invalid_transition"}
    [row] = await _webhook_rows(db_session)
    assert row.processing_error == "invalid_transition"
    [stored] = await _payments(db_session, booking_id)
    assert stored.status == PaymentStatus.pending
    assert await _memos(db_session, booking_id) == []


# ---------------------------------------------------------------------------
# Late payment (R23) and the wrong amount
# ---------------------------------------------------------------------------


async def _expire(session, booking_id, clock, notifier):
    clock.advance(timedelta(hours=25))
    await _move(session, booking_id, Actor.system(), BookingEvent.expire_deposit, clock, notifier)


async def _withdraw(session, booking_id, clock, notifier):
    booking = await _booking(session, booking_id)
    renter = Actor(user_id=booking.renter_id, role=ActorRole.renter)
    await _move(session, booking_id, renter, BookingEvent.cancel, clock, notifier)


async def _vendor_cancel(session, booking_id, clock, notifier):
    booking = await _booking(session, booking_id)
    vendor = Actor(user_id=booking.vendor_id, role=ActorRole.vendor)
    await _move(session, booking_id, vendor, BookingEvent.cancel, clock, notifier, reason="Broken")


@pytest.mark.parametrize(
    ("close", "status"),
    [
        (_expire, BookingStatus.expired),
        (_withdraw, BookingStatus.cancelled_by_renter),
        (_vendor_cancel, BookingStatus.cancelled_by_vendor),
    ],
    ids=["expired", "withdrawn", "vendor_cancelled"],
)
async def test_late_payment_is_recorded_and_refunded(
    client, db_session, clock, notifier, provider, close, status
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    await close(db_session, booking_id, clock, notifier)
    await db_session.commit()
    [closed] = await _payments(db_session, booking_id)
    assert closed.status == PaymentStatus.failed

    response = await _send(client, provider, _event(provider, booking_id, reference))

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "processing_error": None}
    assert (await _booking(db_session, booking_id)).status == status
    paid, refund = await _paid_and_refund(db_session, booking_id)
    assert paid.type == PaymentType.booking
    assert paid.status == PaymentStatus.captured
    assert paid.captured_at == clock.now()
    assert (refund.type, refund.status, refund.amount) == (
        PaymentType.refund,
        PaymentStatus.pending,
        TOTAL,
    )
    assert refund.provider == "fake"
    assert refund.metadata_ == {"reason": "late_payment", "refund_of": str(paid.id)}
    assert await _memos(db_session, booking_id) == ["payment_captured", "late_payment_refund"]
    assert await _balances(db_session, booking_id) == ZEROS
    ledger = await db_session.execute(
        select(LedgerEntry.memo, LedgerEntry.payment_id).where(LedgerEntry.booking_id == booking_id)
    )
    links = {memo: payment_id for memo, payment_id in ledger.all()}
    assert links == {"payment_captured": paid.id, "late_payment_refund": refund.id}
    [audit] = await _audits(db_session, paid.id, "payment.refunded_late")
    assert audit.actor_role == "system" and audit.actor_id is None
    assert audit.request_id == "req_webhook"
    assert audit.entity == "payment"
    assert audit.after["booking_id"] == str(booking_id)
    assert audit.after["booking_status"] == status.value
    assert audit.after["refund_payment"] == {"id": str(refund.id), "amount": str(TOTAL)}
    assert await _whole_ledger_balanced(db_session)


async def test_wrong_amount_is_ledgered_and_refunded(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    short = TOTAL - D("1000.00")

    response = await _send(client, provider, _event(provider, booking_id, reference, short))

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "processing_error": "amount_mismatch"}
    assert (await _booking(db_session, booking_id)).status == BookingStatus.accepted
    paid, refund = await _paid_and_refund(db_session, booking_id)
    assert paid.status == PaymentStatus.captured
    assert paid.amount == short  # what really arrived
    assert paid.metadata_["expected_amount"] == str(TOTAL)
    assert (refund.type, refund.status, refund.amount) == (
        PaymentType.refund,
        PaymentStatus.pending,
        short,
    )
    assert refund.metadata_ == {"reason": "amount_mismatch", "refund_of": str(paid.id)}
    assert await _memos(db_session, booking_id) == ["unapplied_payment", "unapplied_payment_refund"]
    assert await _balances(db_session, booking_id) == ZEROS
    [audit] = await _audits(db_session, paid.id, "payment.refunded_mismatch")
    assert audit.after["amount"] == str(short)
    assert audit.after["expected_amount"] == str(TOTAL)
    assert audit.request_id == "req_webhook"
    [row] = await _webhook_rows(db_session)
    assert row.processing_error == "amount_mismatch"

    # The account has been used: another payment into it is not applied but held
    # and returned, and the booking is left to expire at its deadline.
    again = await _send(
        client,
        provider,
        _event(provider, booking_id, reference, provider_event_id="evt_second"),
    )
    assert again.json()["processing_error"] == "payment_already_captured"
    assert (
        await _memos(db_session, booking_id)
        == ["unapplied_payment", "unapplied_payment_refund"] * 2
    )
    refunds = await _payments(db_session, booking_id, PaymentType.refund)
    assert sorted(r.amount for r in refunds) == sorted([short, TOTAL])
    assert await _balances(db_session, booking_id) == ZEROS
    await _expire(db_session, booking_id, clock, notifier)
    assert (await _booking(db_session, booking_id)).status == BookingStatus.expired
    assert await _balances(db_session, booking_id) == ZEROS


async def test_wrong_amount_on_a_closed_booking_is_refunded_as_a_mismatch(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    await _withdraw(db_session, booking_id, clock, notifier)
    await db_session.commit()

    response = await _send(client, provider, _event(provider, booking_id, reference, D("10.00")))

    assert response.json()["processing_error"] == "amount_mismatch"
    assert await _memos(db_session, booking_id) == ["unapplied_payment", "unapplied_payment_refund"]
    assert await _balances(db_session, booking_id) == ZEROS
    assert (await _booking(db_session, booking_id)).status == BookingStatus.cancelled_by_renter


async def test_second_payment_into_a_confirmed_booking_is_ledgered_and_refunded(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    assert (await _send(client, provider, _event(provider, booking_id, reference))).json()[
        "processing_error"
    ] is None
    [original] = await _payments(db_session, booking_id, PaymentType.booking)
    original_state = (original.status, original.amount, original.captured_at, original.metadata_)
    before = await _balances(db_session, booking_id)
    second = D("50000.00")

    response = await _send(
        client,
        provider,
        _event(provider, booking_id, reference, second, provider_event_id="evt_again"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "processed",
        "processing_error": "payment_already_captured",
    }
    assert (await _booking(db_session, booking_id)).status == BookingStatus.confirmed
    assert await _balances(db_session, booking_id) == before  # net 0 after the reversal
    assert await _memos(db_session, booking_id) == [
        "payment_captured",
        "unapplied_payment",
        "unapplied_payment_refund",
    ]
    [refund] = await _payments(db_session, booking_id, PaymentType.refund)
    assert (refund.status, refund.amount) == (PaymentStatus.pending, second)
    assert refund.metadata_["reason"] == "duplicate_payment"
    assert refund.metadata_["refund_of"] == str(original.id)
    assert refund.metadata_["provider_event_id"] == "evt_again"
    [same] = await _payments(db_session, booking_id, PaymentType.booking)
    assert (same.status, same.amount, same.captured_at, same.metadata_) == original_state
    [audit] = await _audits(db_session, original.id, "payment.refunded_duplicate")
    assert audit.after["amount"] == str(second)
    assert audit.after["refund_payment"] == {"id": str(refund.id), "amount": str(second)}
    assert audit.request_id == "req_webhook"
    assert await _whole_ledger_balanced(db_session)


async def test_non_ascii_signature_is_401_and_recorded(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id, reference = await _accepted(db_session, clock, notifier, provider)
    body = _event(provider, booking_id, reference)

    response = await client.post(
        URL,
        content=body,
        headers=[(b"content-type", b"application/json"), (b"x-vouch-signature", b"\xe9" * 64)],
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    [row] = await _webhook_rows(db_session)
    assert row.signature_ok is False
    assert (await _booking(db_session, booking_id)).status == BookingStatus.accepted


# ---------------------------------------------------------------------------
# payout_completed
# ---------------------------------------------------------------------------


async def _confirmed(client, session, clock, notifier, provider) -> uuid.UUID:
    booking_id, reference = await _accepted(session, clock, notifier, provider)
    response = await _send(client, provider, _event(provider, booking_id, reference))
    assert response.json()["status"] == "processed"
    return booking_id


def _payout(provider, booking_id, amount, reference="trf_1", **kw) -> bytes:
    return provider.make_event(
        "payout_completed",
        booking_id=booking_id,
        payment_reference=reference,
        amount=amount,
        **kw,
    )


async def test_payout_completed_posts_payout(client, db_session, clock, notifier, provider) -> None:
    booking_id = await _confirmed(client, db_session, clock, notifier, provider)
    payable = RENTAL + DELIVERY - COMMISSION

    response = await _send(
        client, provider, _payout(provider, booking_id, payable, recipient_code="RCP_vendor")
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "processing_error": None}
    [payout] = await _payments(db_session, booking_id, PaymentType.payout)
    assert payout.amount == payable
    assert payout.status == PaymentStatus.captured
    assert payout.payout_status == PayoutStatus.paid
    assert payout.paid_at == DEFAULT_NOW
    assert payout.payout_eligible_at == DEFAULT_NOW
    assert payout.vendor_recipient_code == "RCP_vendor"
    assert payout.platform_fee == COMMISSION
    assert payout.provider_reference == "trf_1"
    balances = await _balances(db_session, booking_id)
    assert balances[LedgerAccount.vendor_payable] == D("0.00")
    assert await _memos(db_session, booking_id) == ["payment_captured", "payout"]
    [audit] = await _audits(db_session, payout.id, "payout.paid")
    assert audit.after["amount"] == str(payable)
    assert await _whole_ledger_balanced(db_session)

    # A second payout for the same transfer is not posted again.
    again = await _send(
        client,
        provider,
        _payout(
            provider, booking_id, payable, recipient_code="RCP_vendor", provider_event_id="evt_2"
        ),
    )
    assert again.json()["processing_error"] == "payout_already_paid"
    assert await _memos(db_session, booking_id) == ["payment_captured", "payout"]


async def test_payout_above_vendor_payable_is_a_processing_error(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id = await _confirmed(client, db_session, clock, notifier, provider)
    payable = RENTAL + DELIVERY - COMMISSION

    response = await _send(
        client,
        provider,
        _payout(provider, booking_id, payable + D("0.01"), recipient_code="RCP_vendor"),
    )

    assert response.json() == {"status": "processed", "processing_error": "payout_exceeds_payable"}
    assert await _payments(db_session, booking_id, PaymentType.payout) == []
    assert await _memos(db_session, booking_id) == ["payment_captured"]


async def test_payout_marks_an_existing_payout_row_paid(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id = await _confirmed(client, db_session, clock, notifier, provider)
    eligible_at = DEFAULT_NOW - timedelta(hours=1)
    payout = Payment(
        booking_id=booking_id,
        type=PaymentType.payout,
        status=PaymentStatus.pending,
        amount=D("1000.00"),
        provider="fake",
        provider_reference="trf_existing",
        vendor_recipient_code="RCP_on_file",
        platform_fee=D("0.00"),
        payout_status=PayoutStatus.eligible,
        payout_eligible_at=eligible_at,
    )
    db_session.add(payout)
    await db_session.commit()

    wrong = await _send(
        client,
        provider,
        _payout(provider, booking_id, D("999.00"), "trf_existing", provider_event_id="evt_w"),
    )
    right = await _send(
        client, provider, _payout(provider, booking_id, D("1000.00"), "trf_existing")
    )

    assert wrong.json()["processing_error"] == "amount_mismatch"
    assert right.json() == {"status": "processed", "processing_error": None}
    [stored] = await _payments(db_session, booking_id, PaymentType.payout)
    assert stored.payout_status == PayoutStatus.paid
    assert stored.vendor_recipient_code == "RCP_on_file"  # the event named none
    assert stored.payout_eligible_at == eligible_at
    assert stored.paid_at == DEFAULT_NOW
    assert await _memos(db_session, booking_id) == ["payment_captured", "payout"]


async def test_payout_needs_a_recipient_and_its_own_reference(
    client, db_session, clock, notifier, provider
) -> None:
    booking_id = await _confirmed(client, db_session, clock, notifier, provider)

    missing = await _send(client, provider, _payout(provider, booking_id, D("100.00")))
    clash = await _send(
        client,
        provider,
        _payout(
            provider,
            booking_id,
            D("100.00"),
            reference=f"fake-booking-{booking_id.hex}",  # the renter's payment
            recipient_code="RCP_vendor",
        ),
    )

    assert missing.json()["processing_error"] == "recipient_missing"
    assert clash.json()["processing_error"] == "payout_not_found"
    assert await _payments(db_session, booking_id, PaymentType.payout) == []
    assert await _memos(db_session, booking_id) == ["payment_captured"]


async def test_webhook_body_over_the_cap_is_413(db_session, provider) -> None:
    settings = SETTINGS.model_copy(update={"max_request_bytes": 1024 * 1024})
    app = create_app(settings)
    app.state.payment_provider = provider
    body = b"{" + b" " * (1024 * 1024) + b"}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(URL, content=body, headers={"X-Vouch-Signature": "x"})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert await _webhook_rows(db_session) == []


# ---------------------------------------------------------------------------
# The race: the renter withdraws while the payment is in flight
# ---------------------------------------------------------------------------


async def test_renter_withdraws_while_payment_in_flight(session_factory) -> None:
    clock = FrozenClock(DEFAULT_NOW)
    notifier = RecordingNotifier()
    provider = FakePaymentProvider(SECRET)
    async with session_factory() as setup:
        booking_id, reference = await _accepted(setup, clock, notifier, provider)
        await setup.commit()
    body = _event(provider, booking_id, reference)

    async def deliver() -> str | None:
        async with session_factory() as session:
            result = await handle_webhook(
                session,
                provider,
                body,
                provider.sign(body),
                clock=clock,
                notifier=notifier,
                settings=get_settings(),
                request_id="req_race",
            )
            await session.commit()
            return result.processing_error

    async with session_factory() as renter_session:
        # The withdraw holds the booking row ...
        await _withdraw(renter_session, booking_id, clock, notifier)
        in_flight = asyncio.create_task(deliver())
        await asyncio.sleep(0.3)
        # ... so the payment waits for it rather than confirming a withdrawn booking.
        assert not in_flight.done()
        await renter_session.commit()
    assert await asyncio.wait_for(in_flight, timeout=10) is None

    async with session_factory() as check:
        booking = await check.get(Booking, booking_id)
        assert booking.status == BookingStatus.cancelled_by_renter
        assert await _memos(check, booking_id) == ["payment_captured", "late_payment_refund"]
        assert await _balances(check, booking_id) == ZEROS
        paid, refund = await _paid_and_refund(check, booking_id)
        assert paid.status == PaymentStatus.captured
        assert (refund.type, refund.amount) == (PaymentType.refund, TOTAL)
        assert len(await _audits(check, paid.id, "payment.refunded_late")) == 1


async def test_concurrent_duplicate_deliveries_post_once(session_factory) -> None:
    """Two copies of one event at the same time: the second waits on the unique
    index, then sees the first's row and answers duplicate."""
    clock = FrozenClock(DEFAULT_NOW)
    notifier = RecordingNotifier()
    provider = FakePaymentProvider(SECRET)
    async with session_factory() as setup:
        booking_id, reference = await _accepted(setup, clock, notifier, provider)
    body = _event(provider, booking_id, reference)

    async def deliver() -> str:
        async with session_factory() as session:
            result = await handle_webhook(
                session,
                provider,
                body,
                provider.sign(body),
                clock=clock,
                notifier=notifier,
                settings=get_settings(),
            )
            await session.commit()
            return result.status

    statuses = await asyncio.wait_for(asyncio.gather(deliver(), deliver()), timeout=20)

    assert sorted(statuses) == ["duplicate", "processed"]
    async with session_factory() as check:
        assert (await check.get(Booking, booking_id)).status == BookingStatus.confirmed
        assert await _memos(check, booking_id) == ["payment_captured"]
        assert len(await _webhook_rows(check)) == 1
        assert len(await _audits(check, booking_id, "booking.confirmed")) == 1
        assert await _payments(check, booking_id, PaymentType.refund) == []
