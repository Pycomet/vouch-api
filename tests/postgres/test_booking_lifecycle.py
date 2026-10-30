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

from app.core.clock import FrozenClock, start_of_business_day
from app.core.config import get_settings
from app.db.models import Asset, AuditEvent, Booking, LedgerAccount, LedgerEntry, User
from app.db.models.asset import ListingStatus
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


# ---------------------------------------------------------------------------
# returned + claim_raised -> disputed
# ---------------------------------------------------------------------------

CLAIM = {
    "amount": "40000.00",
    "description": "Cracked LCD screen",
    "evidence": ["https://evidence.example/crack.jpg"],
}


async def test_claim_within_window_disputes(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - DEFAULT_NOW - timedelta(seconds=1))

    result = await _transition(
        db_session,
        booking,
        vendor_actor(booking),
        BookingEvent.claim_raised,
        clock,
        notifier,
        CLAIM,
    )

    assert result.status == BookingStatus.disputed
    assert result.claim_amount == D("40000.00")
    assert result.claim_description == "Cracked LCD screen"
    assert result.claim_evidence == CLAIM["evidence"]
    assert result.claimed_at == clock.now()
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.disputed"
    assert audit.actor_role == "vendor"
    assert audit.after["claim_amount"] == "40000.00"
    # The deposit stays held: nothing moves on the ledger while disputed.
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == DEPOSIT
    ops = [call for call in notifier.calls if call.user_id is None]
    assert [call.template for call in ops] == ["ops.claim_raised"]
    assert ops[0].data["claim_amount"] == "40000.00"


async def test_claim_after_window_409(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - DEFAULT_NOW)  # exactly at the end
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            vendor_actor(booking),
            BookingEvent.claim_raised,
            clock,
            notifier,
            CLAIM,
        )

    assert caught.value.code == "claim_window_closed"
    assert caught.value.status_code == 409
    assert await _counts(db_session) == counts
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.returned
    assert booking.claimed_at is None


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "0"},
        {"amount": "-5.00"},
        {"amount": 100.5},
        {"amount": None},
        {"description": "   "},
        {"description": None},
        {"evidence": []},
        {"evidence": "https://evidence.example/a.jpg"},
        {"evidence": None},
    ],
)
async def test_claim_payload_is_validated(db_session, clock, notifier, change) -> None:
    booking = await _booking(db_session, BookingStatus.returned)
    payload = {**CLAIM, **change}
    payload = {key: value for key, value in payload.items() if value is not None}
    counts = await _counts(db_session)

    with pytest.raises(InvalidRequestError):
        await _transition(
            db_session,
            booking,
            vendor_actor(booking),
            BookingEvent.claim_raised,
            clock,
            notifier,
            payload,
        )
    assert await _counts(db_session) == counts


async def test_renter_cannot_raise_claim(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.returned)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            renter_actor(booking),
            BookingEvent.claim_raised,
            clock,
            notifier,
            CLAIM,
        )


# ---------------------------------------------------------------------------
# returned + complete (timer) -> completed
# ---------------------------------------------------------------------------


async def test_complete_after_window_releases_deposit(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - DEFAULT_NOW + timedelta(seconds=1))

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier
    )

    assert result.status == BookingStatus.completed
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == ZERO
    audits = await _audits(db_session, booking.id)
    assert [a.action for a in audits[-2:]] == ["deposit.released", "booking.completed"]
    assert audits[-2].after["deposit"] == str(DEPOSIT)
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}
    assert set(notifier.templates()) == {"booking.completed"}


async def test_complete_at_window_end_is_invalid(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - DEFAULT_NOW)
    counts = await _counts(db_session)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier
        )
    assert await _counts(db_session) == counts


async def test_complete_with_claim_is_invalid(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.returned, claimed=True)
    clock.advance(timedelta(days=3))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier
        )


async def test_vendor_cannot_complete(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.returned)
    clock.advance(timedelta(days=3))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, vendor_actor(booking), BookingEvent.complete, clock, notifier
        )


# ---------------------------------------------------------------------------
# disputed + resolve (admin) -> completed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "amount", "action", "vendor_gets", "cash_out"),
    [
        ("release", None, "deposit.released", ZERO, DEPOSIT),
        ("deduct", "40000.00", "deposit.deducted", D("40000.00"), D("110000.00")),
        ("forfeit", None, "deposit.forfeited", DEPOSIT, ZERO),
    ],
)
async def test_resolve_decisions(
    db_session, clock, notifier, decision, amount, action, vendor_gets, cash_out
) -> None:
    booking = await _with_deposit(db_session, BookingStatus.disputed, claimed=True)
    admin = await admin_actor(db_session)
    cash_before = await _balance(db_session, LedgerAccount.platform_cash, booking.id)
    payload = {"decision": decision, "reason": "  Photos reviewed  "}
    if amount is not None:
        payload["amount"] = amount

    result = await _transition(
        db_session, booking, admin, BookingEvent.resolve, clock, notifier, payload
    )

    assert result.status == BookingStatus.completed
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == ZERO
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == vendor_gets
    cash_after = await _balance(db_session, LedgerAccount.platform_cash, booking.id)
    assert cash_after - cash_before == cash_out
    audits = await _audits(db_session, booking.id)
    assert [a.action for a in audits[-2:]] == [action, "booking.completed"]
    final = audits[-1]
    assert final.actor_role == "admin"
    assert final.reason == "Photos reviewed"
    assert final.after["decision"] == decision
    assert final.before["status"] == "disputed"
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}


async def test_resolve_deduct_capped_at_deposit(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.disputed, claimed=True)
    admin = await admin_actor(db_session)

    await _transition(
        db_session,
        booking,
        admin,
        BookingEvent.resolve,
        clock,
        notifier,
        {"decision": "deduct", "amount": "900000.00", "reason": "Total loss of the body"},
    )

    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == DEPOSIT
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == ZERO
    final = (await _audits(db_session, booking.id))[-1]
    assert final.after["amount"] == str(DEPOSIT)
    assert final.after["requested_amount"] == "900000.00"


@pytest.mark.parametrize("reason", [None, "", "   ", 5])
async def test_resolve_requires_reason(db_session, clock, notifier, reason) -> None:
    booking = await _with_deposit(db_session, BookingStatus.disputed, claimed=True)
    admin = await admin_actor(db_session)
    payload = {"decision": "release"}
    if reason is not None:
        payload["reason"] = reason
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, admin, BookingEvent.resolve, clock, notifier, payload
        )

    assert caught.value.code == "reason_required"
    assert await _counts(db_session) == counts
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.disputed


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "split"},
        {},
        {"decision": "deduct"},
        {"decision": "deduct", "amount": "0"},
        {"decision": "deduct", "amount": 10.0},
        {"decision": "deduct", "amount": "abc"},
    ],
)
async def test_resolve_payload_is_validated(db_session, clock, notifier, payload) -> None:
    booking = await _with_deposit(db_session, BookingStatus.disputed, claimed=True)
    admin = await admin_actor(db_session)
    counts = await _counts(db_session)

    with pytest.raises(InvalidRequestError):
        await _transition(
            db_session,
            booking,
            admin,
            BookingEvent.resolve,
            clock,
            notifier,
            {**payload, "reason": "Reviewed"},
        )
    assert await _counts(db_session) == counts


@pytest.mark.parametrize("role", ["renter", "vendor", "system"])
async def test_only_admin_resolves(db_session, clock, notifier, role) -> None:
    booking = await _with_deposit(db_session, BookingStatus.disputed, claimed=True)
    actor = {
        "renter": renter_actor(booking),
        "vendor": vendor_actor(booking),
        "system": Actor.system(),
    }[role]

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            actor,
            BookingEvent.resolve,
            clock,
            notifier,
            {"decision": "release", "reason": "x"},
        )


# ---------------------------------------------------------------------------
# in_progress + flag_at_risk (timer): a flag, not a state
# ---------------------------------------------------------------------------


async def test_flag_at_risk_freezes_renter(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.in_progress)

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.flag_at_risk, clock, notifier
    )

    assert result.status == BookingStatus.in_progress
    assert result.at_risk_at == DEFAULT_NOW
    renter = await db_session.get(User, booking.renter_id)
    await db_session.refresh(renter)
    assert renter.restricted_at == DEFAULT_NOW
    assert renter.restricted_reason == "at_risk"
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.at_risk"
    assert (audit.before["status"], audit.after["status"]) == ("in_progress", "in_progress")
    assert audit.after["at_risk_at"] == DEFAULT_NOW.isoformat()
    user_audit = (await _audits(db_session, booking.renter_id))[-1]
    assert user_audit.action == "user.restricted"
    assert user_audit.after["restricted_reason"] == "at_risk"
    assert notifier.templates() == ["ops.at_risk"]
    assert notifier.calls[0].user_id is None
    assert len(await _ledger(db_session, booking.id)) == 3  # only the deposit group


async def test_second_at_risk_flag_is_invalid(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)
    await _transition(
        db_session, booking, Actor.system(), BookingEvent.flag_at_risk, clock, notifier
    )
    counts = await _counts(db_session)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.flag_at_risk, clock, notifier
        )
    assert await _counts(db_session) == counts


async def test_at_risk_keeps_an_existing_restriction(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)
    renter = await db_session.get(User, booking.renter_id)
    earlier = DEFAULT_NOW - timedelta(days=30)
    renter.restricted_at = earlier
    renter.restricted_reason = "lost_item"
    await db_session.flush()

    await _transition(
        db_session, booking, Actor.system(), BookingEvent.flag_at_risk, clock, notifier
    )

    await db_session.refresh(renter)
    assert (renter.restricted_at, renter.restricted_reason) == (earlier, "lost_item")
    assert [a.action for a in await _audits(db_session, booking.renter_id)] == []


async def test_vendor_cannot_flag_at_risk(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, vendor_actor(booking), BookingEvent.flag_at_risk, clock, notifier
        )


# ---------------------------------------------------------------------------
# in_progress + mark_lost (admin) -> lost
# ---------------------------------------------------------------------------


def _lost_from(booking: Booking):
    """start_of_business_day(ends_on + 7 days) in the business zone, as UTC."""
    return start_of_business_day(booking.ends_on + timedelta(days=7), get_settings().business_zone)


async def test_mark_lost_forfeits_and_pauses(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.in_progress)
    admin = await admin_actor(db_session)
    clock = FrozenClock(_lost_from(booking))

    result = await _transition(
        db_session,
        booking,
        admin,
        BookingEvent.mark_lost,
        clock,
        notifier,
        {"reason": "No contact for 7 days"},
    )

    assert result.status == BookingStatus.lost
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == ZERO
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == DEPOSIT
    renter = await db_session.get(User, booking.renter_id)
    await db_session.refresh(renter)
    assert renter.restricted_reason == "lost_item"
    assert renter.restricted_at == clock.now()
    asset = await db_session.get(Asset, booking.asset_id)
    await db_session.refresh(asset)
    assert asset.listing_status == ListingStatus.paused

    audits = await _audits(db_session, booking.id)
    assert [a.action for a in audits[-2:]] == ["deposit.forfeited", "booking.lost"]
    assert audits[-1].reason == "No contact for 7 days"
    assert audits[-1].actor_role == "admin"
    paused = (await _audits(db_session, booking.asset_id))[-1]
    assert paused.action == "asset.paused"
    assert (paused.before["listing_status"], paused.after["listing_status"]) == (
        "active",
        "paused",
    )
    restricted = (await _audits(db_session, booking.renter_id))[-1]
    assert restricted.action == "user.restricted"
    assert restricted.after["restricted_reason"] == "lost_item"
    assert {call.user_id for call in notifier.calls} >= {booking.renter_id, booking.vendor_id}
    assert "ops.lost" in notifier.templates()


async def test_mark_lost_before_t_plus_7_409(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.in_progress)
    admin = await admin_actor(db_session)
    clock = FrozenClock(_lost_from(booking) - timedelta(seconds=1))
    counts = await _counts(db_session)

    with pytest.raises(InvalidTransition) as caught:
        await _transition(
            db_session,
            booking,
            admin,
            BookingEvent.mark_lost,
            clock,
            notifier,
            {"reason": "No contact"},
        )

    assert caught.value.status_code == 409
    assert await _counts(db_session) == counts
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.in_progress


@pytest.mark.parametrize("payload", [None, {"reason": "  "}])
async def test_mark_lost_requires_reason(db_session, clock, notifier, payload) -> None:
    booking = await _with_deposit(db_session, BookingStatus.in_progress)
    admin = await admin_actor(db_session)
    clock = FrozenClock(_lost_from(booking))
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, admin, BookingEvent.mark_lost, clock, notifier, payload
        )

    assert caught.value.code == "reason_required"
    assert await _counts(db_session) == counts


async def test_vendor_cannot_mark_lost(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)
    clock = FrozenClock(_lost_from(booking))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            vendor_actor(booking),
            BookingEvent.mark_lost,
            clock,
            notifier,
            {"reason": "Gone"},
        )


async def test_mark_lost_on_paused_asset_writes_no_pause_row(db_session, clock, notifier) -> None:
    booking = await _with_deposit(db_session, BookingStatus.in_progress)
    asset = await db_session.get(Asset, booking.asset_id)
    asset.listing_status = ListingStatus.paused
    await db_session.flush()
    admin = await admin_actor(db_session)

    result = await _transition(
        db_session,
        booking,
        admin,
        BookingEvent.mark_lost,
        FrozenClock(_lost_from(booking)),
        notifier,
        {"reason": "Gone"},
    )

    assert result.status == BookingStatus.lost
    assert await _audits(db_session, booking.asset_id) == []
