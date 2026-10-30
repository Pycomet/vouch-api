"""Live PostgreSQL tests for the rest of the booking state machine (Task 3.4).

Covers spec 3.1 rows ``confirmed + handover_completed / balance_captured``
(-> in_progress), ``in_progress + return_completed``, ``returned + claim_raised``,
``returned + complete``, ``disputed + resolve``, ``in_progress + flag_at_risk`` and
``in_progress + mark_lost``.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import AuditEvent, Booking, LedgerAccount, LedgerEntry
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection
from app.db.models.payment import PaymentStatus, PaymentType
from app.db.models.user import RenterRole
from app.schemas.errors import BookingGuardFailed, InvalidRequestError, InvalidTransition
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import account_balance, post_txn
from app.services.money import deposit_captured_postings
from app.services.notify import RecordingNotifier
from tests.postgres.factories import (
    DEFAULT_NOW,
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
ZERO = D("0.00")
RENTAL = D("150000.00")
DELIVERY = D("7500.00")
DEPOSIT = D("150000.00")
CONTRIBUTION = D("7500.00")
COMMISSION = D("22500.00")  # 15% of the rental (not of the delivery fee)
BALANCE = RENTAL + DELIVERY
START = date(2027, 1, 4)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def renter_actor(booking: Booking) -> Actor:
    return Actor(user_id=booking.renter_id, role=ActorRole.renter)


def vendor_actor(booking: Booking) -> Actor:
    return Actor(user_id=booking.vendor_id, role=ActorRole.vendor)


async def admin_actor(session: AsyncSession) -> Actor:
    admin = await create_user(session, role=RenterRole.admin)
    return Actor(user_id=admin.id, role=ActorRole.admin)


async def _transition(session, booking, actor, event, clock, notifier, payload=None) -> Booking:
    return await transition(
        session,
        booking.id,
        actor,
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
    )


async def _booking(
    session: AsyncSession, status: BookingStatus, *, starts_on: date = START, **kwargs
) -> Booking:
    """A booking in ``status`` with a full money snapshot (R, F, D, C, rate 0.15)."""
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=DEPOSIT)
    return await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        starts_on=starts_on,
        total_rental_fee=RENTAL,
        deposit_amount=DEPOSIT,
        price_snapshot=money_snapshot(
            rental=str(RENTAL),
            deposit=str(DEPOSIT),
            damage_contribution=str(CONTRIBUTION),
            delivery_fee=str(DELIVERY),
        ),
        fulfilment_choice="delivery",
        **kwargs,
    )


async def _with_deposit(session: AsyncSession, status: BookingStatus, **kwargs) -> Booking:
    """A booking in ``status`` whose deposit and damage contribution are on the ledger."""
    booking = await _booking(session, status, **kwargs)
    await post_txn(
        session,
        postings=deposit_captured_postings(DEPOSIT, CONTRIBUTION),
        booking_id=booking.id,
        memo="deposit_captured",
    )
    return booking


async def _balance_payload(session: AsyncSession, booking: Booking, amount=BALANCE) -> dict:
    payment = await create_payment(
        session, booking, type=PaymentType.rental_balance, amount=BALANCE
    )
    return {"amount": amount, "payment_id": payment.id}


async def _audits(session: AsyncSession, entity_id: uuid.UUID) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.entity_id == entity_id).order_by(AuditEvent.id)
    )
    return list(result.scalars())


async def _ledger(session: AsyncSession, booking_id: uuid.UUID) -> list[LedgerEntry]:
    result = await session.execute(
        select(LedgerEntry).where(LedgerEntry.booking_id == booking_id).order_by(LedgerEntry.id)
    )
    return list(result.scalars())


async def _balance(session, account: LedgerAccount, booking_id: uuid.UUID) -> Decimal:
    return await account_balance(session, account, booking_id=booking_id)


async def _counts(session: AsyncSession) -> tuple[int, int]:
    ledger = await session.scalar(select(func.count()).select_from(LedgerEntry))
    audits = await session.scalar(select(func.count()).select_from(AuditEvent))
    return ledger, audits


# ---------------------------------------------------------------------------
# confirmed + handover_completed / balance_captured -> in_progress
# ---------------------------------------------------------------------------


async def test_handover_then_balance_moves_to_in_progress(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)

    signed = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.handover_completed, clock, notifier
    )

    assert signed.status == BookingStatus.confirmed
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.handover_signed"
    assert (audit.before["status"], audit.after["status"]) == ("confirmed", "confirmed")
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == ZERO

    payload = await _balance_payload(db_session, booking)
    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.balance_captured, clock, notifier, payload
    )

    assert result.status == BookingStatus.in_progress
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.in_progress"
    assert (audit.before["status"], audit.after["status"]) == ("confirmed", "in_progress")
    assert audit.after["amount"] == str(BALANCE)
    assert audit.after["payment_id"] == str(payload["payment_id"])
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == (
        BALANCE - COMMISSION
    )
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION
    balance_rows = [
        row for row in await _ledger(db_session, booking.id) if row.memo != "deposit_captured"
    ]
    assert len(balance_rows) == 3
    assert {row.payment_id for row in balance_rows} == {payload["payment_id"]}
    assert len({row.txn_group for row in balance_rows}) == 1
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}
    assert set(notifier.templates()) == {"booking.in_progress"}


async def test_balance_then_handover_moves_to_in_progress(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.confirmed)
    payload = await _balance_payload(db_session, booking)

    captured = await _transition(
        db_session, booking, Actor.system(), BookingEvent.balance_captured, clock, notifier, payload
    )

    assert captured.status == BookingStatus.confirmed
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.balance_captured"
    assert audit.after["status"] == "confirmed"
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION

    await create_handover(db_session, booking)
    result = await _transition(
        db_session, booking, vendor_actor(booking), BookingEvent.handover_completed, clock, notifier
    )

    assert result.status == BookingStatus.in_progress
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.in_progress"
    assert audit.actor_role == "vendor"
    # The balance is posted once, by balance_captured.
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION


async def test_system_may_record_handover(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.handover_completed, clock, notifier
    )

    assert result.status == BookingStatus.confirmed


@pytest.mark.parametrize("record", ["none", "draft", "return_only"])
async def test_handover_without_completed_pickup_is_refused(
    db_session, clock, notifier, record
) -> None:
    booking = await _booking(db_session, BookingStatus.confirmed)
    if record == "draft":
        await create_handover(db_session, booking, completed=False)
    elif record == "return_only":
        await create_handover(db_session, booking, direction=HandoverDirection.return_)
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            renter_actor(booking),
            BookingEvent.handover_completed,
            clock,
            notifier,
        )

    assert caught.value.code == "handover_incomplete"
    assert caught.value.status_code == 409
    assert await _counts(db_session) == counts


async def test_second_handover_signal_is_invalid(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.handover_completed, clock, notifier
    )

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            vendor_actor(booking),
            BookingEvent.handover_completed,
            clock,
            notifier,
        )


async def test_stranger_cannot_record_handover(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    stranger = Actor(user_id=uuid.uuid4(), role=ActorRole.renter)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, stranger, BookingEvent.handover_completed, clock, notifier
        )


@pytest.mark.parametrize("amount", [RENTAL, BALANCE + D("0.01"), None, "abc", 157500.0])
async def test_balance_amount_mismatch(db_session, clock, notifier, amount) -> None:
    booking = await _with_deposit(db_session, BookingStatus.confirmed)
    payload = await _balance_payload(db_session, booking, amount=amount)
    if amount is None:
        del payload["amount"]
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            Actor.system(),
            BookingEvent.balance_captured,
            clock,
            notifier,
            payload,
        )

    assert caught.value.code == "amount_mismatch"
    assert await _counts(db_session) == counts


async def test_balance_needs_this_bookings_captured_balance_payment(
    db_session, clock, notifier
) -> None:
    booking = await _with_deposit(db_session, BookingStatus.confirmed)
    other = await _booking(db_session, BookingStatus.confirmed, starts_on=date(2027, 3, 1))
    other_payment = await create_payment(
        db_session, other, type=PaymentType.rental_balance, amount=BALANCE
    )
    deposit = await create_payment(db_session, booking, amount=DEPOSIT)
    pending = await create_payment(
        db_session,
        booking,
        type=PaymentType.rental_balance,
        amount=BALANCE,
        status=PaymentStatus.pending,
    )
    counts = await _counts(db_session)

    for payment_id in (other_payment.id, deposit.id, pending.id, uuid.uuid4(), "nope", None):
        with pytest.raises(InvalidRequestError):
            await _transition(
                db_session,
                booking,
                Actor.system(),
                BookingEvent.balance_captured,
                clock,
                notifier,
                {"amount": BALANCE, "payment_id": payment_id},
            )
    assert await _counts(db_session) == counts


async def test_second_balance_capture_is_invalid(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.confirmed)
    payload = await _balance_payload(db_session, booking)
    await _transition(
        db_session, booking, Actor.system(), BookingEvent.balance_captured, clock, notifier, payload
    )
    counts = await _counts(db_session)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            Actor.system(),
            BookingEvent.balance_captured,
            clock,
            notifier,
            await _balance_payload(db_session, booking),
        )
    assert (await _counts(db_session))[0] == counts[0]
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION


async def test_commission_is_frozen_in_the_snapshot(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.confirmed)
    booking.price_snapshot = {**booking.price_snapshot, "commission_rate": "0.125"}
    await db_session.flush()
    payload = await _balance_payload(db_session, booking)

    await _transition(
        db_session, booking, Actor.system(), BookingEvent.balance_captured, clock, notifier, payload
    )

    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == D("18750.00")


# ---------------------------------------------------------------------------
# in_progress + return_completed -> returned
# ---------------------------------------------------------------------------


async def test_return_sets_claim_window(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)
    await create_handover(db_session, booking)
    await create_handover(db_session, booking, direction=HandoverDirection.return_)

    result = await _transition(
        db_session, booking, vendor_actor(booking), BookingEvent.return_completed, clock, notifier
    )

    assert result.status == BookingStatus.returned
    assert result.claim_window_ends_at == DEFAULT_NOW + timedelta(hours=24)
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.returned"
    assert audit.after["claim_window_ends_at"] == (DEFAULT_NOW + timedelta(hours=24)).isoformat()


@pytest.mark.parametrize("record", ["none", "draft", "pickup_only"])
async def test_return_without_completed_return_record_is_refused(
    db_session, clock, notifier, record
) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)
    if record == "draft":
        await create_handover(
            db_session, booking, direction=HandoverDirection.return_, completed=False
        )
    elif record == "pickup_only":
        await create_handover(db_session, booking)
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.return_completed, clock, notifier
        )

    assert caught.value.code == "handover_incomplete"
    assert await _counts(db_session) == counts
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.in_progress
