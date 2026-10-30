"""Live PostgreSQL tests for the money transitions (Task 3.3).

Covers spec 3.1 rows ``accepted + payment_captured`` (deposit_paid -> confirmed; the
one upfront payment of A14), ``accepted + expire_deposit`` and ``confirmed + cancel``
(renter and vendor), with their ledger postings (spec 2.5).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import AuditEvent, Booking, LedgerAccount, LedgerEntry
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentStatus, PaymentType
from app.db.models.user import User
from app.schemas.errors import BookingGuardFailed, InvalidRequestError, InvalidTransition
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import account_balance
from app.services.notify import RecordingNotifier
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
RENTAL = D("150000.00")  # the money_snapshot default; no delivery fee
COMMISSION = D("22500.00")  # 15% of the rental
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL
START = date(2027, 1, 4)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def renter_actor(user_id: uuid.UUID) -> Actor:
    return Actor(user_id=user_id, role=ActorRole.renter)


def vendor_actor(user_id: uuid.UUID) -> Actor:
    return Actor(user_id=user_id, role=ActorRole.vendor)


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


async def _accepted(
    session: AsyncSession,
    *,
    deposit: Decimal = DEPOSIT,
    contribution: Decimal = CONTRIBUTION,
    rental: Decimal = RENTAL,
    starts_on: date = START,
    **snapshot: str,
) -> tuple[Booking, uuid.UUID]:
    """An accepted booking with a money snapshot and its captured upfront payment."""
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=deposit)
    booking = await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=BookingStatus.accepted,
        starts_on=starts_on,
        deposit_amount=deposit,
        total_rental_fee=rental,
        price_snapshot=money_snapshot(
            deposit=str(deposit),
            damage_contribution=str(contribution),
            rental=str(rental),
            **snapshot,
        ),
    )
    payment = await create_payment(
        session, booking, type=PaymentType.booking, amount=deposit + contribution + rental
    )
    return booking, payment.id


async def _confirmed(session, clock, notifier, **kwargs) -> Booking:
    booking, payment_id = await _accepted(session, **kwargs)
    deposit = kwargs.get("deposit", DEPOSIT)
    contribution = kwargs.get("contribution", CONTRIBUTION)
    rental = kwargs.get("rental", RENTAL)
    await _transition(
        session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": deposit + contribution + rental, "payment_id": payment_id},
    )
    notifier.calls.clear()
    return booking


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


async def _phone(session: AsyncSession, user_id: uuid.UUID) -> str:
    return (await session.get(User, user_id)).phone


# ---------------------------------------------------------------------------
# payment_captured (A14: D + C + R + F in one payment)
# ---------------------------------------------------------------------------


async def test_payment_captured_posts_one_group(db_session, clock, notifier) -> None:
    booking, payment_id = await _accepted(db_session)

    result = await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": TOTAL, "payment_id": payment_id},
    )

    assert result.status == BookingStatus.confirmed
    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == DEPOSIT
    assert await _balance(db_session, LedgerAccount.protection_pool, booking.id) == CONTRIBUTION
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == (
        RENTAL - COMMISSION
    )
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION
    assert await _balance(db_session, LedgerAccount.platform_cash, booking.id) == -TOTAL
    rows = await _ledger(db_session, booking.id)
    assert len(rows) == 5
    assert {row.payment_id for row in rows} == {payment_id}
    assert len({row.txn_group for row in rows}) == 1
    assert {row.memo for row in rows} == {"payment_captured"}

    audits = await _audits(db_session, booking.id)
    assert [a.action for a in audits[-2:]] == ["booking.deposit_paid", "booking.confirmed"]
    paid, confirmed = audits[-2:]
    assert (paid.before["status"], paid.after["status"]) == ("accepted", "deposit_paid")
    assert paid.after["amount"] == str(TOTAL)
    assert paid.after["payment_id"] == str(payment_id)
    assert paid.after["commission"] == str(COMMISSION)
    assert (confirmed.before["status"], confirmed.after["status"]) == ("deposit_paid", "confirmed")
    assert paid.actor_role == confirmed.actor_role == "system"

    by_user = {call.user_id: call for call in notifier.calls}
    assert notifier.templates() == ["booking.confirmed", "booking.confirmed"]
    renter_call = by_user[booking.renter_id]
    vendor_call = by_user[booking.vendor_id]
    assert renter_call.data["counterpart_phone"] == await _phone(db_session, booking.vendor_id)
    assert vendor_call.data["counterpart_phone"] == await _phone(db_session, booking.renter_id)


async def test_payment_captured_without_damage_contribution(db_session, clock, notifier) -> None:
    booking, payment_id = await _accepted(db_session, contribution=D("0.00"))

    await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": DEPOSIT + RENTAL, "payment_id": payment_id},
    )

    rows = await _ledger(db_session, booking.id)
    assert len(rows) == 4
    assert {row.account for row in rows} == {
        "platform_cash",
        "renter_deposit_liability",
        "vendor_payable",
        "platform_revenue",
    }
    assert await _balance(db_session, LedgerAccount.protection_pool, booking.id) == D("0.00")


@pytest.mark.parametrize(
    "amount",
    [
        DEPOSIT + CONTRIBUTION,  # the old deposit-only payment is no longer enough
        DEPOSIT,
        RENTAL,
        TOTAL - D("0.01"),
        TOTAL + D("0.01"),
        None,
        "abc",
        307500.0,
        "1e40",
        "1234567890123",
        "307500.001",
    ],
)
async def test_payment_captured_requires_full_amount(db_session, clock, notifier, amount) -> None:
    booking, payment_id = await _accepted(db_session)
    audits_before = len(await _audits(db_session, booking.id))
    payload = {"payment_id": payment_id}
    if amount is not None:
        payload["amount"] = amount

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            Actor.system(),
            BookingEvent.payment_captured,
            clock,
            notifier,
            payload,
        )

    assert caught.value.code == "amount_mismatch"
    assert caught.value.error_details["expected"] == str(TOTAL)
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.accepted
    assert await _ledger(db_session, booking.id) == []
    assert len(await _audits(db_session, booking.id)) == audits_before
    assert notifier.calls == []


async def test_payment_amount_compares_as_decimal(db_session, clock, notifier) -> None:
    booking, payment_id = await _accepted(db_session)

    result = await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": "307500.0", "payment_id": str(payment_id)},
    )

    assert result.status == BookingStatus.confirmed


async def test_payment_captured_needs_this_bookings_captured_payment(
    db_session, clock, notifier
) -> None:
    booking, _ = await _accepted(db_session)
    other, other_payment = await _accepted(db_session, starts_on=date(2027, 2, 1))
    deposit = await create_payment(db_session, booking, amount=TOTAL)
    balance = await create_payment(
        db_session, booking, type=PaymentType.rental_balance, amount=TOTAL
    )
    pending = await create_payment(
        db_session, booking, type=PaymentType.booking, amount=TOTAL, status=PaymentStatus.pending
    )

    for payment_id in (
        other_payment,
        deposit.id,
        balance.id,
        pending.id,
        uuid.uuid4(),
        "not-a-uuid",
        None,
    ):
        with pytest.raises(InvalidRequestError):
            await _transition(
                db_session,
                booking,
                Actor.system(),
                BookingEvent.payment_captured,
                clock,
                notifier,
                {"amount": TOTAL, "payment_id": payment_id},
            )

    await db_session.refresh(booking)
    assert booking.status == BookingStatus.accepted
    assert await _ledger(db_session, booking.id) == []


@pytest.mark.parametrize("role", ["renter", "vendor"])
async def test_only_system_captures_payment(db_session, clock, notifier, role) -> None:
    booking, payment_id = await _accepted(db_session)
    actor = renter_actor(booking.renter_id) if role == "renter" else vendor_actor(booking.vendor_id)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            actor,
            BookingEvent.payment_captured,
            clock,
            notifier,
            {"amount": TOTAL, "payment_id": payment_id},
        )


async def test_second_payment_capture_is_invalid(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=TOTAL)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            Actor.system(),
            BookingEvent.payment_captured,
            clock,
            notifier,
            {"amount": TOTAL, "payment_id": payment.id},
        )
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == DEPOSIT
    assert len(await _ledger(db_session, booking.id)) == 5


async def test_commission_is_frozen_in_the_snapshot(db_session, clock, notifier) -> None:
    booking, payment_id = await _accepted(db_session)
    booking.price_snapshot = {**booking.price_snapshot, "commission_rate": "0.125"}
    await db_session.flush()

    await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": TOTAL, "payment_id": payment_id},
    )

    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == D("18750.00")


# ---------------------------------------------------------------------------
# expire_deposit
# ---------------------------------------------------------------------------


async def test_expire_deposit_after_due(db_session, clock, notifier) -> None:
    booking, _ = await _accepted(db_session)
    clock.advance(booking.deposit_due_at - DEFAULT_NOW + timedelta(seconds=1))

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.expire_deposit, clock, notifier
    )

    assert result.status == BookingStatus.expired
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}
    assert set(notifier.templates()) == {"booking.expired"}
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.expired"
    assert audit.before["status"] == "accepted"
    assert await _ledger(db_session, booking.id) == []


async def test_expire_deposit_at_exact_due_is_invalid(db_session, clock, notifier) -> None:
    booking, _ = await _accepted(db_session)
    clock.advance(booking.deposit_due_at - DEFAULT_NOW)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.expire_deposit, clock, notifier
        )


async def test_vendor_cannot_expire_deposit(db_session, clock, notifier) -> None:
    booking, _ = await _accepted(db_session)
    clock.advance(timedelta(days=2))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            vendor_actor(booking.vendor_id),
            BookingEvent.expire_deposit,
            clock,
            notifier,
        )


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

FEE = D("15000.00")  # 10% of the 150000.00 rental (A13 placeholder rate)
FEE_PART = D("7500.00")  # half of the fee each to the vendor and to Vouch


async def _assert_refunded(session, booking_id: uuid.UUID) -> None:
    """Every booking-scoped account is back to 0 (a full refund)."""
    for account in (
        LedgerAccount.renter_deposit_liability,
        LedgerAccount.protection_pool,
        LedgerAccount.vendor_payable,
        LedgerAccount.platform_revenue,
        LedgerAccount.platform_cash,
    ):
        assert await _balance(session, account, booking_id) == D("0.00"), account


async def _assert_refunded_minus_fee(
    session, booking_id: uuid.UUID, vendor_part: Decimal, platform_part: Decimal
) -> None:
    """Everything paid went back except the fee, which the vendor and Vouch keep."""
    for account in (LedgerAccount.renter_deposit_liability, LedgerAccount.protection_pool):
        assert await _balance(session, account, booking_id) == D("0.00"), account
    assert await _balance(session, LedgerAccount.vendor_payable, booking_id) == vendor_part
    assert await _balance(session, LedgerAccount.platform_revenue, booking_id) == platform_part
    # Credit-normal (cr - dr): the fee is cash the platform still holds.
    assert await _balance(session, LedgerAccount.platform_cash, booking_id) == -(
        vendor_part + platform_part
    )


async def _whole_ledger_balanced(session: AsyncSession) -> bool:
    result = await session.execute(
        select(LedgerEntry.direction, func.sum(LedgerEntry.amount)).group_by(LedgerEntry.direction)
    )
    totals = dict(result.all())
    return totals.get("dr", D("0")) == totals.get("cr", D("0"))


async def test_renter_cancel_confirmed_refunds_all_but_the_fee(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)

    result = await _transition(
        db_session,
        booking,
        renter_actor(booking.renter_id),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "  Plans changed  "},
    )

    assert result.status == BookingStatus.cancelled_by_renter
    assert result.cancelled_at == DEFAULT_NOW
    assert result.cancellation_reason == "Plans changed"
    await _assert_refunded_minus_fee(db_session, booking.id, FEE_PART, FEE_PART)
    # Capture (5), deposit (2), contribution (2) and rental (3) reversals, fee (3).
    rows = await _ledger(db_session, booking.id)
    assert len(rows) == 5 + 2 + 2 + 3 + 3
    fee_rows = [row for row in rows if row.memo == "cancellation_fee"]
    assert len(fee_rows) == 3
    assert len({row.txn_group for row in fee_rows}) == 1
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.cancelled_by_renter"
    assert audit.actor_role == "renter"
    assert audit.reason == "Plans changed"
    assert audit.after["refund"] == {
        "deposit": str(DEPOSIT),
        "damage_contribution": str(CONTRIBUTION),
        "rental": str(RENTAL),
        "delivery_fee": "0.00",
        "commission": str(COMMISSION),
    }
    assert audit.after["cancellation_fee"] == {
        "amount": str(FEE),
        "vendor_part": str(FEE_PART),
        "platform_part": str(FEE_PART),
    }
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}
    assert set(notifier.templates()) == {"booking.cancelled"}


async def test_renter_cancel_confirmed_charges_split_fee(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier, rental=D("45000.00"))

    await _transition(
        db_session, booking, renter_actor(booking.renter_id), BookingEvent.cancel, clock, notifier
    )

    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == D("0.00")
    assert await _balance(db_session, LedgerAccount.protection_pool, booking.id) == D("0.00")
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == D("2250.00")
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == D("2250.00")
    assert await _balance(db_session, LedgerAccount.platform_cash, booking.id) == D("-4500.00")
    assert await _whole_ledger_balanced(db_session)
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.after["cancellation_fee"]["amount"] == "4500.00"


async def test_cancellation_fee_rates_are_frozen_in_the_snapshot(
    db_session, clock, notifier
) -> None:
    # 0.125 x 150000.00 = 18750.00; the vendor gets 0.3 of it = 5625.00.
    booking = await _confirmed(
        db_session,
        clock,
        notifier,
        cancellation_fee_rate="0.125",
        cancellation_fee_vendor_share="0.3",
    )

    await _transition(
        db_session, booking, renter_actor(booking.renter_id), BookingEvent.cancel, clock, notifier
    )

    await _assert_refunded_minus_fee(db_session, booking.id, D("5625.00"), D("13125.00"))


async def test_zero_fee_writes_no_fee_group(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier, cancellation_fee_rate="0")

    await _transition(
        db_session, booking, renter_actor(booking.renter_id), BookingEvent.cancel, clock, notifier
    )

    await _assert_refunded(db_session, booking.id)
    assert [r for r in await _ledger(db_session, booking.id) if r.memo == "cancellation_fee"] == []


async def test_renter_cancel_without_reason_or_contribution(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier, contribution=D("0.00"))

    result = await _transition(
        db_session, booking, renter_actor(booking.renter_id), BookingEvent.cancel, clock, notifier
    )

    assert result.status == BookingStatus.cancelled_by_renter
    assert result.cancellation_reason is None
    await _assert_refunded_minus_fee(db_session, booking.id, FEE_PART, FEE_PART)
    assert len(await _ledger(db_session, booking.id)) == 4 + 2 + 3 + 3


async def test_cancel_at_2330_utc_day_before_start_is_refused(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier, starts_on=date(2027, 1, 4))
    clock = FrozenClock(datetime(2027, 1, 3, 23, 30, tzinfo=UTC))  # 00:30 WAT on the 4th

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            renter_actor(booking.renter_id),
            BookingEvent.cancel,
            clock,
            notifier,
        )

    assert caught.value.code == "cancel_window_closed"
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.confirmed
    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == DEPOSIT
    assert len(await _ledger(db_session, booking.id)) == 5


async def test_cancel_at_2259_utc_day_before_start_is_allowed(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier, starts_on=date(2027, 1, 4))
    clock = FrozenClock(datetime(2027, 1, 3, 22, 59, tzinfo=UTC))  # 23:59 WAT on the 3rd

    result = await _transition(
        db_session, booking, renter_actor(booking.renter_id), BookingEvent.cancel, clock, notifier
    )

    assert result.status == BookingStatus.cancelled_by_renter
    await _assert_refunded_minus_fee(db_session, booking.id, FEE_PART, FEE_PART)


async def test_cancel_at_exact_start_of_business_day_is_refused(
    db_session, clock, notifier
) -> None:
    booking = await _confirmed(db_session, clock, notifier, starts_on=date(2027, 1, 4))
    clock = FrozenClock(datetime(2027, 1, 3, 23, 0, tzinfo=UTC))  # 00:00 WAT on the 4th

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            renter_actor(booking.renter_id),
            BookingEvent.cancel,
            clock,
            notifier,
        )
    assert caught.value.code == "cancel_window_closed"


async def test_vendor_cancel_confirmed_full_refund(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier, starts_on=date(2027, 1, 4))
    clock = FrozenClock(datetime(2027, 1, 5, 12, 0, tzinfo=UTC))  # any time, even after the start

    result = await _transition(
        db_session,
        booking,
        vendor_actor(booking.vendor_id),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Lens is broken"},
    )

    assert result.status == BookingStatus.cancelled_by_vendor
    assert result.cancelled_at == clock.now()
    assert result.cancellation_reason == "Lens is broken"
    await _assert_refunded(db_session, booking.id)
    assert await _whole_ledger_balanced(db_session)
    assert [r for r in await _ledger(db_session, booking.id) if r.memo == "cancellation_fee"] == []
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.cancelled_by_vendor"
    assert audit.reason == "Lens is broken"
    assert "cancellation_fee" not in audit.after

    penalties = [
        a
        for a in await _audits(db_session, booking.vendor_id)
        if a.action == "vendor.reliability_penalty"
    ]
    assert len(penalties) == 1
    penalty = penalties[0]
    assert penalty.entity == "user"
    assert penalty.actor_role == "vendor"
    assert penalty.after["booking_id"] == str(booking.id)
    assert penalty.reason == "Lens is broken"


@pytest.mark.parametrize("paid", [False, True], ids=["accepted", "confirmed"])
@pytest.mark.parametrize("payload", [None, {"reason": "   "}, {"reason": 7}])
async def test_vendor_cancel_requires_reason(db_session, clock, notifier, payload, paid) -> None:
    if paid:
        booking = await _confirmed(db_session, clock, notifier)
    else:
        booking, _ = await _accepted(db_session)
    status = booking.status
    audits_before = await db_session.scalar(select(func.count()).select_from(AuditEvent))

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session,
            booking,
            vendor_actor(booking.vendor_id),
            BookingEvent.cancel,
            clock,
            notifier,
            payload,
        )

    assert caught.value.code == "reason_required"
    await db_session.refresh(booking)
    assert booking.status == status
    assert booking.cancelled_at is None
    assert await db_session.scalar(select(func.count()).select_from(AuditEvent)) == audits_before
    assert len(await _ledger(db_session, booking.id)) == (5 if paid else 0)


async def test_renter_cannot_cancel_other_booking(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)
    stranger = await create_user(db_session, kyc_verified=True)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, renter_actor(stranger.id), BookingEvent.cancel, clock, notifier
        )

    await db_session.refresh(booking)
    assert booking.status == BookingStatus.confirmed


async def test_other_vendor_cannot_cancel(db_session, clock, notifier) -> None:
    booking = await _confirmed(db_session, clock, notifier)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            vendor_actor(uuid.uuid4()),
            BookingEvent.cancel,
            clock,
            notifier,
            {"reason": "x"},
        )


# ---------------------------------------------------------------------------
# accepted + cancel: free withdrawal before payment (A13, R14)
# ---------------------------------------------------------------------------


async def test_renter_withdraws_accepted_free(db_session, clock, notifier) -> None:
    booking, _ = await _accepted(db_session)
    # Any time before payment, even after the start date: nothing has been paid.
    clock.advance(timedelta(days=90))

    result = await _transition(
        db_session,
        booking,
        renter_actor(booking.renter_id),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Found another camera"},
    )

    assert result.status == BookingStatus.cancelled_by_renter
    assert result.cancelled_at == clock.now()
    assert result.cancellation_reason == "Found another camera"
    assert await _ledger(db_session, booking.id) == []
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.cancelled_by_renter"
    assert audit.actor_role == "renter"
    assert (audit.before["status"], audit.after["status"]) == ("accepted", "cancelled_by_renter")
    assert "refund" not in audit.after
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (booking.vendor_id, "booking.withdrawn")
    ]
    assert await _audits(db_session, booking.vendor_id) == []  # no penalty for anyone


async def test_vendor_cancels_accepted_unpaid(db_session, clock, notifier) -> None:
    booking, _ = await _accepted(db_session)

    result = await _transition(
        db_session,
        booking,
        vendor_actor(booking.vendor_id),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Double-booked offline"},
    )

    assert result.status == BookingStatus.cancelled_by_vendor
    assert result.cancellation_reason == "Double-booked offline"
    assert await _ledger(db_session, booking.id) == []
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.cancelled_by_vendor"
    assert audit.before["status"] == "accepted"
    penalties = await _audits(db_session, booking.vendor_id)
    assert [a.action for a in penalties] == ["vendor.reliability_penalty"]
    assert penalties[0].after["booking_id"] == str(booking.id)
    assert penalties[0].reason == "Double-booked offline"
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}
    assert set(notifier.templates()) == {"booking.cancelled"}
