"""Live PostgreSQL tests for the timer job (spec 3.2, Task 3.5).

The timer owns its transactions (R13), so these tests use ``session_factory`` on a
fresh ``head`` schema: every setup session really commits, and so does every run.
Each booking gets its own asset and explicit dates (factory default dates are
counted per session).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock, start_of_business_day
from app.core.config import get_settings
from app.db.models import AuditEvent, Booking, LedgerAccount, LedgerEntry, User
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.jobs import timers as timers_cli
from app.schemas.errors import BookingGuardFailed, InvalidTransition
from app.services import timers as timers_module
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.ledger import account_balance, post_txn
from app.services.money import deposit_captured_postings
from app.services.notify import RecordingNotifier
from app.services.timers import TimerReport, run_timers
from tests.postgres.factories import (
    DEFAULT_NOW,
    create_asset,
    create_booking,
    create_user,
    money_snapshot,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

DEPOSIT = Decimal("150000.00")
ZERO = Decimal("0.00")
# The factory sets hold_expires_at / deposit_due_at to DEFAULT_NOW + 24h and
# claim_window_ends_at to DEFAULT_NOW + 48h.
HOLD_END = DEFAULT_NOW + timedelta(hours=24)
CLAIM_END = DEFAULT_NOW + timedelta(hours=48)
LATER = DEFAULT_NOW + timedelta(hours=49)
# An in_progress booking that ended long before LATER (at risk at LATER).
PAST_START = date(2026, 10, 20)
PAST_END = date(2026, 10, 22)

Factory = async_sessionmaker[AsyncSession]


async def _booking(
    session: AsyncSession,
    status: BookingStatus,
    *,
    starts_on: date = date(2027, 1, 4),
    ends_on: date = date(2027, 1, 6),
    claimed: bool = False,
    with_deposit: bool = False,
) -> Booking:
    """A booking of its own asset, with a money snapshot (and the deposit posted)."""
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session, deposit_amount=DEPOSIT)
    booking = await create_booking(
        session,
        asset=asset,
        renter=renter,
        status=status,
        starts_on=starts_on,
        ends_on=ends_on,
        deposit_amount=DEPOSIT,
        price_snapshot=money_snapshot(deposit=str(DEPOSIT)),
        claimed=claimed,
    )
    if with_deposit:
        await post_txn(
            session,
            postings=deposit_captured_postings(DEPOSIT, ZERO),
            booking_id=booking.id,
            memo="deposit_captured",
        )
    return booking


async def _make(factory: Factory, status: BookingStatus, **kwargs) -> Booking:
    async with factory() as session, session.begin():
        return await _booking(session, status, **kwargs)


async def _run(factory: Factory, at, notifier=None) -> TimerReport:
    return await run_timers(
        factory,
        clock=FrozenClock(at),
        notifier=notifier or RecordingNotifier(),
        settings=get_settings(),
    )


async def _fetch(factory: Factory, booking_id: uuid.UUID) -> Booking:
    async with factory() as session:
        return await session.get(Booking, booking_id)


async def _actions(factory: Factory, entity_id: uuid.UUID) -> list[str]:
    async with factory() as session:
        result = await session.execute(
            select(AuditEvent.action)
            .where(AuditEvent.entity_id == entity_id)
            .order_by(AuditEvent.id)
        )
        return list(result.scalars())


async def _counts(factory: Factory) -> tuple[int, int]:
    async with factory() as session:
        ledger = await session.scalar(select(func.count()).select_from(LedgerEntry))
        audits = await session.scalar(select(func.count()).select_from(AuditEvent))
        return ledger, audits


def _vendor(booking: Booking) -> Actor:
    return Actor(user_id=booking.vendor_id, role=ActorRole.vendor)


# ---------------------------------------------------------------------------
# One booking of each kind
# ---------------------------------------------------------------------------


async def test_timer_expires_hold_and_deposit_and_completes(session_factory) -> None:
    held = await _make(session_factory, BookingStatus.requested)
    unpaid = await _make(session_factory, BookingStatus.accepted)
    returned = await _make(session_factory, BookingStatus.returned, with_deposit=True)
    overdue = await _make(
        session_factory,
        BookingStatus.in_progress,
        starts_on=PAST_START,
        ends_on=PAST_END,
        with_deposit=True,
    )
    notifier = RecordingNotifier()

    report = await _run(session_factory, LATER, notifier)

    assert report == TimerReport(
        expired_holds=1, expired_deposits=1, completed=1, flagged_at_risk=1
    )
    assert (await _fetch(session_factory, held.id)).status == BookingStatus.expired
    assert (await _fetch(session_factory, unpaid.id)).status == BookingStatus.expired
    assert (await _fetch(session_factory, returned.id)).status == BookingStatus.completed
    flagged = await _fetch(session_factory, overdue.id)
    assert flagged.status == BookingStatus.in_progress
    assert flagged.at_risk_at == LATER
    async with session_factory() as session:
        liability = await account_balance(
            session, LedgerAccount.renter_deposit_liability, booking_id=returned.id
        )
    assert liability == ZERO
    assert (await _actions(session_factory, held.id))[-1] == "booking.expired"
    assert (await _actions(session_factory, unpaid.id))[-1] == "booking.expired"
    assert (await _actions(session_factory, returned.id))[-2:] == [
        "deposit.released",
        "booking.completed",
    ]
    assert (await _actions(session_factory, overdue.id))[-1] == "booking.at_risk"
    assert "ops.at_risk" in notifier.templates()


async def test_timer_leaves_bookings_not_yet_due(session_factory) -> None:
    held = await _make(session_factory, BookingStatus.requested)
    returned = await _make(session_factory, BookingStatus.returned, with_deposit=True)

    # Exactly at the deadlines: the guards are strict (now > deadline).
    assert await _run(session_factory, HOLD_END) == TimerReport(0, 0, 0, 0)
    assert await _run(session_factory, CLAIM_END) == TimerReport(1, 0, 0, 0)
    assert (await _fetch(session_factory, returned.id)).status == BookingStatus.returned
    assert (await _fetch(session_factory, held.id)).status == BookingStatus.expired


async def test_timer_idempotent_twice(session_factory) -> None:
    await _make(session_factory, BookingStatus.requested)
    await _make(session_factory, BookingStatus.accepted)
    await _make(session_factory, BookingStatus.returned, with_deposit=True)
    await _make(
        session_factory,
        BookingStatus.in_progress,
        starts_on=PAST_START,
        ends_on=PAST_END,
        with_deposit=True,
    )

    first = await _run(session_factory, LATER)
    counts = await _counts(session_factory)
    second = await _run(session_factory, LATER)

    assert first == TimerReport(1, 1, 1, 1)
    assert second == TimerReport(0, 0, 0, 0)
    assert await _counts(session_factory) == counts


async def test_timer_skips_claimed_returned_booking(session_factory) -> None:
    claimed = await _make(session_factory, BookingStatus.returned, claimed=True, with_deposit=True)

    report = await _run(session_factory, LATER)

    assert report.completed == 0
    assert (await _fetch(session_factory, claimed.id)).status == BookingStatus.returned


# ---------------------------------------------------------------------------
# Racing API traffic
# ---------------------------------------------------------------------------


async def test_timer_and_accept_race_one_wins(session_factory) -> None:
    settings = get_settings()

    # Ordering 1: the vendor's accept holds the row lock when the timer runs.
    accepted = await _make(session_factory, BookingStatus.requested)
    async with session_factory() as vendor_session:
        async with vendor_session.begin():
            await vendor_session.execute(
                select(Booking.id).where(Booking.id == accepted.id).with_for_update()
            )
            # The timer runs while the lock is held: SKIP LOCKED passes over it.
            during = await _run(session_factory, HOLD_END + timedelta(seconds=1))
            assert during.expired_holds == 0
            await transition(
                vendor_session,
                accepted.id,
                _vendor(accepted),
                BookingEvent.accept,
                clock=FrozenClock(HOLD_END - timedelta(seconds=1)),
                notifier=RecordingNotifier(),
                settings=settings,
            )
    after = await _run(session_factory, HOLD_END + timedelta(seconds=2))
    assert after.expired_holds == 0
    assert (await _fetch(session_factory, accepted.id)).status == BookingStatus.accepted
    actions = await _actions(session_factory, accepted.id)
    assert actions.count("booking.accepted") == 1
    assert "booking.expired" not in actions

    # Ordering 2: the timer gets there first; the late accept is refused.
    expired = await _make(
        session_factory,
        BookingStatus.requested,
        starts_on=date(2027, 2, 1),
        ends_on=date(2027, 2, 3),
    )
    first = await _run(session_factory, HOLD_END + timedelta(seconds=1))
    assert first.expired_holds == 1
    with pytest.raises(InvalidTransition):
        async with session_factory() as vendor_session, vendor_session.begin():
            await transition(
                vendor_session,
                expired.id,
                _vendor(expired),
                BookingEvent.accept,
                clock=FrozenClock(HOLD_END - timedelta(seconds=1)),
                notifier=RecordingNotifier(),
                settings=settings,
            )
    assert (await _fetch(session_factory, expired.id)).status == BookingStatus.expired
    actions = await _actions(session_factory, expired.id)
    assert actions.count("booking.expired") == 1
    assert "booking.accepted" not in actions


# ---------------------------------------------------------------------------
# At risk
# ---------------------------------------------------------------------------


async def test_timer_flags_at_risk_once(session_factory) -> None:
    overdue = await _make(
        session_factory,
        BookingStatus.in_progress,
        starts_on=PAST_START,
        ends_on=PAST_END,
        with_deposit=True,
    )

    first = await _run(session_factory, LATER)
    second = await _run(session_factory, LATER + timedelta(hours=1))

    assert first.flagged_at_risk == 1
    assert second.flagged_at_risk == 0
    assert (await _fetch(session_factory, overdue.id)).at_risk_at == LATER
    assert (await _actions(session_factory, overdue.id)).count("booking.at_risk") == 1
    async with session_factory() as session:
        renter = await session.get(User, overdue.renter_id)
    assert renter.restricted_at == LATER
    assert renter.restricted_reason == "at_risk"


async def test_timer_at_risk_boundary(session_factory) -> None:
    ends_on = date(2026, 10, 30)
    zone = get_settings().business_zone
    cutoff = start_of_business_day(ends_on + timedelta(days=1), zone) + timedelta(hours=48)
    overdue = await _make(
        session_factory,
        BookingStatus.in_progress,
        starts_on=date(2026, 10, 28),
        ends_on=ends_on,
        with_deposit=True,
    )

    before = await _run(session_factory, cutoff - timedelta(seconds=1))
    assert before.flagged_at_risk == 0
    assert (await _fetch(session_factory, overdue.id)).at_risk_at is None

    at = await _run(session_factory, cutoff)
    assert at.flagged_at_risk == 1
    assert (await _fetch(session_factory, overdue.id)).at_risk_at == cutoff


async def test_timer_does_not_flag_returned_on_time(session_factory) -> None:
    # A booking that is still in progress but not yet past its return day + 48h.
    current = await _make(
        session_factory,
        BookingStatus.in_progress,
        starts_on=date(2026, 11, 1),
        ends_on=date(2026, 11, 3),
    )

    report = await _run(session_factory, LATER)

    assert report.flagged_at_risk == 0
    assert (await _fetch(session_factory, current.id)).at_risk_at is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_timer_logs_and_skips_refused_transitions(
    session_factory, monkeypatch, vouch_caplog
) -> None:
    first = await _make(session_factory, BookingStatus.requested)
    second = await _make(
        session_factory,
        BookingStatus.requested,
        starts_on=date(2027, 2, 1),
        ends_on=date(2027, 2, 3),
    )
    real = timers_module.transition
    calls: list[uuid.UUID] = []

    async def flaky(session, booking_id, *args, **kwargs):
        calls.append(booking_id)
        if len(calls) == 1:
            raise InvalidTransition("requested", "expire_hold")
        if len(calls) == 2:
            raise BookingGuardFailed("hold_expired")
        return await real(session, booking_id, *args, **kwargs)

    monkeypatch.setattr(timers_module, "transition", flaky)
    report = await _run(session_factory, LATER)

    assert report.expired_holds == 0
    assert set(calls) == {first.id, second.id}
    skipped = [r for r in vouch_caplog.records if "timer_transition_skipped" in r.getMessage()]
    assert [r.levelno for r in skipped] == [logging.WARNING, logging.WARNING]
    # Nothing was written for either; the next run expires both.
    assert (await _fetch(session_factory, first.id)).status == BookingStatus.requested
    report = await _run(session_factory, LATER)
    assert report.expired_holds == 2


async def test_timer_propagates_unexpected_errors(session_factory, monkeypatch) -> None:
    await _make(session_factory, BookingStatus.requested)

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(timers_module, "transition", broken)
    with pytest.raises(RuntimeError, match="boom"):
        await _run(session_factory, LATER)


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_once_exits_zero(migrated_db, vouch_caplog, monkeypatch) -> None:
    # main() reconfigures the root logger, which would drop caplog's handler.
    monkeypatch.setattr(timers_cli, "configure_logging", lambda debug=False: None)

    assert timers_cli.main(["--once"]) == 0
    assert "timer_run expired_holds=0" in vouch_caplog.text


async def test_cli_loop_stops_on_sigterm(migrated_db, vouch_caplog) -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)

    code = await asyncio.wait_for(timers_cli.serve(get_settings(), once=False), 10)

    assert code == 0
    assert "timer_run" in vouch_caplog.text
    assert "timer_stopped" in vouch_caplog.text
    # The handlers are removed again: SIGTERM is back to its default.
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


async def test_cli_loop_runs_again_after_interval(migrated_db, vouch_caplog) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, stop.set)

    code = await asyncio.wait_for(
        timers_cli.serve(get_settings(), once=False, interval=0.1, stop=stop), 10
    )

    assert code == 0
    assert vouch_caplog.text.count("timer_run ") >= 2
