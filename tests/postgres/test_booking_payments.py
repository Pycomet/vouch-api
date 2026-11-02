"""Live PostgreSQL tests for the payment rows (R21 and R22, Task 5.3).

* R21: accepting a booking opens the renter's virtual account through the
  :class:`PaymentProvider` and records a pending ``booking`` payment for
  D + C + R + F. The account is shown to the renter only, while ``accepted``.
  A provider failure, or no provider at all, fails the accept with nothing written.
* R22: every refund (paid renter or vendor cancel, vendor no-show) records a
  pending ``refund`` payment for the refunded amount, with the same provider.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier, get_payment_provider
from app.core.clock import FrozenClock, start_of_business_day
from app.core.config import get_settings
from app.db.models import AuditEvent, Booking, LedgerEntry, Payment
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentStatus, PaymentType
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.providers.payment import FakePaymentProvider
from app.schemas.errors import (
    BookingGuardFailed,
    PaymentAccountFailedError,
    PaymentUnavailableError,
)
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.notify import RecordingNotifier
from app.services.payments import MissingBookingPaymentError
from tests.helpers.auth import bearer
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
DELIVERY = D("5000.00")  # F > 0, so the account is for D + C + R + F
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL + DELIVERY
FEE = D("15000.00")  # 10% of the rental
SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
API = "/api/v1/bookings"
SECRET = "webhook-secret-for-tests"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider(SECRET)


def _vendor(booking: Booking) -> Actor:
    return Actor(user_id=booking.vendor_id, role=ActorRole.vendor)


def _renter(booking: Booking) -> Actor:
    return Actor(user_id=booking.renter_id, role=ActorRole.renter)


async def _transition(session, booking, actor, event, clock, notifier, provider=None, **payload):
    return await transition(
        session,
        booking.id,
        actor,
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
        payment_provider=provider,
        request_id="req_payments",
    )


async def _booking(session: AsyncSession, status: BookingStatus, **kwargs) -> Booking:
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=DEPOSIT)
    return await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        deposit_amount=DEPOSIT,
        total_rental_fee=RENTAL,
        price_snapshot=money_snapshot(
            deposit=str(DEPOSIT),
            damage_contribution=str(CONTRIBUTION),
            rental=str(RENTAL),
            delivery_fee=str(DELIVERY),
        ),
        **kwargs,
    )


async def _payments(session: AsyncSession, booking_id: uuid.UUID) -> list[Payment]:
    rows = await session.scalars(
        select(Payment)
        .where(Payment.booking_id == booking_id)
        .order_by(Payment.created_at, Payment.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _actions(session: AsyncSession, booking_id: uuid.UUID) -> list[str]:
    rows = await session.scalars(
        select(AuditEvent.action).where(AuditEvent.entity_id == booking_id).order_by(AuditEvent.id)
    )
    return list(rows)


# ---------------------------------------------------------------------------
# R21: the virtual account on accept
# ---------------------------------------------------------------------------


async def test_accept_opens_the_virtual_account_and_records_a_pending_payment(
    db_session, clock, notifier, provider
) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    result = await _transition(
        db_session, booking, _vendor(booking), BookingEvent.accept, clock, notifier, provider
    )

    assert result.status == BookingStatus.accepted
    assert provider.accounts == [(booking.id, TOTAL, "booking")]
    [payment] = await _payments(db_session, booking.id)
    assert payment.type == PaymentType.booking
    assert payment.status == PaymentStatus.pending
    assert payment.amount == TOTAL
    assert payment.currency == "NGN"
    assert payment.provider == "fake"
    assert payment.provider_reference == f"fake-booking-{booking.id.hex}"
    assert payment.provider_virtual_account == f"{booking.id.int % 10**10:010d}"
    assert payment.metadata_ == {"bank": FakePaymentProvider.bank}
    assert payment.captured_at is None
    # The deposit lifecycle columns stay unused (R22): the ledger tracks the hold.
    assert payment.deposit_status is None and payment.deposit_held_at is None

    audit = (
        await db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_id == booking.id)
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
    ).one()
    assert audit.action == "booking.accepted"
    assert audit.request_id == "req_payments"
    assert audit.after["payment"] == {
        "id": str(payment.id),
        "amount": "312500.00",
        "provider": "fake",
        "reference": payment.provider_reference,
    }
    [notice] = notifier.calls
    assert notice.user_id == booking.renter_id
    assert notice.template == "booking.accepted"
    assert notice.data["payment_instructions"] == {
        "bank": FakePaymentProvider.bank,
        "account_number": payment.provider_virtual_account,
        "reference": payment.provider_reference,
        "amount": "312500.00",
        "due_by": result.deposit_due_at.isoformat(),
    }


async def test_accept_fails_with_no_change_when_the_provider_raises(
    db_session, clock, notifier
) -> None:
    booking = await _booking(db_session, BookingStatus.requested)
    failing = FakePaymentProvider(SECRET, fail_create=True)

    with pytest.raises(PaymentAccountFailedError) as raised:
        await _transition(
            db_session, booking, _vendor(booking), BookingEvent.accept, clock, notifier, failing
        )

    assert raised.value.status_code == 502
    assert raised.value.code == "payment_provider_error"
    reloaded = await db_session.get(Booking, booking.id, populate_existing=True)
    assert reloaded.status == BookingStatus.requested
    assert reloaded.deposit_due_at is None
    assert await _payments(db_session, booking.id) == []
    assert await _actions(db_session, booking.id) == []
    assert notifier.calls == []


async def test_accept_without_a_provider_is_refused_with_no_change(
    db_session, clock, notifier
) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    with pytest.raises(PaymentUnavailableError) as raised:
        await _transition(
            db_session, booking, _vendor(booking), BookingEvent.accept, clock, notifier
        )

    assert raised.value.status_code == 503
    reloaded = await db_session.get(Booking, booking.id, populate_existing=True)
    assert reloaded.status == BookingStatus.requested
    assert await _payments(db_session, booking.id) == []
    assert notifier.calls == []


async def test_a_failed_accept_guard_never_calls_the_provider(
    db_session, clock, notifier, provider
) -> None:
    booking = await _booking(db_session, BookingStatus.requested)
    clock.advance(booking.hold_expires_at - clock.now())  # exactly at hold expiry

    with pytest.raises(BookingGuardFailed):
        await _transition(
            db_session, booking, _vendor(booking), BookingEvent.accept, clock, notifier, provider
        )

    assert provider.accounts == []
    assert await _payments(db_session, booking.id) == []


async def test_other_events_ignore_the_provider(db_session, clock, notifier, provider) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    await _transition(
        db_session, booking, _vendor(booking), BookingEvent.decline, clock, notifier, provider
    )

    assert provider.accounts == []
    assert await _payments(db_session, booking.id) == []


# ---------------------------------------------------------------------------
# R22: refund rows
# ---------------------------------------------------------------------------


async def _confirmed(session, clock, notifier, provider: str = "fake") -> Booking:
    booking = await _booking(session, BookingStatus.accepted)
    payment = await create_payment(
        session, booking, type=PaymentType.booking, amount=TOTAL, provider=provider
    )
    await _transition(
        session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        amount=TOTAL,
        payment_id=payment.id,
    )
    return booking


async def _refund(session: AsyncSession, booking_id: uuid.UUID) -> Payment:
    [refund] = [p for p in await _payments(session, booking_id) if p.type == PaymentType.refund]
    return refund


async def _booking_payment(session: AsyncSession, booking_id: uuid.UUID) -> Payment:
    [paid] = [p for p in await _payments(session, booking_id) if p.type == PaymentType.booking]
    return paid


async def test_renter_cancel_records_a_pending_refund_less_the_fee(
    db_session, clock, notifier
) -> None:
    booking = await _confirmed(db_session, clock, notifier, provider="other-provider")

    await _transition(db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier)

    refund = await _refund(db_session, booking.id)
    paid = await _booking_payment(db_session, booking.id)
    assert refund.status == PaymentStatus.pending
    assert refund.amount == TOTAL - FEE
    assert refund.provider == "other-provider"  # the provider that took the money
    assert refund.currency == "NGN"
    assert refund.provider_reference is None  # not yet sent to the provider
    assert refund.metadata_ == {"reason": "cancelled_by_renter", "refund_of": str(paid.id)}
    assert refund.deposit_status is None and refund.payout_status is None
    assert paid.status == PaymentStatus.captured  # the original row is left as it was
    audit = (
        await db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "booking.cancelled_by_renter")
            .where(AuditEvent.entity_id == booking.id)
        )
    ).one()
    assert audit.after["refund_payment"] == {"id": str(refund.id), "amount": "297500.00"}


async def test_refund_ledger_groups_point_at_the_refund_row(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)

    await _transition(db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier)

    refund = await _refund(db_session, booking.id)
    rows = await db_session.execute(
        select(LedgerEntry.memo, LedgerEntry.payment_id)
        .where(LedgerEntry.booking_id == booking.id)
        .distinct()
    )
    by_memo = dict(rows.all())
    for memo in ("cancel_deposit_refund", "cancel_contribution_refund", "cancel_rental_refund"):
        assert by_memo[memo] == refund.id
    assert by_memo["cancellation_fee"] is None


async def test_vendor_cancel_records_a_full_refund(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)

    await _transition(
        db_session,
        booking,
        _vendor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        reason="Lens damaged",
    )

    refund = await _refund(db_session, booking.id)
    assert refund.amount == TOTAL
    assert refund.provider == "fake"
    assert refund.metadata_["reason"] == "cancelled_by_vendor"


async def test_no_show_records_a_full_refund(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)
    opens = start_of_business_day(booking.starts_on, get_settings().business_zone)
    clock.advance(opens - clock.now())

    await _transition(
        db_session, booking, _renter(booking), BookingEvent.report_no_show, clock, notifier
    )

    refund = await _refund(db_session, booking.id)
    assert refund.amount == TOTAL
    assert refund.metadata_["reason"] == "vendor_no_show"


async def test_an_unpaid_cancel_records_no_refund(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)

    await _transition(db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier)

    assert await _payments(db_session, booking.id) == []


async def test_a_refused_cancel_records_no_refund(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)

    with pytest.raises(BookingGuardFailed):  # a vendor must give a reason
        await _transition(
            db_session, booking, _vendor(booking), BookingEvent.cancel, clock, notifier
        )

    assert [p.type for p in await _payments(db_session, booking.id)] == [PaymentType.booking]


async def test_a_paid_cancel_without_the_captured_payment_is_refused(
    db_session, clock, notifier
) -> None:
    """A confirmed booking always has its captured payment; if not, refuse loudly."""
    booking = await _booking(db_session, BookingStatus.confirmed)
    ledger_before = await db_session.scalar(select(func.count()).select_from(LedgerEntry))

    with pytest.raises(MissingBookingPaymentError):
        await _transition(
            db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier
        )

    assert await db_session.scalar(select(func.count()).select_from(LedgerEntry)) == ledger_before


# ---------------------------------------------------------------------------
# The API: provider dependency and payment instructions
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_and_client(
    db_session: AsyncSession, clock: FrozenClock, notifier: RecordingNotifier, provider
) -> AsyncIterator[tuple]:
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
    app.dependency_overrides[get_payment_provider] = lambda: provider
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield app, http


@pytest.fixture
async def client(app_and_client) -> AsyncClient:
    return app_and_client[1]


def _auth(user_id: uuid.UUID, role: ActorRole) -> dict[str, str]:
    return bearer(user_id, role, SETTINGS)


async def _requested(session: AsyncSession) -> tuple[uuid.UUID, dict, dict]:
    booking = await _booking(session, BookingStatus.requested)
    return (
        booking.id,
        _auth(booking.renter_id, ActorRole.renter),
        _auth(booking.vendor_id, ActorRole.vendor),
    )


async def test_api_accept_shows_payment_instructions_to_the_renter_only(
    client, db_session, provider
) -> None:
    booking_id, renter, vendor = await _requested(db_session)
    admin_user = await create_user(db_session, role=RenterRole.admin)
    admin = _auth(admin_user.id, ActorRole.admin)

    accepted = await client.post(f"{API}/{booking_id}/accept", headers=vendor)

    assert accepted.status_code == 200
    assert accepted.json()["payment_instructions"] is None  # the vendor's own view
    body = (await client.get(f"{API}/{booking_id}", headers=renter)).json()
    assert body["payment_instructions"] == {
        "bank": FakePaymentProvider.bank,
        "account_number": f"{booking_id.int % 10**10:010d}",
        "reference": f"fake-booking-{booking_id.hex}",
        "amount": "312500.00",
        "due_by": body["deposit_due_at"],
    }
    assert body["payment_instructions"]["amount"] == body["money_line"]["next"]["amount_due"]
    vendor_view = (await client.get(f"{API}/{booking_id}", headers=vendor)).json()
    admin_view = (await client.get(f"{API}/{booking_id}", headers=admin)).json()
    assert vendor_view["payment_instructions"] is None
    assert admin_view["payment_instructions"] is None
    assert provider.accounts == [(booking_id, TOTAL, "booking")]


async def test_api_instructions_disappear_once_paid(client, db_session, clock, notifier) -> None:
    booking_id, renter, vendor = await _requested(db_session)
    await client.post(f"{API}/{booking_id}/accept", headers=vendor)
    payment = await _booking_payment(db_session, booking_id)
    payment.status = PaymentStatus.captured
    payment.captured_at = clock.now()
    booking = await db_session.get(Booking, booking_id)
    await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        amount=TOTAL,
        payment_id=payment.id,
    )

    body = (await client.get(f"{API}/{booking_id}", headers=renter)).json()

    assert body["status"] == "confirmed"
    assert body["payment_instructions"] is None


async def test_api_accepted_booking_without_an_account_has_no_instructions(
    client, db_session
) -> None:
    """An accepted booking from before R21 (no pending payment) shows none."""
    booking = await _booking(db_session, BookingStatus.accepted)

    body = (
        await client.get(f"{API}/{booking.id}", headers=_auth(booking.renter_id, ActorRole.renter))
    ).json()

    assert body["status"] == "accepted"
    assert body["payment_instructions"] is None


async def test_api_requested_booking_has_no_instructions(client, db_session) -> None:
    booking_id, renter, _ = await _requested(db_session)

    body = (await client.get(f"{API}/{booking_id}", headers=renter)).json()

    assert body["payment_instructions"] is None


async def test_api_accept_is_502_with_no_change_when_the_provider_fails(
    app_and_client, db_session
) -> None:
    app, client = app_and_client
    app.dependency_overrides[get_payment_provider] = lambda: FakePaymentProvider(
        SECRET, fail_create=True
    )
    booking_id, renter, vendor = await _requested(db_session)

    response = await client.post(f"{API}/{booking_id}/accept", headers=vendor)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "payment_provider_error"
    body = (await client.get(f"{API}/{booking_id}", headers=renter)).json()
    assert body["status"] == "requested"
    assert await _payments(db_session, booking_id) == []
    assert await _actions(db_session, booking_id) == []


async def test_api_accept_is_503_without_a_payment_provider(app_and_client, db_session) -> None:
    app, client = app_and_client
    del app.dependency_overrides[get_payment_provider]
    app.state.payment_provider = None
    booking_id, renter, vendor = await _requested(db_session)

    response = await client.post(f"{API}/{booking_id}/accept", headers=vendor)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_unavailable"
    body = (await client.get(f"{API}/{booking_id}", headers=renter)).json()
    assert body["status"] == "requested"
    assert await _payments(db_session, booking_id) == []


async def test_api_accept_visibility_is_checked_before_the_provider(
    app_and_client, db_session
) -> None:
    """A stranger still gets 404 (R19) even when payments are unavailable."""
    app, client = app_and_client
    del app.dependency_overrides[get_payment_provider]
    app.state.payment_provider = None
    booking_id, _, _ = await _requested(db_session)
    stranger = await create_user(db_session, role=RenterRole.vendor)

    response = await client.post(
        f"{API}/{booking_id}/accept", headers=_auth(stranger.id, ActorRole.vendor)
    )

    assert response.status_code == 404


async def test_api_accept_uses_the_apps_own_provider(app_and_client, db_session) -> None:
    app, client = app_and_client
    del app.dependency_overrides[get_payment_provider]
    booking_id, _, vendor = await _requested(db_session)

    response = await client.post(f"{API}/{booking_id}/accept", headers=vendor)

    assert response.status_code == 200
    assert app.state.payment_provider.accounts == [(booking_id, TOTAL, "booking")]
