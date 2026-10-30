"""The booking state machine (spec 3.1).

Two entry points:

* :func:`request_booking` creates a ``requested`` booking (the table's ``request``
  row). Its guards run in order: the asset is publicly visible (else 404), the
  dates are valid and start no earlier than today on the business calendar, the
  fulfilment is offered, the renter is not restricted, the renter's trust tier is
  high enough, no blocked availability window overlaps, and finally the insert
  itself, where the exclusion constraint ``ex_bookings_no_live_overlap`` is the
  real guard against overlapping live bookings.
* :func:`transition` applies an event to an existing booking. It locks the row
  (``SELECT ... FOR UPDATE``), looks up ``(status, event)`` in ``_TRANSITIONS``,
  checks the actor, runs the handler, writes one audit row and flushes.

A handler may also record an intermediate step itself: ``deposit_captured``
writes the ``booking.deposit_paid`` audit row inside its handler and returns
``_Outcome.before`` so the engine's ``booking.confirmed`` row starts from
``deposit_paid``. Money transitions post their ledger groups with the recipes in
:mod:`app.services.money`, using amounts from the booking's ``price_snapshot``.

``_TRANSITIONS`` is a plain dict from ``(BookingStatus, BookingEvent)`` to a
:class:`_Rule`. Later steps add their rows to it with :func:`_register`. A missing
row, a role the rule does not allow, or a renter or vendor who is not this
booking's own party all raise :class:`InvalidTransition` (409) before anything is
written. A handler checks its guards before it changes anything, so a failed
guard (:class:`BookingGuardFailed`) also writes nothing.

Services flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import exists, inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, business_date, start_of_business_day
from app.core.config import Settings
from app.db.models import (
    Asset,
    AuditEvent,
    AvailabilityWindow,
    Booking,
    Direction,
    HandoverRecord,
    LedgerAccount,
    LedgerEntry,
    Payment,
    User,
)
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection, HandoverStatus
from app.db.models.payment import PaymentStatus, PaymentType
from app.db.models.user import TrustTier
from app.schemas.errors import (
    BookingGuardFailed,
    InvalidRequestError,
    InvalidTransition,
    ResourceNotFoundError,
)
from app.services.audit import Actor, record_audit
from app.services.inventory import get_public_asset
from app.services.ledger import Posting, post_txn
from app.services.money import (
    balance_captured_postings,
    commission_amount,
    deposit_captured_postings,
    deposit_release_postings,
    refund_contribution_postings,
)
from app.services.notify import Notifier
from app.services.pricing import money, price_snapshot
from app.services.trust import required_tier_met, tier_effective

HOLD_PERIOD = timedelta(hours=4)
DEPOSIT_PERIOD = timedelta(hours=12)
CLAIM_WINDOW = timedelta(hours=24)

AUDIT_ENTITY = "booking"
REQUEST_EVENT = "request"

# SQLSTATEs from the booking insert that mean "someone else holds these dates".
_EXCLUSION_VIOLATION = "23P01"
_DEADLOCK_DETECTED = "40P01"
_CHECK_VIOLATION = "23514"
_NOT_SELF_CONSTRAINT = "ck_bookings_not_self"

# Columns never reported in an audit diff (maintained by the database).
_UNAUDITED_COLUMNS = frozenset({"created_at", "updated_at"})


class BookingEvent(StrEnum):
    accept = "accept"
    decline = "decline"
    approve_vendor = "approve_vendor"
    expire_hold = "expire_hold"
    deposit_captured = "deposit_captured"
    expire_deposit = "expire_deposit"
    cancel = "cancel"
    handover_completed = "handover_completed"
    balance_captured = "balance_captured"
    return_completed = "return_completed"
    claim_raised = "claim_raised"
    complete = "complete"
    resolve = "resolve"
    flag_at_risk = "flag_at_risk"
    mark_lost = "mark_lost"


# ---------------------------------------------------------------------------
# The transition registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Notice:
    """One notification to send after the transition is recorded."""

    user_id: uuid.UUID | None
    template: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Context:
    """What a handler sees: the locked booking and the request around it."""

    session: AsyncSession
    booking: Booking
    actor: Actor
    event: BookingEvent
    payload: Mapping[str, Any]
    now: datetime
    clock: Clock
    notifier: Notifier
    settings: Settings


@dataclass(frozen=True)
class _Outcome:
    """What a handler returns once it has changed the booking.

    ``action`` defaults to ``booking.<new status>``. ``before`` and ``after`` are
    merged into the audit row's before- and after-data, on top of the changed
    columns (``before`` lets a handler that recorded an intermediate step start
    the final row from that step's status).
    """

    action: str | None = None
    before: Mapping[str, Any] = field(default_factory=dict)
    after: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    notices: tuple[Notice, ...] = ()


Handler = Callable[[_Context], Awaitable[_Outcome]]


@dataclass(frozen=True)
class _Rule:
    """Who may send an event from a status, and what it does.

    ``roles`` are the actor roles allowed. With ``party_only`` (the default), a
    renter or vendor actor must be this booking's ``renter_id`` / ``vendor_id``.
    Admin and system actors are never party-checked.
    """

    roles: frozenset[ActorRole]
    handler: Handler
    party_only: bool = True


_TRANSITIONS: dict[tuple[BookingStatus, BookingEvent], _Rule] = {}


def _register(
    status: BookingStatus,
    event: BookingEvent,
    *roles: ActorRole,
    party_only: bool = True,
) -> Callable[[Handler], Handler]:
    """Add the handler for ``(status, event)`` to ``_TRANSITIONS``."""

    def decorate(handler: Handler) -> Handler:
        key = (status, event)
        if key in _TRANSITIONS:
            raise ValueError(f"transition already registered: {key}")
        _TRANSITIONS[key] = _Rule(frozenset(roles), handler, party_only)
        return handler

    return decorate


def _actor_allowed(rule: _Rule, actor: Actor, booking: Booking) -> bool:
    if actor.role not in rule.roles:
        return False
    if not rule.party_only:
        return True
    if actor.role == ActorRole.renter:
        return actor.user_id == booking.renter_id
    if actor.role == ActorRole.vendor:
        return actor.user_id == booking.vendor_id
    return True


def _audited_columns(booking: Booking) -> dict[str, Any]:
    return {
        attr.key: getattr(booking, attr.key)
        for attr in inspect(Booking).column_attrs
        if attr.key not in _UNAUDITED_COLUMNS
    }


async def transition(
    session: AsyncSession,
    booking_id: uuid.UUID,
    actor: Actor,
    event: BookingEvent,
    payload: Mapping[str, Any] | None = None,
    *,
    clock: Clock,
    notifier: Notifier,
    settings: Settings,
) -> Booking:
    """Apply ``event`` to the booking as ``actor``; flush, never commit.

    Raises :class:`ResourceNotFoundError` for an unknown booking,
    :class:`InvalidTransition` when the table has no such row or the actor may not
    send it, and :class:`BookingGuardFailed` when a guard fails.
    """
    event = BookingEvent(event)
    booking = await session.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if booking is None:
        raise ResourceNotFoundError()

    from_status = booking.status
    rule = _TRANSITIONS.get((from_status, event))
    if rule is None or not _actor_allowed(rule, actor, booking):
        raise InvalidTransition(from_status.value, event.value)

    before_columns = _audited_columns(booking)
    context = _Context(
        session=session,
        booking=booking,
        actor=actor,
        event=event,
        payload=payload or {},
        now=clock.now(),
        clock=clock,
        notifier=notifier,
        settings=settings,
    )
    outcome = await rule.handler(context)

    after_columns = _audited_columns(booking)
    changed = [key for key, value in after_columns.items() if before_columns[key] != value]
    before = {
        "status": from_status,
        **{key: before_columns[key] for key in changed},
        **outcome.before,
    }
    after = {
        "status": booking.status,
        **{key: after_columns[key] for key in changed},
        **outcome.after,
    }
    await record_audit(
        session,
        actor=actor,
        entity=AUDIT_ENTITY,
        entity_id=booking.id,
        action=outcome.action or f"booking.{booking.status.value}",
        before=before,
        after=after,
        reason=outcome.reason,
    )
    for notice in outcome.notices:
        await notifier.send(notice.user_id, notice.template, notice.data)
    return booking


# ---------------------------------------------------------------------------
# Handlers: requested
# ---------------------------------------------------------------------------


def _notice(booking: Booking, user_id: uuid.UUID, template: str, **extra: Any) -> Notice:
    data = {"booking_id": str(booking.id), "status": booking.status.value}
    data.update({key: _plain(value) for key, value in extra.items()})
    return Notice(user_id, template, data)


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    return value


def _reason(payload: Mapping[str, Any]) -> str | None:
    reason = payload.get("reason")
    if not isinstance(reason, str):
        return None
    return reason.strip() or None


@_register(BookingStatus.requested, BookingEvent.accept, ActorRole.vendor)
async def _accept(ctx: _Context) -> _Outcome:
    booking = ctx.booking
    if not ctx.now < booking.hold_expires_at:
        raise BookingGuardFailed(
            "hold_expired", {"hold_expires_at": _plain(booking.hold_expires_at)}
        )
    minimum = await ctx.session.scalar(
        select(Asset.minimum_trust_tier).where(Asset.id == booking.asset_id)
    )
    if minimum == TrustTier.vendor_approved and booking.vendor_approved_at is None:
        raise BookingGuardFailed("vendor_approval_required")

    booking.status = BookingStatus.accepted
    booking.deposit_due_at = ctx.now + DEPOSIT_PERIOD
    return _Outcome(
        notices=(
            _notice(
                booking,
                booking.renter_id,
                "booking.accepted",
                deposit_due_at=booking.deposit_due_at,
            ),
        )
    )


@_register(BookingStatus.requested, BookingEvent.decline, ActorRole.vendor)
async def _decline(ctx: _Context) -> _Outcome:
    booking = ctx.booking
    booking.status = BookingStatus.declined
    # "Notify renter with alternatives": the notification only (R8).
    return _Outcome(
        reason=_reason(ctx.payload),
        notices=(_notice(booking, booking.renter_id, "booking.declined"),),
    )


@_register(BookingStatus.requested, BookingEvent.approve_vendor, ActorRole.vendor)
async def _approve_vendor(ctx: _Context) -> _Outcome:
    booking = ctx.booking
    if booking.vendor_approved_at is not None:
        raise InvalidTransition(booking.status.value, ctx.event.value)
    booking.vendor_approved_at = ctx.now
    return _Outcome(action="booking.vendor_approved")


@_register(BookingStatus.requested, BookingEvent.expire_hold, ActorRole.system)
async def _expire_hold(ctx: _Context) -> _Outcome:
    booking = ctx.booking
    if not ctx.now > booking.hold_expires_at:
        raise InvalidTransition(booking.status.value, ctx.event.value)
    booking.status = BookingStatus.expired
    return _Outcome(notices=(_notice(booking, booking.renter_id, "booking.expired"),))


# ---------------------------------------------------------------------------
# Handlers: money (accepted, confirmed)
# ---------------------------------------------------------------------------


def _snapshot_amount(booking: Booking, key: str) -> Decimal:
    """A money value frozen in ``price_snapshot`` (a missing optional key is 0)."""
    return money(booking.price_snapshot.get(key, "0"))


def _payload_amount(payload: Mapping[str, Any]) -> Decimal | None:
    """The payload's ``amount`` as a Decimal, or None if absent or not money."""
    raw = payload.get("amount")
    if isinstance(raw, bool) or not isinstance(raw, Decimal | int | str):
        return None  # floats are never money
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


async def _deposit_payment_id(ctx: _Context) -> uuid.UUID:
    """The payload's ``payment_id``, which must be this booking's deposit payment."""
    raw = ctx.payload.get("payment_id")
    try:
        payment_id = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except ValueError:
        payment_id = None
    found = payment_id is not None and await ctx.session.scalar(
        select(
            exists().where(
                Payment.id == payment_id,
                Payment.booking_id == ctx.booking.id,
                Payment.type == PaymentType.deposit,
            )
        )
    )
    if not found:
        raise InvalidRequestError(
            "The payment is not this booking's deposit.", {"payment_id": _plain(raw)}
        )
    return payment_id


async def _post(
    ctx: _Context, postings: list[Posting], memo: str, payment_id: uuid.UUID | None = None
) -> None:
    """Write one ledger group for this booking; an empty recipe writes nothing."""
    if postings:
        await post_txn(
            ctx.session,
            postings=postings,
            booking_id=ctx.booking.id,
            payment_id=payment_id,
            memo=memo,
        )


async def _phones(ctx: _Context) -> dict[uuid.UUID, str]:
    booking = ctx.booking
    rows = await ctx.session.execute(
        select(User.id, User.phone).where(User.id.in_((booking.renter_id, booking.vendor_id)))
    )
    return {user_id: phone for user_id, phone in rows.all()}


def _both(booking: Booking, template: str, **extra: Any) -> tuple[Notice, ...]:
    return tuple(
        _notice(booking, user_id, template, **extra)
        for user_id in (booking.renter_id, booking.vendor_id)
    )


@_register(BookingStatus.accepted, BookingEvent.deposit_captured, ActorRole.system)
async def _deposit_captured(ctx: _Context) -> _Outcome:
    """accepted -> deposit_paid -> confirmed in one call, with two audit rows."""
    booking = ctx.booking
    deposit = _snapshot_amount(booking, "deposit")
    contribution = _snapshot_amount(booking, "damage_contribution")
    expected = deposit + contribution
    amount = _payload_amount(ctx.payload)
    if amount is None or amount != expected:
        raise BookingGuardFailed(
            "amount_mismatch",
            {"expected": str(expected), "amount": _plain(ctx.payload.get("amount"))},
        )
    payment_id = await _deposit_payment_id(ctx)

    booking.status = BookingStatus.deposit_paid
    await record_audit(
        ctx.session,
        actor=ctx.actor,
        entity=AUDIT_ENTITY,
        entity_id=booking.id,
        action=f"booking.{BookingStatus.deposit_paid.value}",
        before={"status": BookingStatus.accepted},
        after={
            "status": BookingStatus.deposit_paid,
            "amount": money(amount),
            "payment_id": payment_id,
        },
    )
    await _post(
        ctx, deposit_captured_postings(deposit, contribution), "deposit_captured", payment_id
    )

    booking.status = BookingStatus.confirmed
    # "Release contact details": each party gets the counterpart's phone (R8).
    phones = await _phones(ctx)
    return _Outcome(
        before={"status": BookingStatus.deposit_paid},
        notices=(
            _notice(
                booking,
                booking.renter_id,
                "booking.confirmed",
                counterpart_phone=phones.get(booking.vendor_id),
            ),
            _notice(
                booking,
                booking.vendor_id,
                "booking.confirmed",
                counterpart_phone=phones.get(booking.renter_id),
            ),
        ),
    )


@_register(BookingStatus.accepted, BookingEvent.expire_deposit, ActorRole.system)
async def _expire_deposit(ctx: _Context) -> _Outcome:
    booking = ctx.booking
    if not ctx.now > booking.deposit_due_at:
        raise InvalidTransition(booking.status.value, ctx.event.value)
    booking.status = BookingStatus.expired
    return _Outcome(notices=_both(booking, "booking.expired"))


@_register(BookingStatus.confirmed, BookingEvent.cancel, ActorRole.renter, ActorRole.vendor)
async def _cancel(ctx: _Context) -> _Outcome:
    """Renter before the start day, or vendor any time; both refund in full (A8)."""
    booking = ctx.booking
    reason = _reason(ctx.payload)
    if ctx.actor.role == ActorRole.renter:
        cutoff = start_of_business_day(booking.starts_on, ctx.settings.business_zone)
        if not ctx.now < cutoff:
            raise BookingGuardFailed("cancel_window_closed", {"cancel_before": _plain(cutoff)})
        status = BookingStatus.cancelled_by_renter
    else:
        if reason is None:
            raise BookingGuardFailed("reason_required")
        status = BookingStatus.cancelled_by_vendor

    # By `confirmed` the renter has paid the deposit and the damage contribution
    # (the rental balance is due at handover); each goes back in its own txn_group.
    deposit = _snapshot_amount(booking, "deposit")
    contribution = _snapshot_amount(booking, "damage_contribution")
    await _post(ctx, deposit_release_postings(deposit), "cancel_deposit_refund")
    await _post(ctx, refund_contribution_postings(contribution), "cancel_contribution_refund")

    booking.status = status
    booking.cancelled_at = ctx.now
    booking.cancellation_reason = reason
    if status == BookingStatus.cancelled_by_vendor:
        await record_audit(
            ctx.session,
            actor=ctx.actor,
            entity="user",
            entity_id=booking.vendor_id,
            action="vendor.reliability_penalty",
            before=None,
            after={"booking_id": booking.id, "status": status},
            reason=reason,
        )
    return _Outcome(
        after={"refund": {"deposit": deposit, "damage_contribution": contribution}},
        reason=reason,
        notices=_both(booking, "booking.cancelled"),
    )


# ---------------------------------------------------------------------------
# Handlers: handover, balance and return (confirmed, in_progress)
# ---------------------------------------------------------------------------


async def _handover_done(ctx: _Context, direction: HandoverDirection) -> bool:
    """Whether this booking's ``direction`` handover record is completed (both signed)."""
    return bool(
        await ctx.session.scalar(
            select(
                exists().where(
                    HandoverRecord.booking_id == ctx.booking.id,
                    HandoverRecord.direction == direction,
                    HandoverRecord.status == HandoverStatus.completed,
                )
            )
        )
    )


async def _balance_posted(ctx: _Context) -> bool:
    """Whether ``balance_captured`` has already run for this booking.

    Its ledger group is the only one that credits ``vendor_payable`` or
    ``platform_revenue`` before the booking leaves ``confirmed``.
    """
    return bool(
        await ctx.session.scalar(
            select(
                exists().where(
                    LedgerEntry.booking_id == ctx.booking.id,
                    LedgerEntry.direction == Direction.cr.value,
                    LedgerEntry.account.in_(
                        (LedgerAccount.vendor_payable.value, LedgerAccount.platform_revenue.value)
                    ),
                )
            )
        )
    )


async def _handover_signal_recorded(ctx: _Context) -> bool:
    """Whether an earlier ``handover_completed`` left a ``booking.handover_signed`` row."""
    return bool(
        await ctx.session.scalar(
            select(
                exists().where(
                    AuditEvent.entity == AUDIT_ENTITY,
                    AuditEvent.entity_id == ctx.booking.id,
                    AuditEvent.action == "booking.handover_signed",
                )
            )
        )
    )


async def _balance_payment_id(ctx: _Context) -> uuid.UUID:
    """The payload's ``payment_id``: this booking's captured rental-balance payment."""
    raw = ctx.payload.get("payment_id")
    try:
        payment_id = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except ValueError:
        payment_id = None
    found = payment_id is not None and await ctx.session.scalar(
        select(
            exists().where(
                Payment.id == payment_id,
                Payment.booking_id == ctx.booking.id,
                Payment.type == PaymentType.rental_balance,
                Payment.status == PaymentStatus.captured,
            )
        )
    )
    if not found:
        raise InvalidRequestError(
            "The payment is not this booking's captured rental balance.",
            {"payment_id": _plain(raw)},
        )
    return payment_id


def _start_rental(booking: Booking, outcome_after: Mapping[str, Any]) -> _Outcome:
    """Both in_progress facts hold: confirmed -> in_progress."""
    booking.status = BookingStatus.in_progress
    return _Outcome(after=outcome_after, notices=_both(booking, "booking.in_progress"))


@_register(
    BookingStatus.confirmed,
    BookingEvent.handover_completed,
    ActorRole.renter,
    ActorRole.vendor,
    ActorRole.system,
)
async def _handover_completed(ctx: _Context) -> _Outcome:
    """Record the signed pickup; start the rental if the balance is already captured."""
    if await _handover_signal_recorded(ctx):
        raise InvalidTransition(ctx.booking.status.value, ctx.event.value)
    if not await _handover_done(ctx, HandoverDirection.pickup):
        raise BookingGuardFailed("handover_incomplete", {"direction": "pickup"})

    after = {"handover": HandoverDirection.pickup.value}
    if await _balance_posted(ctx):
        return _start_rental(ctx.booking, after)
    return _Outcome(action="booking.handover_signed", after=after)


@_register(BookingStatus.confirmed, BookingEvent.balance_captured, ActorRole.system)
async def _balance_captured(ctx: _Context) -> _Outcome:
    """Post R + F (vendor share and commission K); start the rental if handed over."""
    booking = ctx.booking
    if await _balance_posted(ctx):
        raise InvalidTransition(booking.status.value, ctx.event.value)
    rental = _snapshot_amount(booking, "rental_total")
    delivery_fee = _snapshot_amount(booking, "delivery_fee")
    expected = rental + delivery_fee
    amount = _payload_amount(ctx.payload)
    if amount is None or amount != expected:
        raise BookingGuardFailed(
            "amount_mismatch",
            {"expected": str(expected), "amount": _plain(ctx.payload.get("amount"))},
        )
    payment_id = await _balance_payment_id(ctx)

    # The rate frozen at request time, unquantized (it is a rate, not money).
    commission = commission_amount(rental, Decimal(booking.price_snapshot["commission_rate"]))
    await _post(
        ctx,
        balance_captured_postings(rental, delivery_fee, commission),
        "balance_captured",
        payment_id,
    )

    after = {"amount": money(amount), "payment_id": payment_id, "commission": commission}
    if await _handover_done(ctx, HandoverDirection.pickup):
        return _start_rental(booking, after)
    return _Outcome(action="booking.balance_captured", after=after)


@_register(
    BookingStatus.in_progress,
    BookingEvent.return_completed,
    ActorRole.renter,
    ActorRole.vendor,
    ActorRole.system,
)
async def _return_completed(ctx: _Context) -> _Outcome:
    """Both return signatures: open the 24-hour claim window."""
    if not await _handover_done(ctx, HandoverDirection.return_):
        raise BookingGuardFailed("handover_incomplete", {"direction": "return"})
    booking = ctx.booking
    booking.status = BookingStatus.returned
    booking.claim_window_ends_at = ctx.now + CLAIM_WINDOW
    return _Outcome(
        notices=_both(
            booking, "booking.returned", claim_window_ends_at=booking.claim_window_ends_at
        )
    )


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------


def _insert_conflict(exc: DBAPIError) -> str | None:
    """The guard code for a booking insert failure, or None to re-raise it."""
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in (_EXCLUSION_VIOLATION, _DEADLOCK_DETECTED):
        return "dates_unavailable"
    if sqlstate == _CHECK_VIOLATION:
        constraint = getattr(getattr(orig, "__cause__", None), "constraint_name", None)
        if constraint == _NOT_SELF_CONSTRAINT or _NOT_SELF_CONSTRAINT in str(orig):
            return "self_booking"
    return None


def _tier_allows(effective: int, minimum: int) -> bool:
    # A tier-3 asset may be requested at effective tier 2; the vendor's approval
    # is checked when the booking is accepted.
    if minimum == TrustTier.vendor_approved:
        return effective >= TrustTier.established
    return required_tier_met(effective, minimum, vendor_approved=False)


async def request_booking(
    session: AsyncSession,
    *,
    renter: Actor,
    asset_id: uuid.UUID,
    starts_on: date,
    ends_on: date,
    fulfilment: Literal["pickup", "delivery"],
    damage_contribution: bool,
    clock: Clock,
    notifier: Notifier,
    settings: Settings,
) -> Booking:
    """Create a ``requested`` booking holding the unit for four hours."""
    if renter.role != ActorRole.renter:
        raise InvalidTransition(None, REQUEST_EVENT)

    asset = (
        await get_public_asset(session, asset_id, launched_cities=settings.launched_cities)
    ).asset

    now = clock.now()
    if starts_on > ends_on:
        raise InvalidRequestError("The start date must not be after the end date.")
    if starts_on < business_date(now, settings.business_zone):
        raise InvalidRequestError("The start date must not be in the past.")
    if fulfilment not in ("pickup", "delivery") or not (asset.fulfilment or {}).get(fulfilment):
        raise InvalidRequestError(
            "This item does not offer that fulfilment.", {"fulfilment": fulfilment}
        )

    user = await session.get(User, renter.user_id)
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or user.restricted_at is not None
    ):
        raise BookingGuardFailed("renter_restricted")

    effective = await tier_effective(session, user.id)
    if not _tier_allows(effective, asset.minimum_trust_tier):
        raise BookingGuardFailed(
            "tier_insufficient",
            {"required": asset.minimum_trust_tier, "effective": effective},
        )

    blocked = await session.scalar(
        select(
            exists().where(
                AvailabilityWindow.asset_id == asset.id,
                AvailabilityWindow.is_blocked.is_(True),
                AvailabilityWindow.starts_at <= ends_on,
                AvailabilityWindow.ends_at >= starts_on,
            )
        )
    )
    if blocked:
        raise BookingGuardFailed("dates_blocked")

    snapshot = price_snapshot(asset, starts_on, ends_on, fulfilment, damage_contribution, settings)
    booking = Booking(
        asset_id=asset.id,
        renter_id=user.id,
        vendor_id=asset.vendor_id,
        starts_on=starts_on,
        ends_on=ends_on,
        status=BookingStatus.requested,
        total_rental_fee=Decimal(snapshot["rental_total"]),
        deposit_amount=Decimal(snapshot["deposit"]),
        price_snapshot=snapshot,
        fulfilment_choice=fulfilment,
        hold_expires_at=now + HOLD_PERIOD,
    )
    try:
        async with session.begin_nested():
            session.add(booking)
            await session.flush()
    except DBAPIError as exc:
        code = _insert_conflict(exc)
        if code is None:
            raise
        raise BookingGuardFailed(code) from exc

    await record_audit(
        session,
        actor=renter,
        entity=AUDIT_ENTITY,
        entity_id=booking.id,
        action="booking.requested",
        before=None,
        after={
            "status": booking.status,
            "asset_id": booking.asset_id,
            "starts_on": booking.starts_on,
            "ends_on": booking.ends_on,
            "fulfilment_choice": booking.fulfilment_choice,
            "hold_expires_at": booking.hold_expires_at,
            "total_rental_fee": booking.total_rental_fee,
            "deposit_amount": booking.deposit_amount,
            "price_snapshot": booking.price_snapshot,
        },
    )
    notice = _notice(
        booking, booking.vendor_id, "booking.requested", hold_expires_at=booking.hold_expires_at
    )
    await notifier.send(notice.user_id, notice.template, notice.data)
    return booking
