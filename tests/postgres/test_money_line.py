"""Live PostgreSQL tests for the money line (spec 3.4, Task 4.2).

``compute_money_line`` reads the booking's ledger postings, its last ``deposit.*``
audit row and its status; nothing is stored. These tests drive bookings through
the real transitions (or post ledger groups directly where no transition exists
yet, such as payouts) and check each part of the line.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import Booking, Payment
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection
from app.db.models.payment import PaymentStatus, PaymentType, PayoutStatus
from app.db.models.user import RenterRole
from app.providers.payment import FakePaymentProvider
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import post_txn
from app.services.money import payout_postings
from app.services.money_line import compute_money_line, local_day, local_time
from app.services.notify import RecordingNotifier
from app.services.webhooks import handle_webhook
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
DEPOSIT = D("150000.00")
CONTRIBUTION = D("7500.00")
RENTAL = D("150000.00")
DELIVERY = D("5000.00")
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL + DELIVERY
START = date(2027, 1, 8)  # a Friday


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


async def _booking(
    session: AsyncSession, status: BookingStatus, *, deposit: Decimal = DEPOSIT
) -> Booking:
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=deposit)
    return await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        starts_on=START,
        ends_on=START + timedelta(days=2),
        deposit_amount=deposit,
        total_rental_fee=RENTAL,
        price_snapshot=money_snapshot(
            deposit=str(deposit),
            damage_contribution=str(CONTRIBUTION),
            rental=str(RENTAL),
            delivery_fee=str(DELIVERY),
        ),
    )


async def _go(session, booking, actor, event, clock, notifier, payload=None) -> Booking:
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


async def _pay(session, booking, clock, notifier, *, deposit: Decimal = DEPOSIT) -> Booking:
    total = deposit + CONTRIBUTION + RENTAL + DELIVERY
    payment = await create_payment(session, booking, type=PaymentType.booking, amount=total)
    return await _go(
        session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": total, "payment_id": payment.id},
    )


async def _returned(session, clock, notifier) -> Booking:
    booking = await _booking(session, BookingStatus.accepted)
    await _pay(session, booking, clock, notifier)
    await create_handover(session, booking, direction=HandoverDirection.pickup)
    await _go(session, booking, Actor.system(), BookingEvent.handover_completed, clock, notifier)
    await create_handover(session, booking, direction=HandoverDirection.return_)
    await _go(session, booking, Actor.system(), BookingEvent.return_completed, clock, notifier)
    return booking


async def _line(session, booking):
    return await compute_money_line(session, booking, settings=get_settings())


def vendor(booking: Booking) -> Actor:
    return Actor(user_id=booking.vendor_id, role=ActorRole.vendor)


def renter(booking: Booking) -> Actor:
    return Actor(user_id=booking.renter_id, role=ActorRole.renter)


async def admin(session: AsyncSession) -> Actor:
    user = await create_user(session, role=RenterRole.admin)
    return Actor(user_id=user.id, role=ActorRole.admin)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_local_day_and_time_use_the_business_zone() -> None:
    zone = get_settings().business_zone
    # 13:00 UTC is 14:00 in West Africa Time (UTC+1).
    assert local_time(datetime(2026, 10, 9, 13, 0, tzinfo=UTC), zone) == "Fri 9 Oct 14:00"
    # 23:30 UTC on Friday is already Saturday locally.
    assert local_time(datetime(2026, 10, 9, 23, 30, tzinfo=UTC), zone) == "Sat 10 Oct 00:30"
    assert local_day(date(2026, 10, 10)) == "Sat 10 Oct"


# ---------------------------------------------------------------------------
# Before payment
# ---------------------------------------------------------------------------


async def test_requested_awaits_the_vendor(db_session) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    line = await _line(db_session, booking)

    assert line.deposit.amount == "150000.00"
    assert line.deposit.status == "due"
    assert line.deposit.held_by == "platform"
    assert line.rental.amount == str(RENTAL + DELIVERY)
    assert line.rental.status == "due"
    assert line.next.label == "Awaiting vendor response"
    assert line.next.amount_due == "0.00"


async def test_accepted_asks_for_the_whole_payment(db_session) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    booking.deposit_due_at = datetime(2027, 1, 5, 13, 0, tzinfo=UTC)

    line = await _line(db_session, booking)

    assert line.deposit.status == "due"
    assert line.rental.status == "due"
    assert line.next.label == "Payment due Tue 5 Jan 14:00"
    assert line.next.amount_due == str(TOTAL)
    assert D(line.next.amount_due) == DEPOSIT + CONTRIBUTION + RENTAL + DELIVERY


@pytest.mark.parametrize(
    "status",
    [
        BookingStatus.declined,
        BookingStatus.expired,
        BookingStatus.cancelled_by_renter,
        BookingStatus.cancelled_by_vendor,
    ],
)
async def test_closed_before_payment_owes_nothing(db_session, status) -> None:
    booking = await _booking(db_session, status)

    line = await _line(db_session, booking)

    # A18: nothing was ever paid, so every part is "cancelled" and nothing is owed.
    assert (line.deposit.status, line.rental.status) == ("cancelled", "cancelled")
    assert line.contribution.status == "cancelled"
    assert line.refund is None
    assert line.cancellation_fee is None
    assert (line.next.label, line.next.amount_due) == ("Closed", "0.00")


# ---------------------------------------------------------------------------
# Paid
# ---------------------------------------------------------------------------


async def test_confirmed_holds_the_deposit(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    await _pay(db_session, booking, clock, notifier)

    line = await _line(db_session, booking)

    assert line.deposit.status == "held"
    assert line.rental.status == "received"
    assert line.next.label == "Handover Fri 8 Jan"
    assert line.next.amount_due == "0.00"


async def test_zero_deposit_paid_booking_counts_as_held(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted, deposit=D("0.00"))
    await _pay(db_session, booking, clock, notifier, deposit=D("0.00"))

    line = await _line(db_session, booking)

    assert (line.deposit.amount, line.deposit.status) == ("0.00", "held")
    assert line.rental.status == "received"


async def test_in_progress_and_returned_labels(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    await _pay(db_session, booking, clock, notifier)
    await create_handover(db_session, booking, direction=HandoverDirection.pickup)
    await _go(db_session, booking, Actor.system(), BookingEvent.handover_completed, clock, notifier)

    line = await _line(db_session, booking)
    assert line.next.label == "Return by Sun 10 Jan"
    assert (line.deposit.status, line.rental.status) == ("held", "received")

    await create_handover(db_session, booking, direction=HandoverDirection.return_)
    await _go(db_session, booking, Actor.system(), BookingEvent.return_completed, clock, notifier)

    line = await _line(db_session, booking)
    # DEFAULT_NOW 09:00 UTC + 24 h = 10:00 local the next day.
    assert line.next.label == "Claim window closes Tue 3 Nov 10:00"
    assert line.next.amount_due == "0.00"


async def test_disputed_is_under_review(db_session, clock, notifier) -> None:
    booking = await _returned(db_session, clock, notifier)
    await _go(
        db_session,
        booking,
        vendor(booking),
        BookingEvent.claim_raised,
        clock,
        notifier,
        {"amount": D("20000.00"), "description": "Cracked screen", "evidence": ["https://e/1"]},
    )

    line = await _line(db_session, booking)

    assert line.deposit.status == "held"
    assert (line.next.label, line.next.amount_due) == ("Under review", "0.00")


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("release", "released"), ("deduct", "deducted"), ("forfeit", "forfeited")],
)
async def test_resolution_sets_the_deposit_status(
    db_session, clock, notifier, decision, expected
) -> None:
    booking = await _returned(db_session, clock, notifier)
    await _go(
        db_session,
        booking,
        vendor(booking),
        BookingEvent.claim_raised,
        clock,
        notifier,
        {"amount": D("20000.00"), "description": "Cracked screen", "evidence": ["https://e/1"]},
    )
    await _go(
        db_session,
        booking,
        await admin(db_session),
        BookingEvent.resolve,
        clock,
        notifier,
        {"decision": decision, "reason": "Checked the photos", "amount": D("20000.00")},
    )

    line = await _line(db_session, booking)

    assert line.deposit.status == expected
    assert line.rental.status == "received"
    assert (line.next.label, line.next.amount_due) == ("Closed", "0.00")


async def test_completed_without_claim_releases(db_session, clock, notifier) -> None:
    booking = await _returned(db_session, clock, notifier)
    clock.advance(timedelta(hours=25))
    await _go(db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier)

    line = await _line(db_session, booking)

    assert (line.deposit.status, line.rental.status) == ("released", "received")


async def test_paid_cancel_refunds(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    await _pay(db_session, booking, clock, notifier)
    await _go(db_session, booking, renter(booking), BookingEvent.cancel, clock, notifier)

    line = await _line(db_session, booking)

    assert line.deposit.status == "released"
    assert line.rental.status == "refunded"
    assert (line.next.label, line.next.amount_due) == ("Closed", "0.00")


# ---------------------------------------------------------------------------
# Payouts (no payout transition exists in M2; the rule is implemented now)
# ---------------------------------------------------------------------------


async def _payout(session: AsyncSession, booking: Booking, amount: Decimal) -> None:
    payment = Payment(
        booking_id=booking.id,
        type=PaymentType.payout,
        status=PaymentStatus.captured,
        amount=amount,
        vendor_recipient_code="RCP_test",
        platform_fee=D("0.00"),
        payout_status=PayoutStatus.paid,
        payout_eligible_at=DEFAULT_NOW,
        paid_at=DEFAULT_NOW,
    )
    session.add(payment)
    await session.flush()
    await post_txn(
        session,
        postings=payout_postings(amount),
        booking_id=booking.id,
        payment_id=payment.id,
        memo="payout",
    )


async def test_rental_paid_out_once_payouts_cover_the_payable(db_session, clock, notifier) -> None:
    booking = await _returned(db_session, clock, notifier)
    clock.advance(timedelta(hours=25))
    await _go(db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier)
    payable = RENTAL + DELIVERY - D("22500.00")  # less the 15% commission on the rental

    await _payout(db_session, booking, payable - D("100.00"))
    assert (await _line(db_session, booking)).rental.status == "received"

    await _payout(db_session, booking, D("100.00"))
    assert (await _line(db_session, booking)).rental.status == "paid_out"


# ---------------------------------------------------------------------------
# Contribution, cancellation fee and refund: the line adds up
# ---------------------------------------------------------------------------

FEE = (RENTAL * D("0.10")).quantize(D("0.01"))
FEE_VENDOR = (FEE * D("0.50")).quantize(D("0.01"))


def _paid_in(line) -> Decimal:
    """D + C + (R + F) as the line shows them."""
    return D(line.deposit.amount) + D(line.contribution.amount) + D(line.rental.amount)


async def test_contribution_is_due_then_received(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    booking.deposit_due_at = datetime(2027, 1, 5, 13, 0, tzinfo=UTC)

    line = await _line(db_session, booking)
    assert (line.contribution.amount, line.contribution.status) == ("7500.00", "due")
    # (a) accepted: what is due is exactly D + C + (R + F).
    assert D(line.next.amount_due) == _paid_in(line)
    assert line.cancellation_fee is None
    assert line.refund is None

    await _pay(db_session, booking, clock, notifier)
    line = await _line(db_session, booking)
    assert line.contribution.status == "received"
    assert line.refund is None


async def test_no_contribution_line_when_contribution_is_zero(db_session) -> None:
    renter_user = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await create_booking(
        db_session,
        asset=asset,
        renter=renter_user,
        status=BookingStatus.requested,
        price_snapshot=money_snapshot(damage_contribution="0.00"),
    )

    line = await _line(db_session, booking)

    assert line.contribution is None
    assert "contribution" not in line.model_dump()


async def test_paid_renter_cancel_adds_up(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    await _pay(db_session, booking, clock, notifier)
    await _go(db_session, booking, renter(booking), BookingEvent.cancel, clock, notifier)

    line = await _line(db_session, booking)

    assert line.contribution.status == "refunded"
    assert line.cancellation_fee.amount == str(FEE)
    assert line.cancellation_fee.vendor_part == str(FEE_VENDOR)
    assert line.cancellation_fee.platform_part == str(FEE - FEE_VENDOR)
    assert line.refund.amount == str(TOTAL - FEE)
    # (b) refund + fee == D + C + R + F.
    assert D(line.refund.amount) + D(line.cancellation_fee.amount) == _paid_in(line)


async def test_paid_vendor_cancel_refunds_everything(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    await _pay(db_session, booking, clock, notifier)
    await _go(
        db_session,
        booking,
        vendor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Kit unavailable"},
    )

    line = await _line(db_session, booking)

    assert line.contribution.status == "refunded"
    assert line.cancellation_fee is None
    # (c) refund == D + C + R + F.
    assert D(line.refund.amount) == TOTAL == _paid_in(line)


async def test_unpaid_withdrawal_has_no_refund(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    await _go(db_session, booking, renter(booking), BookingEvent.cancel, clock, notifier)

    line = await _line(db_session, booking)

    assert (line.deposit.status, line.rental.status) == ("cancelled", "cancelled")
    assert line.contribution.status == "cancelled"
    assert line.refund is None
    assert line.cancellation_fee is None


# ---------------------------------------------------------------------------
# Money that was not applied (I1): wrong amount, late, duplicate
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "webhook-secret-for-tests-that-is-long-enough"


async def _accepted_with_account(session, clock, notifier):
    """An accepted booking whose virtual account is open (R21); (booking, provider)."""
    provider = FakePaymentProvider(WEBHOOK_SECRET)
    booking = await _booking(session, BookingStatus.requested)
    await transition(
        session,
        booking.id,
        vendor(booking),
        BookingEvent.accept,
        None,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
        payment_provider=provider,
    )
    return booking, provider


async def _receive(session, provider, booking, amount, clock, notifier, event_id=None):
    body = provider.make_event(
        "payment_captured",
        booking_id=booking.id,
        payment_reference=f"fake-booking-{booking.id.hex}",
        amount=amount,
        provider_event_id=event_id,
    )
    result = await handle_webhook(
        session,
        provider,
        body,
        provider.sign(body),
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
    )
    await session.refresh(booking)
    return result


def _given_back(line) -> list[tuple[str, str]]:
    return [(item.amount, item.reason) for item in line.returned_payments or []]


async def test_wrong_amount_leaves_everything_due(db_session, clock, notifier) -> None:
    booking, provider = await _accepted_with_account(db_session, clock, notifier)
    short = TOTAL - D("1000.00")
    await _receive(db_session, provider, booking, short, clock, notifier)

    line = await _line(db_session, booking)

    assert booking.status == BookingStatus.accepted
    assert (line.deposit.status, line.rental.status) == ("due", "due")
    assert line.contribution.status == "due"
    assert line.next.label.startswith("Payment due ")
    assert line.next.amount_due == str(TOTAL)
    assert line.refund is None
    assert _given_back(line) == [(str(short), "amount_mismatch")]


async def test_late_payment_after_expiry_is_returned(db_session, clock, notifier) -> None:
    booking, provider = await _accepted_with_account(db_session, clock, notifier)
    clock.advance(timedelta(hours=25))
    await _go(db_session, booking, Actor.system(), BookingEvent.expire_deposit, clock, notifier)
    await _receive(db_session, provider, booking, TOTAL, clock, notifier)

    line = await _line(db_session, booking)

    assert booking.status == BookingStatus.expired
    assert (line.deposit.status, line.rental.status) == ("cancelled", "cancelled")
    assert line.contribution.status == "cancelled"
    assert line.refund is None
    assert line.cancellation_fee is None
    assert (line.next.label, line.next.amount_due) == ("Closed", "0.00")
    assert _given_back(line) == [(str(TOTAL), "late_payment")]


async def test_late_wrong_amount_after_withdrawal_is_returned(db_session, clock, notifier) -> None:
    booking, provider = await _accepted_with_account(db_session, clock, notifier)
    await _go(db_session, booking, renter(booking), BookingEvent.cancel, clock, notifier)
    await _receive(db_session, provider, booking, D("10.00"), clock, notifier)

    line = await _line(db_session, booking)

    assert booking.status == BookingStatus.cancelled_by_renter
    assert (line.deposit.status, line.rental.status) == ("cancelled", "cancelled")
    assert line.contribution.status == "cancelled"
    assert line.refund is None
    assert _given_back(line) == [("10.00", "amount_mismatch")]


async def test_duplicate_payment_is_returned_beside_the_paid_lines(
    db_session, clock, notifier
) -> None:
    booking, provider = await _accepted_with_account(db_session, clock, notifier)
    await _receive(db_session, provider, booking, TOTAL, clock, notifier)
    await _receive(db_session, provider, booking, D("5000.00"), clock, notifier, "evt_again")

    line = await _line(db_session, booking)

    assert booking.status == BookingStatus.confirmed
    assert (line.deposit.status, line.rental.status) == ("held", "received")
    assert line.contribution.status == "received"
    assert line.next.label == "Handover Fri 8 Jan"
    assert _given_back(line) == [("5000.00", "duplicate_payment")]
    assert "returned_payments" in line.model_dump()


async def test_no_returned_payments_part_when_nothing_was_returned(
    db_session, clock, notifier
) -> None:
    booking, provider = await _accepted_with_account(db_session, clock, notifier)
    await _receive(db_session, provider, booking, TOTAL, clock, notifier)

    line = await _line(db_session, booking)

    assert line.returned_payments is None
    assert "returned_payments" not in line.model_dump()
