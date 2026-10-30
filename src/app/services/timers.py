"""The every-minute timer job (spec 3.2).

One run of :func:`run_timers` performs four sweeps, in this order:

* expire holds: ``requested`` bookings whose ``hold_expires_at`` has passed;
* expire deposit windows: ``accepted`` bookings whose ``deposit_due_at`` has passed;
* complete: ``returned`` bookings whose claim window has closed with no claim
  (the transition releases the deposit);
* flag at risk: ``in_progress`` bookings not yet flagged whose return day ended at
  least 48 hours ago. The spec says "T+48h no contact"; M2 has no contact signal,
  so "no contact" is read as "not returned": a booking is at risk from
  ``start_of_business_day(ends_on + 1 day) + 48h``.

Every action is an ordinary :func:`app.services.booking.transition` sent as
``Actor.system()``. The run reads the clock once; that instant judges every
candidate and is the ``now`` every transition sees.

Concurrency and idempotency
---------------------------
Candidates are listed with a plain read, then each is processed in its own
transaction (R13: this module is the one place that commits). Inside it the
booking is re-selected by id AND by the sweep's predicate with
``FOR UPDATE SKIP LOCKED``. A booking locked by API traffic, or one that has
already moved on (another run, a vendor accept), returns no row and is skipped
without being counted. The predicates are the transition guards themselves
(strict ``now > deadline``), so a second run over the same data does nothing.

An :class:`InvalidTransition` or :class:`BookingGuardFailed` for one booking is
logged and skipped, and its transaction rolls back; the run goes on. Any other
error propagates (earlier bookings stay committed).

Known limitation (M2)
---------------------
The engine sends notifications after its flush and before this module commits.
If a per-booking commit then fails, the notifications already went out for a
change that never landed (orphaned), and the next run sends them again (at
least once). A transactional outbox is out of scope for M2.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, FrozenClock, business_date
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Booking
from app.db.models.booking import BookingStatus
from app.schemas.errors import BookingGuardFailed, InvalidTransition
from app.services.audit import Actor
from app.services.booking import BookingEvent, transition
from app.services.notify import Notifier

AT_RISK_AFTER = timedelta(hours=48)

logger = get_logger("timers")


@dataclass(frozen=True)
class TimerReport:
    """How many bookings each sweep of one run moved."""

    expired_holds: int = 0
    expired_deposits: int = 0
    completed: int = 0
    flagged_at_risk: int = 0


def at_risk_last_end_date(now: datetime, settings: Settings) -> date:
    """The latest ``ends_on`` that is at risk at ``now``.

    At risk iff ``now >= start_of_business_day(ends_on + 1 day) + 48h``, that is
    ``start_of_business_day(ends_on + 1 day) <= now - 48h``. Local midnight of a
    date is at or before an instant exactly when the date is at or before that
    instant's business date, so the condition is
    ``ends_on + 1 day <= business_date(now - 48h)``.
    """
    return business_date(now - AT_RISK_AFTER, settings.business_zone) - timedelta(days=1)


Predicate = Callable[[datetime, Settings], Sequence[ColumnElement[bool]]]


@dataclass(frozen=True)
class _Sweep:
    field: str
    event: BookingEvent
    predicate: Predicate


_SWEEPS: tuple[_Sweep, ...] = (
    _Sweep(
        "expired_holds",
        BookingEvent.expire_hold,
        lambda now, _: (
            Booking.status == BookingStatus.requested,
            Booking.hold_expires_at < now,
        ),
    ),
    _Sweep(
        "expired_deposits",
        BookingEvent.expire_deposit,
        lambda now, _: (
            Booking.status == BookingStatus.accepted,
            Booking.deposit_due_at < now,
        ),
    ),
    _Sweep(
        "completed",
        BookingEvent.complete,
        lambda now, _: (
            Booking.status == BookingStatus.returned,
            Booking.claim_window_ends_at < now,
            Booking.claimed_at.is_(None),
        ),
    ),
    _Sweep(
        "flagged_at_risk",
        BookingEvent.flag_at_risk,
        lambda now, settings: (
            Booking.status == BookingStatus.in_progress,
            Booking.at_risk_at.is_(None),
            Booking.ends_on <= at_risk_last_end_date(now, settings),
        ),
    ),
)


async def _candidates(
    session_factory: async_sessionmaker[AsyncSession],
    where: Sequence[ColumnElement[bool]],
) -> list[uuid.UUID]:
    async with session_factory() as session:
        result = await session.execute(
            select(Booking.id).where(*where).order_by(Booking.created_at, Booking.id)
        )
        return list(result.scalars())


async def _process(
    session_factory: async_sessionmaker[AsyncSession],
    booking_id: uuid.UUID,
    sweep: _Sweep,
    where: Sequence[ColumnElement[bool]],
    *,
    clock: Clock,
    notifier: Notifier,
    settings: Settings,
) -> bool:
    """Apply the sweep's event to one booking in its own transaction.

    Returns True when the booking moved (the transaction committed).
    """
    try:
        async with session_factory() as session, session.begin():
            locked = await session.scalar(
                select(Booking.id)
                .where(Booking.id == booking_id, *where)
                .with_for_update(skip_locked=True)
            )
            if locked is None:
                return False
            await transition(
                session,
                booking_id,
                Actor.system(),
                sweep.event,
                clock=clock,
                notifier=notifier,
                settings=settings,
            )
    except (InvalidTransition, BookingGuardFailed) as exc:
        logger.warning(
            "timer_transition_skipped booking_id=%s event=%s code=%s",
            booking_id,
            sweep.event.value,
            exc.code,
        )
        return False
    return True


async def run_timers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock,
    notifier: Notifier,
    settings: Settings,
) -> TimerReport:
    """Run every sweep once at ``clock.now()`` and report what moved."""
    now = clock.now()
    run_clock = FrozenClock(now)
    counts: dict[str, int] = {}
    for sweep in _SWEEPS:
        where = sweep.predicate(now, settings)
        moved = 0
        for booking_id in await _candidates(session_factory, where):
            if await _process(
                session_factory,
                booking_id,
                sweep,
                where,
                clock=run_clock,
                notifier=notifier,
                settings=settings,
            ):
                moved += 1
        counts[sweep.field] = moved
    return TimerReport(**counts)
