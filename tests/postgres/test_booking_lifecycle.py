"""Live PostgreSQL tests for the rest of the booking state machine (Task 3.4).

Covers spec 3.1 rows ``confirmed + handover_completed`` (-> in_progress; the
renter paid everything upfront, A14), ``in_progress + return_completed``,
``returned + claim_raised``, ``returned + complete``, ``disputed + resolve``,
``in_progress + flag_at_risk`` and ``in_progress + mark_lost``.

The last part proves spec 3.1 as amended (A13-A16) as a whole: one
``test_row_<from>_<event>`` per table row, ``test_forbidden_transitions`` over every
(status, event) pair the table does not allow (one test per status, one SAVEPOINT
per pair), and a service-level happy path that checks the ledger after every step.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock, start_of_business_day
from app.core.config import get_settings
from app.db.models import Asset, AuditEvent, Booking, LedgerAccount, LedgerEntry, User
from app.db.models.asset import ListingStatus, PausedBy
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection, HandoverStatus
from app.db.models.payment import PaymentType
from app.db.models.user import RenterRole
from app.providers.payment import FakePaymentProvider
from app.schemas.errors import BookingGuardFailed, InvalidRequestError, InvalidTransition
from app.services import booking as booking_service
from app.services.audit import Actor
from app.services.booking import BookingEvent, request_booking, transition
from app.services.ledger import account_balance, post_txn
from app.services.money import payment_captured_postings
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
TOTAL = DEPOSIT + CONTRIBUTION + RENTAL + DELIVERY  # the one upfront payment (A14)
VENDOR_SHARE = RENTAL + DELIVERY - COMMISSION
# Accept opens the renter's virtual account through it (R21).
PROVIDER = FakePaymentProvider("test-webhook-secret")
START = date(2027, 1, 4)
# Ends 2026-10-29, so at risk from 2026-10-29 23:00 UTC: before DEFAULT_NOW.
OVERDUE_START = date(2026, 10, 27)


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
        payment_provider=PROVIDER,
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


async def _paid(session: AsyncSession, status: BookingStatus, **kwargs) -> Booking:
    """A booking in ``status`` whose one upfront payment (D+C+R+F) is on the ledger,
    with its captured ``booking`` payment row (refunds are recorded against it, R22)."""
    booking = await _booking(session, status, **kwargs)
    payment = await create_payment(session, booking, type=PaymentType.booking, amount=TOTAL)
    await post_txn(
        session,
        postings=payment_captured_postings(DEPOSIT, CONTRIBUTION, RENTAL, DELIVERY, COMMISSION),
        booking_id=booking.id,
        payment_id=payment.id,
        memo="payment_captured",
    )
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


async def _counts(session: AsyncSession) -> tuple[int, int]:
    ledger = await session.scalar(select(func.count()).select_from(LedgerEntry))
    audits = await session.scalar(select(func.count()).select_from(AuditEvent))
    return ledger, audits


# ---------------------------------------------------------------------------
# confirmed + handover_completed -> in_progress (everything was paid upfront, A14)
# ---------------------------------------------------------------------------


async def test_handover_alone_starts_rental(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    ledger_before = len(await _ledger(db_session, booking.id))

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.handover_completed, clock, notifier
    )

    assert result.status == BookingStatus.in_progress
    audit = (await _audits(db_session, booking.id))[-1]
    assert audit.action == "booking.in_progress"
    assert audit.actor_role == "renter"
    assert (audit.before["status"], audit.after["status"]) == ("confirmed", "in_progress")
    assert audit.after["handover"] == "pickup"
    # Nothing more to collect: the handover moves no money.
    assert len(await _ledger(db_session, booking.id)) == ledger_before
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == VENDOR_SHARE
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION
    assert {call.user_id for call in notifier.calls} == {booking.renter_id, booking.vendor_id}
    assert set(notifier.templates()) == {"booking.in_progress"}


@pytest.mark.parametrize("role", ["vendor", "system"])
async def test_vendor_or_system_may_record_handover(db_session, clock, notifier, role) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    actor = vendor_actor(booking) if role == "vendor" else Actor.system()

    result = await _transition(
        db_session, booking, actor, BookingEvent.handover_completed, clock, notifier
    )

    assert result.status == BookingStatus.in_progress
    assert (await _audits(db_session, booking.id))[-1].actor_role == role


@pytest.mark.parametrize("record", ["none", "draft", "return_only"])
async def test_handover_without_completed_pickup_is_refused(
    db_session, clock, notifier, record
) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
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
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.confirmed


async def test_second_handover_signal_is_invalid(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.handover_completed, clock, notifier
    )
    counts = await _counts(db_session)

    # The first signal started the rental; in_progress has no handover_completed row.
    with pytest.raises(InvalidTransition) as caught:
        await _transition(
            db_session,
            booking,
            vendor_actor(booking),
            BookingEvent.handover_completed,
            clock,
            notifier,
        )
    assert caught.value.error_details == {"from": "in_progress", "event": "handover_completed"}
    assert await _counts(db_session) == counts


async def test_stranger_cannot_record_handover(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    stranger = Actor(user_id=uuid.uuid4(), role=ActorRole.renter)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, stranger, BookingEvent.handover_completed, clock, notifier
        )


# ---------------------------------------------------------------------------
# confirmed + cancel: the whole upfront payment is reversed (less the renter's fee)
# ---------------------------------------------------------------------------


async def test_vendor_cancel_confirmed_refunds_everything(db_session, clock, notifier) -> None:
    status = BookingStatus.cancelled_by_vendor
    booking = await _paid(db_session, BookingStatus.confirmed)
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == VENDOR_SHARE

    result = await _transition(
        db_session,
        booking,
        vendor_actor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "No go"},
    )

    assert result.status == status
    for account in (
        LedgerAccount.vendor_payable,
        LedgerAccount.platform_revenue,
        LedgerAccount.renter_deposit_liability,
        LedgerAccount.protection_pool,
        LedgerAccount.platform_cash,
    ):
        assert await _balance(db_session, account, booking.id) == ZERO, account
    assert await _whole_ledger_balanced(db_session)
    refund_rows = [
        row for row in await _ledger(db_session, booking.id) if row.memo == "cancel_rental_refund"
    ]
    assert len(refund_rows) == 3
    assert len({row.txn_group for row in refund_rows}) == 1
    audit = [a for a in await _audits(db_session, booking.id) if a.action == f"booking.{status}"]
    assert audit[0].after["refund"] == {
        "deposit": str(DEPOSIT),
        "damage_contribution": str(CONTRIBUTION),
        "rental": str(RENTAL),
        "delivery_fee": str(DELIVERY),
        "commission": str(COMMISSION),
    }


@pytest.mark.parametrize("role", [ActorRole.renter, ActorRole.vendor])
async def test_cancel_after_pickup_handover_is_refused(db_session, clock, notifier, role) -> None:
    # Both signed the pickup, but handover_completed has not run yet: still confirmed.
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)
    actor = renter_actor(booking) if role == ActorRole.renter else vendor_actor(booking)
    counts = await _counts(db_session)
    penalties = len(await _audits(db_session, booking.vendor_id))

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, actor, BookingEvent.cancel, clock, notifier, {"reason": "No go"}
        )

    assert caught.value.code == "handover_signed"
    assert await _counts(db_session) == counts
    assert len(await _audits(db_session, booking.vendor_id)) == penalties
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.confirmed
    assert booking.cancelled_at is None
    assert booking.cancellation_reason is None
    assert notifier.calls == []


async def test_cancel_with_only_a_draft_pickup_is_allowed(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking, completed=False)

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.cancel, clock, notifier
    )

    assert result.status == BookingStatus.cancelled_by_renter


# ---------------------------------------------------------------------------
# confirmed + report_no_show -> cancelled_by_vendor (A17): full refund, penalty
# ---------------------------------------------------------------------------


def _no_show_from(booking: Booking) -> datetime:
    """00:00 (business zone) on ``starts_on``: the first instant a no-show counts."""
    return start_of_business_day(booking.starts_on, get_settings().business_zone)


async def _report_no_show(session, booking, clock, notifier, payload=None) -> Booking:
    return await _transition(
        session,
        booking,
        renter_actor(booking),
        BookingEvent.report_no_show,
        clock,
        notifier,
        payload,
    )


async def _assert_no_show_refused(session, booking, clock, notifier, code: str) -> None:
    counts = await _counts(session)
    penalties = len(await _audits(session, booking.vendor_id))

    with pytest.raises(BookingGuardFailed) as caught:
        await _report_no_show(session, booking, clock, notifier)

    assert caught.value.code == code
    assert await _counts(session) == counts
    assert len(await _audits(session, booking.vendor_id)) == penalties
    await session.refresh(booking)
    assert booking.status == BookingStatus.confirmed
    assert booking.cancelled_at is None
    assert booking.cancellation_reason is None
    assert notifier.calls == []


async def test_no_show_before_the_start_is_too_early(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    just_before = FrozenClock(_no_show_from(booking) - timedelta(microseconds=1))

    await _assert_no_show_refused(db_session, booking, just_before, notifier, "no_show_too_early")


async def test_no_show_at_local_midnight_on_the_start_day_is_allowed(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    opens = _no_show_from(booking)
    # 00:00 Africa/Lagos on 4 Jan is 23:00 UTC on 3 Jan.
    assert opens == datetime(2027, 1, 3, 23, 0, tzinfo=UTC)

    result = await _report_no_show(db_session, booking, FrozenClock(opens), notifier)

    assert result.status == BookingStatus.cancelled_by_vendor
    assert result.cancelled_at == opens


async def test_no_show_after_a_completed_pickup_is_refused(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)

    await _assert_no_show_refused(
        db_session, booking, FrozenClock(_no_show_from(booking)), notifier, "handover_signed"
    )


async def test_no_show_after_the_vendor_signed_the_pickup_is_refused(db_session, notifier) -> None:
    # The vendor signed its side; the renter has not (and may refuse to).
    booking = await _paid(db_session, BookingStatus.confirmed)
    record = await create_handover(db_session, booking, completed=False)
    record.status = HandoverStatus.vendor_signed
    record.vendor_signer_id = booking.vendor_id
    record.vendor_signed_at = DEFAULT_NOW
    await db_session.flush()

    await _assert_no_show_refused(
        db_session,
        booking,
        FrozenClock(_no_show_from(booking)),
        notifier,
        "vendor_signed_handover",
    )


async def test_no_show_too_early_wins_over_a_vendor_signed_pickup(db_session, notifier) -> None:
    # Two guards would refuse; the first one (too early) is the one reported.
    booking = await _paid(db_session, BookingStatus.confirmed)
    record = await create_handover(db_session, booking, completed=False)
    record.status = HandoverStatus.vendor_signed
    record.vendor_signer_id = booking.vendor_id
    record.vendor_signed_at = DEFAULT_NOW
    await db_session.flush()
    just_before = FrozenClock(_no_show_from(booking) - timedelta(microseconds=1))

    await _assert_no_show_refused(db_session, booking, just_before, notifier, "no_show_too_early")


async def test_no_show_with_only_a_draft_pickup_is_allowed(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking, completed=False)

    result = await _report_no_show(
        db_session, booking, FrozenClock(_no_show_from(booking)), notifier
    )

    assert result.status == BookingStatus.cancelled_by_vendor


async def test_no_show_refunds_everything_and_penalises_the_vendor(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    clock = FrozenClock(_no_show_from(booking) + timedelta(hours=10))
    last_id = await _last_audit_id(db_session)

    result = await transition(
        db_session,
        booking.id,
        renter_actor(booking),
        BookingEvent.report_no_show,
        {"reason": "Nobody at the address"},
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
        request_id="req_no_show",
    )

    assert result.status == BookingStatus.cancelled_by_vendor
    assert result.cancelled_at == clock.now()
    assert result.cancellation_reason == "vendor_no_show: Nobody at the address"
    for account in (
        LedgerAccount.vendor_payable,
        LedgerAccount.platform_revenue,
        LedgerAccount.renter_deposit_liability,
        LedgerAccount.protection_pool,
        LedgerAccount.platform_cash,
    ):
        assert await _balance(db_session, account, booking.id) == ZERO, account
    assert await _whole_ledger_balanced(db_session)
    # No fee: no cancellation_fee group.
    assert "cancellation_fee" not in {row.memo for row in await _ledger(db_session, booking.id)}

    rows = await _audits_after(db_session, last_id)
    assert [(row.entity, row.action) for row in rows] == [
        ("user", "vendor.reliability_penalty"),
        ("booking", "booking.cancelled_by_vendor"),
    ]
    assert {row.request_id for row in rows} == {"req_no_show"}
    penalty, cancelled = rows
    assert penalty.entity_id == booking.vendor_id
    assert penalty.reason == "vendor_no_show"
    assert penalty.after["booking_id"] == str(booking.id)
    assert cancelled.reason == "vendor_no_show: Nobody at the address"
    assert cancelled.after["refund"] == {
        "deposit": str(DEPOSIT),
        "damage_contribution": str(CONTRIBUTION),
        "rental": str(RENTAL),
        "delivery_fee": str(DELIVERY),
        "commission": str(COMMISSION),
    }
    assert "cancellation_fee" not in cancelled.after

    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (booking.renter_id, "booking.no_show_refunded"),
        (booking.vendor_id, "booking.no_show_reported"),
        (None, "ops.vendor_no_show"),
    ]
    assert notifier.calls[2].data["vendor_id"] == str(booking.vendor_id)


@pytest.mark.parametrize("role", [ActorRole.vendor, ActorRole.system, ActorRole.admin])
async def test_only_the_renter_reports_a_no_show(db_session, notifier, role) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    actors = {
        ActorRole.vendor: vendor_actor(booking),
        ActorRole.system: Actor.system(),
        ActorRole.admin: await admin_actor(db_session),
    }

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            actors[role],
            BookingEvent.report_no_show,
            FrozenClock(_no_show_from(booking)),
            notifier,
        )


async def test_another_renter_cannot_report_a_no_show(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    stranger = await create_user(db_session, kyc_verified=True)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session,
            booking,
            Actor(user_id=stranger.id, role=ActorRole.renter),
            BookingEvent.report_no_show,
            FrozenClock(_no_show_from(booking)),
            notifier,
        )


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
    booking = await _paid(db_session, BookingStatus.returned)
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
    booking = await _paid(db_session, BookingStatus.returned)
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
        {"amount": "1e40"},
        {"amount": "1234567890123"},  # 13 digits: beyond Numeric(12, 2)
        {"amount": "100.005"},  # more than 2 decimal places
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
    booking = await _paid(db_session, BookingStatus.returned)
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
    booking = await _paid(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - DEFAULT_NOW)
    counts = await _counts(db_session)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier
        )
    assert await _counts(db_session) == counts


async def test_complete_with_claim_is_invalid(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.returned, claimed=True)
    clock.advance(timedelta(days=3))

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier
        )


async def test_vendor_cannot_complete(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.returned)
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
    booking = await _paid(db_session, BookingStatus.disputed, claimed=True)
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
    assert (
        await _balance(db_session, LedgerAccount.vendor_payable, booking.id)
        == VENDOR_SHARE + vendor_gets
    )
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
    booking = await _paid(db_session, BookingStatus.disputed, claimed=True)
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

    assert (
        await _balance(db_session, LedgerAccount.vendor_payable, booking.id)
        == VENDOR_SHARE + DEPOSIT
    )
    assert await _balance(db_session, LedgerAccount.renter_deposit_liability, booking.id) == ZERO
    final = (await _audits(db_session, booking.id))[-1]
    assert final.after["amount"] == str(DEPOSIT)
    assert final.after["requested_amount"] == "900000.00"


@pytest.mark.parametrize("reason", [None, "", "   ", 5])
async def test_resolve_requires_reason(db_session, clock, notifier, reason) -> None:
    booking = await _paid(db_session, BookingStatus.disputed, claimed=True)
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
        {"decision": "deduct", "amount": "0.004"},  # more than 2 decimal places
        {"decision": "deduct", "amount": "100.005"},
        {"decision": "deduct", "amount": "1e40"},
        {"decision": "deduct", "amount": "1234567890123"},  # beyond Numeric(12, 2)
        {"decision": "deduct", "amount": 10.0},
        {"decision": "deduct", "amount": "abc"},
    ],
)
async def test_resolve_payload_is_validated(db_session, clock, notifier, payload) -> None:
    booking = await _paid(db_session, BookingStatus.disputed, claimed=True)
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
    booking = await _paid(db_session, BookingStatus.disputed, claimed=True)
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
    booking = await _paid(db_session, BookingStatus.in_progress, starts_on=OVERDUE_START)

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
    assert len(await _ledger(db_session, booking.id)) == 5  # only the upfront payment group


async def test_second_at_risk_flag_is_invalid(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress, starts_on=OVERDUE_START)
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
    booking = await _booking(db_session, BookingStatus.in_progress, starts_on=OVERDUE_START)
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


def _at_risk_from(booking: Booking) -> datetime:
    """start_of_business_day(ends_on + 1 day): the first at-risk instant (A15)."""
    zone = get_settings().business_zone
    return start_of_business_day(booking.ends_on + timedelta(days=1), zone)


async def test_flag_at_risk_before_the_cutoff_is_invalid(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.in_progress)
    early = FrozenClock(_at_risk_from(booking) - timedelta(seconds=1))
    counts = await _counts(db_session)

    with pytest.raises(InvalidTransition):
        await _transition(
            db_session, booking, Actor.system(), BookingEvent.flag_at_risk, early, notifier
        )

    assert await _counts(db_session) == counts
    await db_session.refresh(booking)
    assert booking.at_risk_at is None
    renter = await db_session.get(User, booking.renter_id)
    await db_session.refresh(renter)
    assert renter.restricted_at is None
    assert notifier.calls == []


async def test_flag_at_risk_at_the_cutoff_flags(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.in_progress)
    at = FrozenClock(_at_risk_from(booking))

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.flag_at_risk, at, notifier
    )

    assert result.at_risk_at == at.now()
    assert notifier.templates() == ["ops.at_risk"]


@pytest.mark.parametrize(
    ("instant", "allowed"),
    [
        (datetime(2027, 1, 6, 22, 59, 59, tzinfo=UTC), False),  # 23:59:59 WAT, last day
        (datetime(2027, 1, 6, 23, 0, 0, tzinfo=UTC), True),  # 00:00 WAT on the 7th
    ],
)
async def test_at_risk_at_end_of_last_day(db_session, notifier, instant, allowed) -> None:
    """ends_on 2027-01-06: at risk from Africa/Lagos midnight, 23:00Z on the 6th."""
    assert get_settings().business_timezone == "Africa/Lagos"
    booking = await _paid(db_session, BookingStatus.in_progress, ends_on=date(2027, 1, 6))

    async def flag() -> Booking:
        return await _transition(
            db_session,
            booking,
            Actor.system(),
            BookingEvent.flag_at_risk,
            FrozenClock(instant),
            notifier,
        )

    if allowed:
        assert (await flag()).at_risk_at == instant
    else:
        with pytest.raises(InvalidTransition):
            await flag()
        assert booking.at_risk_at is None


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
    booking = await _paid(db_session, BookingStatus.in_progress)
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
    assert (
        await _balance(db_session, LedgerAccount.vendor_payable, booking.id)
        == VENDOR_SHARE + DEPOSIT
    )
    renter = await db_session.get(User, booking.renter_id)
    await db_session.refresh(renter)
    assert renter.restricted_reason == "lost_item"
    assert renter.restricted_at == clock.now()
    asset = await db_session.get(Asset, booking.asset_id)
    await db_session.refresh(asset)
    assert asset.listing_status == ListingStatus.paused
    assert asset.paused_by == PausedBy.admin

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
    assert paused.after["paused_by"] == "admin"
    restricted = (await _audits(db_session, booking.renter_id))[-1]
    assert restricted.action == "user.restricted"
    assert restricted.after["restricted_reason"] == "lost_item"
    assert {call.user_id for call in notifier.calls} >= {booking.renter_id, booking.vendor_id}
    assert "ops.lost" in notifier.templates()


async def test_mark_lost_before_t_plus_7_409(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.in_progress)
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
    booking = await _paid(db_session, BookingStatus.in_progress)
    admin = await admin_actor(db_session)
    clock = FrozenClock(_lost_from(booking))
    counts = await _counts(db_session)

    with pytest.raises(BookingGuardFailed) as caught:
        await _transition(
            db_session, booking, admin, BookingEvent.mark_lost, clock, notifier, payload
        )

    assert caught.value.code == "reason_required"
    assert await _counts(db_session) == counts


@pytest.mark.parametrize(
    ("instant", "allowed"),
    [
        (datetime(2027, 1, 12, 22, 59, 59, tzinfo=UTC), False),  # 23:59:59 WAT on the 12th
        (datetime(2027, 1, 12, 23, 0, 0, tzinfo=UTC), True),  # 00:00 WAT on the 13th
    ],
)
async def test_mark_lost_boundary_absolute_instant(db_session, notifier, instant, allowed) -> None:
    """ends_on 2027-01-06 + 7 days = 13 Jan; Africa/Lagos midnight is 23:00Z on the 12th."""
    assert get_settings().business_timezone == "Africa/Lagos"
    booking = await _paid(db_session, BookingStatus.in_progress, ends_on=date(2027, 1, 6))
    assert booking.ends_on == date(2027, 1, 6)
    admin = await admin_actor(db_session)

    async def mark() -> Booking:
        return await _transition(
            db_session,
            booking,
            admin,
            BookingEvent.mark_lost,
            FrozenClock(instant),
            notifier,
            {"reason": "Unreachable"},
        )

    if allowed:
        assert (await mark()).status == BookingStatus.lost
    else:
        with pytest.raises(InvalidTransition):
            await mark()


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


async def _lost_with_asset_paused_by(db_session, notifier, paused_by: PausedBy):
    booking = await _paid(db_session, BookingStatus.in_progress)
    asset = await db_session.get(Asset, booking.asset_id)
    asset.listing_status = ListingStatus.paused
    asset.paused_by = paused_by
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
    await db_session.refresh(asset)
    return booking, asset


async def test_mark_lost_takes_over_a_vendor_pause(db_session, clock, notifier) -> None:
    # A vendor must never be able to resume lost gear, so the pause becomes an admin one.
    booking, asset = await _lost_with_asset_paused_by(db_session, notifier, PausedBy.vendor)

    assert asset.listing_status == ListingStatus.paused
    assert asset.paused_by == PausedBy.admin
    audits = await _audits(db_session, booking.asset_id)
    assert [a.action for a in audits] == ["asset.pause_taken_over"]
    assert audits[0].before == {"listing_status": "paused", "paused_by": "vendor"}
    assert audits[0].after == {
        "listing_status": "paused",
        "paused_by": "admin",
        "booking_id": str(booking.id),
    }
    assert audits[0].reason == "Gone"
    assert audits[0].actor_role == "admin"


async def test_mark_lost_on_admin_paused_asset_writes_no_pause_row(
    db_session, clock, notifier
) -> None:
    booking, asset = await _lost_with_asset_paused_by(db_session, notifier, PausedBy.admin)

    assert (asset.listing_status, asset.paused_by) == (ListingStatus.paused, PausedBy.admin)
    assert await _audits(db_session, booking.asset_id) == []


async def test_mark_lost_reads_the_locked_asset_not_a_stale_copy(
    db_session, clock, notifier
) -> None:
    booking = await _paid(db_session, BookingStatus.in_progress)
    asset = await db_session.get(Asset, booking.asset_id)
    assert asset.listing_status == ListingStatus.active  # the session's copy
    # Paused behind the session's back (as another transaction would).
    await db_session.execute(
        text("UPDATE assets SET listing_status = 'paused', paused_by = 'admin' WHERE id = :id"),
        {"id": asset.id},
    )
    admin = await admin_actor(db_session)

    await _transition(
        db_session,
        booking,
        admin,
        BookingEvent.mark_lost,
        FrozenClock(_lost_from(booking)),
        notifier,
        {"reason": "Gone"},
    )

    assert await _audits(db_session, booking.asset_id) == []


async def test_restrict_reads_the_locked_renter_not_a_stale_copy(
    db_session, clock, notifier
) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress, starts_on=OVERDUE_START)
    renter = await db_session.get(User, booking.renter_id)
    assert renter.restricted_at is None  # the session's copy
    earlier = DEFAULT_NOW - timedelta(days=30)
    # Restricted behind the session's back (as another transaction would).
    await db_session.execute(
        text(
            "UPDATE users SET restricted_at = :at, restricted_reason = 'lost_item' WHERE id = :id"
        ),
        {"at": earlier, "id": renter.id},
    )

    await _transition(
        db_session, booking, Actor.system(), BookingEvent.flag_at_risk, clock, notifier
    )

    assert (renter.restricted_at, renter.restricted_reason) == (earlier, "lost_item")
    assert await _audits(db_session, booking.renter_id) == []


# ---------------------------------------------------------------------------
# Spec 3.1 coverage: one test per table row
# ---------------------------------------------------------------------------


async def test_row_none_request(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)

    booking = await request_booking(
        db_session,
        renter=Actor(user_id=renter.id, role=ActorRole.renter),
        asset_id=asset.id,
        starts_on=START,
        ends_on=START + timedelta(days=2),
        fulfilment="pickup",
        damage_contribution=False,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
    )

    assert booking.status == BookingStatus.requested
    assert booking.hold_expires_at == DEFAULT_NOW + timedelta(hours=4)
    assert booking.price_snapshot["rental_total"] == "150000.00"
    assert [a.action for a in await _audits(db_session, booking.id)] == ["booking.requested"]
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (asset.vendor_id, "booking.requested")
    ]


async def test_row_requested_accept(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    result = await _transition(
        db_session, booking, vendor_actor(booking), BookingEvent.accept, clock, notifier
    )

    assert result.status == BookingStatus.accepted
    assert result.deposit_due_at == DEFAULT_NOW + timedelta(hours=12)
    assert (await _audits(db_session, booking.id))[-1].action == "booking.accepted"
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (booking.renter_id, "booking.accepted")
    ]


async def test_row_requested_decline(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    result = await _transition(
        db_session, booking, vendor_actor(booking), BookingEvent.decline, clock, notifier
    )

    assert result.status == BookingStatus.declined
    assert (await _audits(db_session, booking.id))[-1].action == "booking.declined"
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (booking.renter_id, "booking.declined")
    ]


async def test_row_requested_cancel_renter(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.requested)

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.cancel, clock, notifier
    )

    assert result.status == BookingStatus.cancelled_by_renter
    assert await _ledger(db_session, booking.id) == []
    assert (await _audits(db_session, booking.id))[-1].action == "booking.cancelled_by_renter"
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (booking.vendor_id, "booking.withdrawn")
    ]


async def test_row_requested_expire_hold(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.requested)
    clock.advance(booking.hold_expires_at - DEFAULT_NOW + timedelta(seconds=1))

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.expire_hold, clock, notifier
    )

    assert result.status == BookingStatus.expired
    assert (await _audits(db_session, booking.id))[-1].action == "booking.expired"
    assert notifier.templates() == ["booking.expired"]


async def test_row_accepted_payment_captured(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=TOTAL)

    result = await _transition(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": TOTAL, "payment_id": payment.id},
    )

    assert result.status == BookingStatus.confirmed
    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == DEPOSIT
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == VENDOR_SHARE
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION
    assert [a.action for a in (await _audits(db_session, booking.id))[-2:]] == [
        "booking.deposit_paid",
        "booking.confirmed",
    ]
    assert {c.user_id for c in notifier.calls} == {booking.renter_id, booking.vendor_id}


async def test_row_accepted_cancel_renter(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.cancel, clock, notifier
    )

    assert result.status == BookingStatus.cancelled_by_renter
    assert await _ledger(db_session, booking.id) == []
    assert (await _audits(db_session, booking.id))[-1].action == "booking.cancelled_by_renter"
    assert [(c.user_id, c.template) for c in notifier.calls] == [
        (booking.vendor_id, "booking.withdrawn")
    ]


async def test_row_accepted_cancel_vendor(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)

    result = await _transition(
        db_session,
        booking,
        vendor_actor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Body failed"},
    )

    assert result.status == BookingStatus.cancelled_by_vendor
    assert await _ledger(db_session, booking.id) == []
    penalty = (await _audits(db_session, booking.vendor_id))[-1]
    assert penalty.action == "vendor.reliability_penalty"


async def test_row_accepted_expire_deposit(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    clock.advance(booking.deposit_due_at - DEFAULT_NOW + timedelta(seconds=1))

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.expire_deposit, clock, notifier
    )

    assert result.status == BookingStatus.expired
    assert {c.user_id for c in notifier.calls} == {booking.renter_id, booking.vendor_id}


async def test_row_confirmed_cancel_renter(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.cancel, clock, notifier
    )

    assert result.status == BookingStatus.cancelled_by_renter
    for account in (LedgerAccount.renter_deposit_liability, LedgerAccount.protection_pool):
        assert await _balance(db_session, account, booking.id) == ZERO
    # The fee (10% of R) is split evenly between the vendor and Vouch (A13).
    fee_part = D("7500.00")
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == fee_part
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == fee_part
    assert (await _audits(db_session, booking.id))[-1].action == "booking.cancelled_by_renter"


async def test_row_confirmed_cancel_vendor(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    clock.advance(timedelta(days=90))  # any time, even after the start

    result = await _transition(
        db_session,
        booking,
        vendor_actor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Body failed"},
    )

    assert result.status == BookingStatus.cancelled_by_vendor
    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == ZERO
    penalty = (await _audits(db_session, booking.vendor_id))[-1]
    assert penalty.action == "vendor.reliability_penalty"


async def test_row_confirmed_handover_completed(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    await create_handover(db_session, booking)

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.handover_completed, clock, notifier
    )

    assert result.status == BookingStatus.in_progress
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == VENDOR_SHARE
    assert await _balance(db_session, LedgerAccount.platform_revenue, booking.id) == COMMISSION
    assert [a.action for a in (await _audits(db_session, booking.id))[-1:]] == [
        "booking.in_progress",
    ]


async def test_row_in_progress_return_completed(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress)
    await create_handover(db_session, booking, direction=HandoverDirection.return_)

    result = await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.return_completed, clock, notifier
    )

    assert result.status == BookingStatus.returned
    assert result.claim_window_ends_at == DEFAULT_NOW + timedelta(hours=24)
    assert (await _audits(db_session, booking.id))[-1].action == "booking.returned"


async def test_row_returned_claim_raised(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.returned)

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
    assert result.claimed_at == DEFAULT_NOW
    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == DEPOSIT  # frozen
    assert (await _audits(db_session, booking.id))[-1].action == "booking.disputed"
    assert notifier.templates() == ["ops.claim_raised"]


async def test_row_returned_complete(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.returned)
    clock.advance(booking.claim_window_ends_at - DEFAULT_NOW + timedelta(seconds=1))

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.complete, clock, notifier
    )

    assert result.status == BookingStatus.completed
    liability = LedgerAccount.renter_deposit_liability
    assert await _balance(db_session, liability, booking.id) == ZERO
    assert {c.user_id for c in notifier.calls} == {booking.renter_id, booking.vendor_id}


async def test_row_disputed_resolve(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.disputed, claimed=True)

    result = await _transition(
        db_session,
        booking,
        await admin_actor(db_session),
        BookingEvent.resolve,
        clock,
        notifier,
        {"decision": "deduct", "amount": "20000.00", "reason": "Scratch confirmed"},
    )

    assert result.status == BookingStatus.completed
    assert await _balance(db_session, LedgerAccount.vendor_payable, booking.id) == VENDOR_SHARE + D(
        "20000.00"
    )
    final = (await _audits(db_session, booking.id))[-1]
    assert (final.action, final.reason) == ("booking.completed", "Scratch confirmed")


async def test_row_in_progress_flag_at_risk(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.in_progress, starts_on=OVERDUE_START)

    result = await _transition(
        db_session, booking, Actor.system(), BookingEvent.flag_at_risk, clock, notifier
    )

    assert result.status == BookingStatus.in_progress
    assert result.at_risk_at == DEFAULT_NOW
    renter = await db_session.get(User, booking.renter_id)
    assert renter.restricted_reason == "at_risk"
    assert notifier.templates() == ["ops.at_risk"]


async def test_row_in_progress_mark_lost(db_session, clock, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.in_progress)

    result = await _transition(
        db_session,
        booking,
        await admin_actor(db_session),
        BookingEvent.mark_lost,
        FrozenClock(_lost_from(booking)),
        notifier,
        {"reason": "Unreachable"},
    )

    assert result.status == BookingStatus.lost
    assert (
        await _balance(db_session, LedgerAccount.vendor_payable, booking.id)
        == VENDOR_SHARE + DEPOSIT
    )
    renter = await db_session.get(User, booking.renter_id)
    assert renter.restricted_reason == "lost_item"
    asset = await db_session.get(Asset, booking.asset_id)
    assert asset.listing_status == ListingStatus.paused


async def test_row_confirmed_report_no_show(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)

    result = await _transition(
        db_session,
        booking,
        renter_actor(booking),
        BookingEvent.report_no_show,
        FrozenClock(_no_show_from(booking)),
        notifier,
    )

    assert result.status == BookingStatus.cancelled_by_vendor
    assert result.cancellation_reason == "vendor_no_show"
    assert (await _audits(db_session, booking.id))[-1].action == "booking.cancelled_by_vendor"
    penalty = (await _audits(db_session, booking.vendor_id))[-1]
    assert (penalty.action, penalty.reason) == ("vendor.reliability_penalty", "vendor_no_show")


# ---------------------------------------------------------------------------
# Spec 3.1 coverage: every pair the table does not allow is refused
# ---------------------------------------------------------------------------

S, E = BookingStatus, BookingEvent

# Written out by hand from spec 3.1 as amended (A3 vendor approval; A13/R14 free
# withdrawal; A14 one upfront payment, so no balance step; A17 vendor no-show), so a registry bug
# cannot silently shrink the forbidden list below.
EXPECTED_ALLOWED = frozenset(
    {
        (S.requested, E.accept),
        (S.requested, E.decline),
        (S.requested, E.approve_vendor),
        (S.requested, E.expire_hold),
        (S.requested, E.cancel),
        (S.accepted, E.payment_captured),
        (S.accepted, E.expire_deposit),
        (S.accepted, E.cancel),
        (S.confirmed, E.cancel),
        (S.confirmed, E.handover_completed),
        (S.confirmed, E.report_no_show),
        (S.in_progress, E.return_completed),
        (S.in_progress, E.flag_at_risk),
        (S.in_progress, E.mark_lost),
        (S.returned, E.claim_raised),
        (S.returned, E.complete),
        (S.disputed, E.resolve),
    }
)

FORBIDDEN = [
    (status, event)
    for status in BookingStatus
    for event in BookingEvent
    if (status, event) not in booking_service._TRANSITIONS
]

# For each event, a role its own row accepts (so only the missing row refuses it).
EVENT_ROLE = {
    E.accept: ActorRole.vendor,
    E.decline: ActorRole.vendor,
    E.approve_vendor: ActorRole.vendor,
    E.expire_hold: ActorRole.system,
    E.payment_captured: ActorRole.system,
    E.expire_deposit: ActorRole.system,
    E.cancel: ActorRole.renter,
    E.handover_completed: ActorRole.renter,
    E.return_completed: ActorRole.renter,
    E.claim_raised: ActorRole.vendor,
    E.complete: ActorRole.system,
    E.resolve: ActorRole.admin,
    E.flag_at_risk: ActorRole.system,
    E.mark_lost: ActorRole.admin,
    E.report_no_show: ActorRole.renter,
}


def test_registry_is_exactly_the_spec_table() -> None:
    assert set(booking_service._TRANSITIONS) == EXPECTED_ALLOWED
    assert len(FORBIDDEN) == len(BookingStatus) * len(BookingEvent) - len(EXPECTED_ALLOWED)
    assert len(FORBIDDEN) == 178  # 13 statuses x 15 events - 17 rows
    for (_status, event), rule in booking_service._TRANSITIONS.items():
        assert EVENT_ROLE[event] in rule.roles


@pytest.mark.parametrize("status", list(BookingStatus), ids=lambda status: status.value)
async def test_forbidden_transitions(db_session, notifier, status) -> None:
    """Every (status, event) pair outside the table raises InvalidTransition.

    One booking per status (one ``db_session`` setup), each forbidden event tried
    inside its own SAVEPOINT. The booking is given every fact a guard could ask for
    (completed pickup and return records, a captured upfront payment, a long-past
    clock),
    so only the missing table row can refuse the event.
    """
    events = [event for (s, event) in FORBIDDEN if s == status]
    allowed_here = [pair for pair in EXPECTED_ALLOWED if pair[0] == status]
    assert len(events) == len(BookingEvent) - len(allowed_here)

    booking = await _paid(db_session, status, claimed=status == BookingStatus.disputed)
    await create_handover(db_session, booking)
    await create_handover(db_session, booking, direction=HandoverDirection.return_)
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=TOTAL)
    admin = await create_user(db_session, role=RenterRole.admin)
    actors = {
        ActorRole.renter: renter_actor(booking),
        ActorRole.vendor: vendor_actor(booking),
        ActorRole.admin: Actor(user_id=admin.id, role=ActorRole.admin),
        ActorRole.system: Actor.system(),
    }
    payload = {
        **CLAIM,
        "amount": TOTAL,
        "payment_id": payment.id,
        "decision": "release",
        "reason": "Forbidden-pair check",
    }
    late = FrozenClock(DEFAULT_NOW + timedelta(days=400))

    tried = []
    for event in events:
        counts = await _counts(db_session)
        savepoint = await db_session.begin_nested()
        # Everything is checked INSIDE the savepoint, before it is rolled back, so a
        # row written before the raise would still be visible here.
        with pytest.raises(InvalidTransition) as caught:
            await _transition(
                db_session, booking, actors[EVENT_ROLE[event]], event, late, notifier, payload
            )
        assert caught.value.error_details == {"from": status.value, "event": event.value}
        assert booking.status == status, (status, event)  # in memory, before any refresh
        await db_session.flush()
        assert await _counts(db_session) == counts, (status, event)
        await db_session.refresh(booking)
        assert booking.status == status, (status, event)
        await savepoint.rollback()
        tried.append((status, event))

    assert tried == [pair for pair in FORBIDDEN if pair[0] == status]
    assert notifier.calls == []


# ---------------------------------------------------------------------------
# Spec 3.1 coverage: the full happy path at the service level
# ---------------------------------------------------------------------------


async def _whole_ledger_balanced(session: AsyncSession) -> bool:
    result = await session.execute(
        select(LedgerEntry.direction, func.sum(LedgerEntry.amount)).group_by(LedgerEntry.direction)
    )
    totals = dict(result.all())
    return totals.get("dr", ZERO) == totals.get("cr", ZERO)


async def test_happy_path_ledger_at_every_step(db_session, clock, notifier) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(
        db_session,
        daily_rate=D("50000.00"),
        deposit_amount=DEPOSIT,
        fulfilment={"pickup": True, "delivery": True, "delivery_fee": "7500.00"},
    )

    booking = await request_booking(
        db_session,
        renter=Actor(user_id=renter.id, role=ActorRole.renter),
        asset_id=asset.id,
        starts_on=START,
        ends_on=START + timedelta(days=2),
        fulfilment="delivery",
        damage_contribution=True,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
    )
    snapshot = booking.price_snapshot
    rental = D(snapshot["rental_total"])
    delivery = D(snapshot["delivery_fee"])
    deposit = D(snapshot["deposit"])
    contribution = D(snapshot["damage_contribution"])
    commission = (rental * D(snapshot["commission_rate"])).quantize(
        D("0.01"), rounding=ROUND_HALF_UP
    )
    assert (rental, delivery, deposit) == (RENTAL, DELIVERY, DEPOSIT)
    assert contribution > ZERO
    assert commission > ZERO
    vendor_share = rental + delivery - commission

    async def check(status, liability, vendor, revenue) -> None:
        await db_session.refresh(booking)
        assert booking.status == status
        bid = booking.id
        assert await _balance(db_session, LedgerAccount.renter_deposit_liability, bid) == liability
        assert await _balance(db_session, LedgerAccount.vendor_payable, bid) == vendor
        assert await _balance(db_session, LedgerAccount.platform_revenue, bid) == revenue
        assert await _whole_ledger_balanced(db_session), status

    await check(BookingStatus.requested, ZERO, ZERO, ZERO)

    await _transition(db_session, booking, vendor_actor(booking), E.accept, clock, notifier)
    await check(BookingStatus.accepted, ZERO, ZERO, ZERO)

    total = deposit + contribution + rental + delivery
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=total)
    await _transition(
        db_session,
        booking,
        Actor.system(),
        E.payment_captured,
        clock,
        notifier,
        {"amount": total, "payment_id": payment.id},
    )
    await check(BookingStatus.confirmed, deposit, vendor_share, commission)
    assert await _balance(db_session, LedgerAccount.protection_pool, booking.id) == contribution
    assert -(await _balance(db_session, LedgerAccount.platform_cash, booking.id)) == total

    await create_handover(db_session, booking)
    await _transition(
        db_session, booking, renter_actor(booking), E.handover_completed, clock, notifier
    )
    await check(BookingStatus.in_progress, deposit, vendor_share, commission)

    await create_handover(db_session, booking, direction=HandoverDirection.return_)
    await _transition(
        db_session, booking, vendor_actor(booking), E.return_completed, clock, notifier
    )
    await check(BookingStatus.returned, deposit, vendor_share, commission)

    clock.advance(timedelta(hours=24, seconds=1))
    await _transition(db_session, booking, Actor.system(), E.complete, clock, notifier)
    await check(BookingStatus.completed, ZERO, vendor_share, commission)

    # The deposit went back; the cash kept is C + R + F, split between the vendor's
    # share, the platform's commission and the protection pool.
    kept = -(await _balance(db_session, LedgerAccount.platform_cash, booking.id))
    assert kept == contribution + rental + delivery
    assert kept == vendor_share + commission + contribution
    transitions = [
        ((a.before or {}).get("status"), a.after["status"])
        for a in await _audits(db_session, booking.id)
        if a.action.startswith("booking.")
    ]
    assert transitions == [
        (None, "requested"),
        ("requested", "accepted"),
        ("accepted", "deposit_paid"),
        ("deposit_paid", "confirmed"),
        ("confirmed", "in_progress"),
        ("in_progress", "returned"),
        ("returned", "completed"),
    ]


# ---------------------------------------------------------------------------
# request_id (R16): every audit row a transition writes carries the caller's id
# ---------------------------------------------------------------------------


async def _last_audit_id(session: AsyncSession) -> int:
    return await session.scalar(select(func.coalesce(func.max(AuditEvent.id), 0)))


async def _audits_after(session: AsyncSession, last_id: int) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.id > last_id).order_by(AuditEvent.id)
    )
    return list(result.scalars())


async def _transition_as_request(session, booking, actor, event, clock, notifier, payload):
    return await transition(
        session,
        booking.id,
        actor,
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=get_settings(),
        request_id="req_test",
        payment_provider=PROVIDER,
    )


async def test_mark_lost_writes_request_id_on_every_audit_row(db_session, notifier) -> None:
    booking = await _paid(db_session, BookingStatus.in_progress)
    admin = await admin_actor(db_session)
    last_id = await _last_audit_id(db_session)

    await _transition_as_request(
        db_session,
        booking,
        admin,
        BookingEvent.mark_lost,
        FrozenClock(_lost_from(booking)),
        notifier,
        {"reason": "Unreachable"},
    )

    rows = await _audits_after(db_session, last_id)
    assert [row.action for row in rows] == [
        "deposit.forfeited",
        "user.restricted",
        "asset.paused",
        "booking.lost",
    ]
    assert {row.request_id for row in rows} == {"req_test"}


async def test_payment_captured_writes_request_id_on_both_rows(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    payment = await create_payment(db_session, booking, type=PaymentType.booking, amount=TOTAL)
    last_id = await _last_audit_id(db_session)

    await _transition_as_request(
        db_session,
        booking,
        Actor.system(),
        BookingEvent.payment_captured,
        clock,
        notifier,
        {"amount": TOTAL, "payment_id": payment.id},
    )

    rows = await _audits_after(db_session, last_id)
    assert [row.action for row in rows] == ["booking.deposit_paid", "booking.confirmed"]
    assert {row.request_id for row in rows} == {"req_test"}


async def test_vendor_cancel_writes_request_id_on_the_penalty_row(
    db_session, clock, notifier
) -> None:
    booking = await _paid(db_session, BookingStatus.confirmed)
    last_id = await _last_audit_id(db_session)

    await _transition_as_request(
        db_session,
        booking,
        vendor_actor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "Lens is broken"},
    )

    rows = await _audits_after(db_session, last_id)
    assert [row.action for row in rows] == [
        "vendor.reliability_penalty",
        "booking.cancelled_by_vendor",
    ]
    assert {row.request_id for row in rows} == {"req_test"}


async def test_transition_without_request_id_stores_none(db_session, clock, notifier) -> None:
    booking = await _booking(db_session, BookingStatus.accepted)
    last_id = await _last_audit_id(db_session)

    await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.cancel, clock, notifier
    )

    rows = await _audits_after(db_session, last_id)
    assert [row.request_id for row in rows] == [None]
