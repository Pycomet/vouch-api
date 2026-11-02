"""Live PostgreSQL tests for the rest of the payment-row lifecycle (Task 5.3, fix round 1).

* N1: when a booking leaves ``accepted`` unpaid (deposit expiry, renter withdraw,
  vendor cancel), its pending ``booking`` payment becomes ``failed``.
* Every return of money to the renter writes a pending ``refund`` row: the
  deposit release on completion (``deposit_released``), and an admin's release
  (``resolve_release``, D) or deduction (``resolve_deduct``, D - X). A forfeit
  (lost, or resolve forfeit) returns nothing and writes no row. The row's amount
  equals what the ledger group pays out of platform cash, and the group points at it.
* A refund of 0.00 never writes a row.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock, start_of_business_day
from app.core.config import get_settings
from app.db.models import AuditEvent, Booking, LedgerAccount, LedgerEntry, Payment
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentStatus, PaymentType
from app.db.models.user import RenterRole
from app.providers.payment import FakePaymentProvider
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
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
RENTAL = D("150000.00")
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider("webhook-secret-for-tests")


def _renter(booking: Booking) -> Actor:
    return Actor(user_id=booking.renter_id, role=ActorRole.renter)


def _vendor(booking: Booking) -> Actor:
    return Actor(user_id=booking.vendor_id, role=ActorRole.vendor)


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
    )


async def _booking(session: AsyncSession, status: BookingStatus, **kwargs) -> Booking:
    deposit = kwargs.pop("deposit", DEPOSIT)
    snapshot = kwargs.pop("snapshot", {})
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=deposit)
    return await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        deposit_amount=deposit,
        total_rental_fee=RENTAL,
        price_snapshot=money_snapshot(
            deposit=str(deposit),
            damage_contribution=str(CONTRIBUTION),
            rental=str(RENTAL),
            **snapshot,
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


async def _refunds(session: AsyncSession, booking_id: uuid.UUID) -> list[Payment]:
    return [p for p in await _payments(session, booking_id) if p.type == PaymentType.refund]


async def _cash_out(session: AsyncSession, booking_id: uuid.UUID, memo: str) -> Decimal:
    """What ledger group ``memo`` pays out of platform cash: the renter's money back."""
    rows = await session.execute(
        select(LedgerEntry.direction, LedgerEntry.amount).where(
            LedgerEntry.booking_id == booking_id,
            LedgerEntry.memo == memo,
            LedgerEntry.account == LedgerAccount.platform_cash,
        )
    )
    return sum(
        (amount if direction == "cr" else -amount for direction, amount in rows.all()), D("0")
    )


async def _group_payment_ids(session: AsyncSession, booking_id: uuid.UUID, memo: str) -> set:
    rows = await session.scalars(
        select(LedgerEntry.payment_id).where(
            LedgerEntry.booking_id == booking_id, LedgerEntry.memo == memo
        )
    )
    return set(rows)


# ---------------------------------------------------------------------------
# N1: the pending account fails when the booking closes unpaid
# ---------------------------------------------------------------------------


async def _accepted(session, clock, notifier, provider) -> Booking:
    booking = await _booking(session, BookingStatus.requested)
    await _transition(
        session, booking, _vendor(booking), BookingEvent.accept, clock, notifier, provider
    )
    return booking


async def test_expire_deposit_fails_the_pending_booking_payment(
    db_session, clock, notifier, provider
) -> None:
    booking = await _accepted(db_session, clock, notifier, provider)
    clock.advance(timedelta(hours=12, seconds=1))

    await _transition(
        db_session, booking, Actor.system(), BookingEvent.expire_deposit, clock, notifier
    )

    [payment] = await _payments(db_session, booking.id)
    assert payment.type == PaymentType.booking
    assert payment.status == PaymentStatus.failed
    assert payment.failed_at == clock.now()


@pytest.mark.parametrize("by", ["renter", "vendor"])
async def test_cancel_accepted_fails_the_pending_booking_payment(
    db_session, clock, notifier, provider, by
) -> None:
    booking = await _accepted(db_session, clock, notifier, provider)
    actor = _renter(booking) if by == "renter" else _vendor(booking)

    await _transition(
        db_session, booking, actor, BookingEvent.cancel, clock, notifier, reason="Plans changed"
    )

    [payment] = await _payments(db_session, booking.id)
    assert payment.status == PaymentStatus.failed
    assert payment.failed_at == clock.now()


async def test_withdrawing_a_request_touches_no_payment(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    await _transition(db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier)

    assert await _payments(db_session, booking.id) == []


async def test_only_a_pending_booking_payment_fails(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    captured = await create_payment(db_session, booking, type=PaymentType.booking, amount=TOTAL)

    await _transition(db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier)

    [row] = await _payments(db_session, booking.id)
    assert (row.id, row.status) == (captured.id, PaymentStatus.captured)


# ---------------------------------------------------------------------------
# Deposit returns write refund rows
# ---------------------------------------------------------------------------


async def _paid(session, status: BookingStatus, **kwargs) -> tuple[Booking, Payment]:
    booking = await _booking(session, status, **kwargs)
    paid = await create_payment(session, booking, type=PaymentType.booking, amount=TOTAL)
    return booking, paid


async def test_complete_records_the_deposit_refund(db_session, clock, notifier) -> None:
    booking, paid = await _paid(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - clock.now() + timedelta(seconds=1))

    await _transition(db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier)

    [refund] = await _refunds(db_session, booking.id)
    assert refund.status == PaymentStatus.pending
    assert refund.amount == DEPOSIT
    assert refund.provider == paid.provider
    assert refund.metadata_ == {"reason": "deposit_released", "refund_of": str(paid.id)}
    assert await _cash_out(db_session, booking.id, "deposit_released") == refund.amount
    assert await _group_payment_ids(db_session, booking.id, "deposit_released") == {refund.id}


async def _resolve(session, booking, clock, notifier, **payload) -> None:
    admin = await create_user(session, role=RenterRole.admin)
    await _transition(
        session,
        booking,
        Actor(user_id=admin.id, role=ActorRole.admin),
        BookingEvent.resolve,
        clock,
        notifier,
        reason="Reviewed",
        **payload,
    )


async def test_resolve_release_records_the_deposit_refund(db_session, clock, notifier) -> None:
    booking, paid = await _paid(db_session, BookingStatus.disputed, claimed=True)

    await _resolve(db_session, booking, clock, notifier, decision="release")

    [refund] = await _refunds(db_session, booking.id)
    assert refund.amount == DEPOSIT
    assert refund.metadata_ == {"reason": "resolve_release", "refund_of": str(paid.id)}
    assert await _cash_out(db_session, booking.id, "resolve_release") == refund.amount
    assert await _group_payment_ids(db_session, booking.id, "resolve_release") == {refund.id}


async def test_resolve_deduct_refunds_the_rest_of_the_deposit(db_session, clock, notifier) -> None:
    booking, _ = await _paid(db_session, BookingStatus.disputed, claimed=True)

    await _resolve(db_session, booking, clock, notifier, decision="deduct", amount="40000.00")

    [refund] = await _refunds(db_session, booking.id)
    assert refund.amount == DEPOSIT - D("40000.00")
    assert refund.metadata_["reason"] == "resolve_deduct"
    assert await _cash_out(db_session, booking.id, "resolve_deduct") == refund.amount
    assert await _group_payment_ids(db_session, booking.id, "resolve_deduct") == {refund.id}


async def test_resolve_deduct_of_the_whole_deposit_records_no_refund(
    db_session, clock, notifier
) -> None:
    booking, _ = await _paid(db_session, BookingStatus.disputed, claimed=True)

    await _resolve(db_session, booking, clock, notifier, decision="deduct", amount="999999.00")

    assert await _refunds(db_session, booking.id) == []
    assert await _cash_out(db_session, booking.id, "resolve_deduct") == D("0")
    assert await _group_payment_ids(db_session, booking.id, "resolve_deduct") == {None}


async def test_resolve_forfeit_records_no_refund(db_session, clock, notifier) -> None:
    booking, _ = await _paid(db_session, BookingStatus.disputed, claimed=True)

    await _resolve(db_session, booking, clock, notifier, decision="forfeit")

    assert await _refunds(db_session, booking.id) == []


async def test_mark_lost_records_no_refund(db_session, clock, notifier) -> None:
    booking, _ = await _paid(db_session, BookingStatus.in_progress)
    zone = get_settings().business_zone
    clock.advance(start_of_business_day(booking.ends_on + timedelta(days=7), zone) - clock.now())
    admin = await create_user(db_session, role=RenterRole.admin)

    await _transition(
        db_session,
        booking,
        Actor(user_id=admin.id, role=ActorRole.admin),
        BookingEvent.mark_lost,
        clock,
        notifier,
        reason="Not returned",
    )

    assert await _refunds(db_session, booking.id) == []


# ---------------------------------------------------------------------------
# Never a refund row for 0.00
# ---------------------------------------------------------------------------


async def test_a_zero_cancel_refund_writes_no_row(db_session, clock, notifier) -> None:
    """Fee rate 1.0 with D = C = F = 0: the fee keeps everything, nothing goes back."""
    booking = await _booking(
        db_session,
        BookingStatus.accepted,
        deposit=D("0.00"),
        snapshot={"cancellation_fee_rate": "1.0"},
    )
    booking.price_snapshot = {**booking.price_snapshot, "damage_contribution": "0.00"}
    await db_session.flush()
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=RENTAL)
    await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        amount=RENTAL,
        payment_id=payment.id,
    )

    await _transition(db_session, booking, _renter(booking), BookingEvent.cancel, clock, notifier)

    reloaded = await db_session.get(Booking, booking.id, populate_existing=True)
    assert reloaded.status == BookingStatus.cancelled_by_renter
    assert await _refunds(db_session, booking.id) == []
    audit = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == booking.id,
                AuditEvent.action == "booking.cancelled_by_renter",
            )
        )
    ).one()
    assert audit.after["refund_payment"] is None
    assert audit.after["cancellation_fee"]["amount"] == str(RENTAL)


async def test_complete_with_no_deposit_writes_no_row(db_session, clock, notifier) -> None:
    booking, _ = await _paid(db_session, BookingStatus.returned, deposit=D("0.00"))
    clock.advance(booking.claim_window_ends_at - clock.now() + timedelta(seconds=1))

    await _transition(db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier)

    reloaded = await db_session.get(Booking, booking.id, populate_existing=True)
    assert reloaded.status == BookingStatus.completed
    assert await _refunds(db_session, booking.id) == []
