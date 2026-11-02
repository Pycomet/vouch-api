"""Provider webhooks: record every event once, then act on it (R22-R24, A14).

:func:`handle_webhook` is the whole of ``POST /webhooks/{provider}``:

1. The signature of the raw body is checked. A bad one is recorded with
   ``signature_ok = false`` and nothing else happens (the route answers 401). It
   is stored under ``unverified:<sha256 of the body>``, never under the id the
   body claims, so a forged body cannot take a real event's id and make the
   genuine delivery look like a replay. The claimed id stays in the payload.
2. A verified event is inserted with ``ON CONFLICT (provider, provider_event_id)
   DO NOTHING RETURNING id``. No row back means the event was seen before: the
   answer is ``duplicate`` and nothing else is done, so a replay never posts
   twice. The insert and the processing share one transaction, so a concurrent
   copy of the same event waits on the unique index and then sees a duplicate.
3. The event is dispatched by type through :data:`_HANDLERS`, inside a
   SAVEPOINT. A domain error (``InvalidTransition``, ``BookingGuardFailed``, an
   unknown booking or payment...) rolls the savepoint back, so the booking and
   payment are untouched, and its code goes into ``processing_error``; the event
   row stays. The route still answers 200, so the provider does not retry an
   event that can never succeed. Any other error propagates (500), the whole
   transaction rolls back, event row included, and the provider's retry runs it
   again.
4. ``processed_at`` is set on every verified event.

``payment_captured`` (the renter's one upfront payment, A14) finds the
``booking`` payment row by the provider's reference, in status ``pending`` or
``failed`` (a payment that arrives after the booking closed unpaid), and then:

* **the amount is not the row's amount**: the money is still real. The row is
  marked ``captured`` for what arrived (the expected amount kept in
  ``metadata``), an ``unapplied_payment`` group holds it for the renter and an
  ``unapplied_payment_refund`` group returns it at once, with a pending refund
  row and the audit row ``payment.refunded_mismatch``. ``processing_error`` is
  ``amount_mismatch``. The booking is not touched: it has no pending payment
  any more, so nobody can pay it, and the deposit timer expires it at
  ``deposit_due_at`` (the renter requests again);
* **the booking is not ``accepted``** (expired, withdrawn, cancelled, declined:
  R23): the late money is captured and returned. The capture group is posted as
  for any payment, then its exact reversal (``late_payment_refund``), with a
  pending refund row for the whole amount and the audit row
  ``payment.refunded_late``. The booking state does not change;
* **otherwise** the row is marked ``captured`` and the booking moves through
  :func:`app.services.booking.transition` (``payment_captured``), which posts
  the split of the money and confirms it. That runs in its own savepoint: if
  the booking refuses the capture for any guard reason, the refusal is rolled
  back and the money takes the wrong-amount path instead (audit
  ``payment.refunded_mismatch`` for ``amount_mismatch``, else
  ``payment.refunded_unapplied``), with the guard's code as
  ``processing_error``.

Money never moves without a ledger entry: a payment into an account whose row
is already ``captured`` (a second transfer) is held and returned the same way
(``unapplied_payment`` / ``unapplied_payment_refund``, a pending refund row with
reason ``duplicate_payment``, audit ``payment.refunded_duplicate``). The
captured row is left untouched, and ``processing_error`` is
``payment_already_captured``.

``payout_completed`` (the vendor's transfer landed) posts ``payout`` (dr
vendor_payable / cr platform_cash) for the booking and records the ``payout``
payment row as paid. The amount may not exceed the booking's vendor_payable.

The booking row is locked (``FOR UPDATE``) before anything is decided, so a
payment and a renter's withdraw of the same booking are serialised: whichever
commits first, the other sees its result.

Services flush and never commit: the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import Booking, LedgerAccount, Payment, WebhookEvent
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentStatus, PaymentType, PayoutStatus
from app.providers.payment import PaymentEvent, PaymentEventError, PaymentProvider
from app.schemas.errors import VouchAPIError
from app.services.audit import Actor, record_audit
from app.services.booking import BookingEvent, booking_amounts, transition
from app.services.ledger import Posting, account_balance, post_txn
from app.services.money import (
    payment_captured_postings,
    payout_postings,
    reversal_postings,
    unapplied_payment_postings,
)
from app.services.notify import Notifier
from app.services.payments import record_refund

UNVERIFIED_PREFIX = "unverified:"
UNPARSED_PREFIX = "sha256:"


class WebhookProcessingError(Exception):
    """An event that is genuine but cannot be applied; ``code`` is recorded."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WebhookResult:
    """What became of one delivery.

    ``status`` is ``processed`` (recorded and acted on, or recorded with a
    ``processing_error``), ``duplicate`` (seen before; nothing done) or
    ``invalid_signature`` (recorded, nothing done; the route answers 401).
    """

    status: Literal["processed", "duplicate", "invalid_signature"]
    processing_error: str | None = None


@dataclass(frozen=True)
class _Context:
    session: AsyncSession
    provider: PaymentProvider
    now: datetime
    clock: Clock
    notifier: Notifier
    settings: Settings
    request_id: str | None


# ---------------------------------------------------------------------------
# Recording the event
# ---------------------------------------------------------------------------


def _refuse_constant(name: str) -> Any:
    raise ValueError(name)


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _loose_json(body: bytes) -> dict[str, Any] | None:
    """The body as a JSON object to store, or ``None`` if it is not one.

    Numbers with a fraction are kept as their text (never a binary float), and a
    body JSONB cannot hold (NaN, a NUL character) is treated as unreadable.
    """
    try:
        parsed = json.loads(body, parse_float=str, parse_constant=_refuse_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(parsed, dict) or "\\u0000" in json.dumps(parsed):
        return None
    return parsed


def _stored_payload(body: bytes) -> dict[str, Any]:
    return _loose_json(body) or {"body_sha256": _body_hash(body)}


async def _record(
    session: AsyncSession,
    provider: str,
    event_id: str,
    payload: dict[str, Any],
    signature_ok: bool,
) -> int | None:
    """Insert the event row; its id, or ``None`` if this event is already stored."""
    statement = (
        pg_insert(WebhookEvent)
        .values(
            provider=provider,
            provider_event_id=event_id,
            payload=payload,
            signature_ok=signature_ok,
        )
        .on_conflict_do_nothing(index_elements=["provider", "provider_event_id"])
        .returning(WebhookEvent.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


def _event_id(event: PaymentEvent | None, body: bytes) -> str:
    """The provider's id for the event; for a body that does not parse, its own
    ``id`` if it has a usable one, else the hash of the body."""
    if event is not None:
        return event.provider_event_id
    loose = _loose_json(body) or {}
    claimed = loose.get("id")
    if isinstance(claimed, str) and claimed.strip():
        return claimed
    return UNPARSED_PREFIX + _body_hash(body)


async def handle_webhook(
    session: AsyncSession,
    provider: PaymentProvider,
    body: bytes,
    signature: str | None,
    *,
    clock: Clock,
    notifier: Notifier,
    settings: Settings,
    request_id: str | None = None,
) -> WebhookResult:
    """Record and process one delivery of ``body`` from ``provider``; flush, never commit."""
    if not provider.verify_webhook(signature, body):
        await _record(
            session,
            provider.name,
            UNVERIFIED_PREFIX + _body_hash(body),
            _stored_payload(body),
            signature_ok=False,
        )
        return WebhookResult("invalid_signature")

    try:
        event: PaymentEvent | None = provider.parse_event(body)
    except PaymentEventError:
        event = None
    row_id = await _record(
        session, provider.name, _event_id(event, body), _stored_payload(body), signature_ok=True
    )
    if row_id is None:
        return WebhookResult("duplicate")

    now = clock.now()
    context = _Context(session, provider, now, clock, notifier, settings, request_id)
    error = "malformed_event" if event is None else await _dispatch(context, event)
    await session.execute(
        update(WebhookEvent)
        .where(WebhookEvent.id == row_id)
        .values(processed_at=now, processing_error=error)
    )
    return WebhookResult("processed", error)


async def _dispatch(ctx: _Context, event: PaymentEvent) -> str | None:
    """Run the event's handler in a savepoint; the processing error, if any."""
    handler = _HANDLERS.get(event.type)
    if handler is None:
        return "unsupported_event"
    try:
        async with ctx.session.begin_nested():
            return await handler(ctx, event)
    except WebhookProcessingError as exc:
        return exc.code
    except VouchAPIError as exc:  # InvalidTransition, BookingGuardFailed, ...
        return exc.code


# ---------------------------------------------------------------------------
# payment_captured
# ---------------------------------------------------------------------------


async def _locked_booking(ctx: _Context, booking_id: uuid.UUID) -> Booking:
    booking = await ctx.session.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if booking is None:
        raise WebhookProcessingError("booking_not_found")
    return booking


async def _locked_payment(ctx: _Context, reference: str) -> Payment | None:
    return await ctx.session.scalar(
        select(Payment)
        .where(Payment.provider == ctx.provider.name, Payment.provider_reference == reference)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _post(
    ctx: _Context, booking: Booking, postings: list[Posting], memo: str, payment_id: uuid.UUID
) -> None:
    await post_txn(
        ctx.session, postings=postings, booking_id=booking.id, payment_id=payment_id, memo=memo
    )


def _capture(ctx: _Context, payment: Payment) -> None:
    payment.status = PaymentStatus.captured
    payment.captured_at = ctx.now
    payment.failed_at = None


async def _audit_payment(
    ctx: _Context, payment: Payment, action: str, before: dict[str, Any], after: dict[str, Any]
) -> None:
    await record_audit(
        ctx.session,
        actor=Actor.system(),
        entity="payment",
        entity_id=payment.id,
        action=action,
        before=before,
        after=after,
        request_id=ctx.request_id,
    )


async def _payment_captured(ctx: _Context, event: PaymentEvent) -> str | None:
    booking = await _locked_booking(ctx, event.booking_id)
    payment = await _locked_payment(ctx, event.payment_reference)
    if payment is None or payment.booking_id != booking.id or payment.type != PaymentType.booking:
        raise WebhookProcessingError("payment_not_found")
    if payment.status not in (PaymentStatus.pending, PaymentStatus.failed):
        await _refund_duplicate(ctx, booking, payment, event)
        return "payment_already_captured"
    if event.amount != payment.amount:
        await _refund_mismatch(ctx, booking, payment, event.amount, expected=payment.amount)
        return "amount_mismatch"
    if booking.status != BookingStatus.accepted:
        await _refund_late(ctx, booking, payment)
        return None

    try:
        # Its own savepoint: if the booking refuses the capture, the money still
        # arrived, so it is held and returned rather than rolled away.
        async with ctx.session.begin_nested():
            _capture(ctx, payment)
            await ctx.session.flush()
            await transition(
                ctx.session,
                booking.id,
                Actor.system(),
                BookingEvent.payment_captured,
                {"amount": event.amount, "payment_id": payment.id},
                clock=ctx.clock,
                notifier=ctx.notifier,
                settings=ctx.settings,
                request_id=ctx.request_id,
            )
    except VouchAPIError as refused:  # BookingGuardFailed, InvalidTransition, ...
        # The rollback expired both rows: read them again (still locked).
        booking = await _locked_booking(ctx, event.booking_id)
        payment = await _locked_payment(ctx, event.payment_reference)
        assert payment is not None  # locked above, in this transaction
        await _refund_mismatch(
            ctx,
            booking,
            payment,
            event.amount,
            expected=booking_amounts(booking).total,
            reason=refused.code,
        )
        return refused.code
    return None


async def _refund_late(ctx: _Context, booking: Booking, payment: Payment) -> None:
    """R23: the booking closed before the money came; capture it and give it back."""
    before = {"status": payment.status}
    _capture(ctx, payment)
    amounts = booking_amounts(booking)
    capture = payment_captured_postings(
        amounts.deposit,
        amounts.contribution,
        amounts.rental,
        amounts.delivery_fee,
        amounts.commission,
    )
    await _post(ctx, booking, capture, "payment_captured", payment.id)
    refund = await record_refund(ctx.session, payment, payment.amount, reason="late_payment")
    assert refund is not None  # payments.amount > 0 (CHECK)
    await _post(ctx, booking, reversal_postings(capture), "late_payment_refund", refund.id)
    await _audit_payment(
        ctx,
        payment,
        "payment.refunded_late",
        before,
        {
            "status": payment.status,
            "booking_id": booking.id,
            "booking_status": booking.status,
            "amount": payment.amount,
            "refund_payment": {"id": refund.id, "amount": refund.amount},
        },
    )


async def _hold_and_return(
    ctx: _Context, booking: Booking, received_on: Payment, amount: Decimal, reason: str
) -> Payment:
    """Ledger money that pays for nothing, then give it back (the controller ruling:
    money never moves without a ledger entry).

    ``unapplied_payment`` (linked to the payment it arrived on), a pending refund
    row for ``amount``, then ``unapplied_payment_refund`` (linked to the refund).
    Every booking account nets to 0. Returns the refund row.
    """
    held = unapplied_payment_postings(amount)
    await _post(ctx, booking, held, "unapplied_payment", received_on.id)
    refund = await record_refund(ctx.session, received_on, amount, reason=reason)
    assert refund is not None  # the event amount is > 0 (parse_event)
    await _post(ctx, booking, reversal_postings(held), "unapplied_payment_refund", refund.id)
    return refund


async def _refund_mismatch(
    ctx: _Context,
    booking: Booking,
    payment: Payment,
    received: Decimal,
    *,
    expected: Decimal,
    reason: str = "amount_mismatch",
) -> None:
    """Money the booking will not take (a wrong amount, or a capture the booking
    refused with ``reason``): the row records what arrived, which is held for the
    renter and returned. The booking is not touched."""
    before = {"status": payment.status, "amount": payment.amount}
    _capture(ctx, payment)
    payment.amount = received
    payment.metadata_ = {**(payment.metadata_ or {}), "expected_amount": str(expected)}
    refund = await _hold_and_return(ctx, booking, payment, received, reason)
    action = (
        "payment.refunded_mismatch" if reason == "amount_mismatch" else "payment.refunded_unapplied"
    )
    await _audit_payment(
        ctx,
        payment,
        action,
        before,
        {
            "status": payment.status,
            "booking_id": booking.id,
            "booking_status": booking.status,
            "amount": received,
            "expected_amount": expected,
            "reason": reason,
            "refund_payment": {"id": refund.id, "amount": refund.amount},
        },
    )


async def _refund_duplicate(
    ctx: _Context, booking: Booking, payment: Payment, event: PaymentEvent
) -> None:
    """A second payment into an account already captured: held and returned.

    The captured row is left as it is; the refund row records the money received
    (``refund_of`` the captured row, the event id in ``metadata``).
    """
    refund = await _hold_and_return(ctx, booking, payment, event.amount, "duplicate_payment")
    refund.metadata_ = {**(refund.metadata_ or {}), "provider_event_id": event.provider_event_id}
    await ctx.session.flush()
    await _audit_payment(
        ctx,
        payment,
        "payment.refunded_duplicate",
        {"status": payment.status},
        {
            "booking_id": booking.id,
            "booking_status": booking.status,
            "amount": event.amount,
            "provider_event_id": event.provider_event_id,
            "refund_payment": {"id": refund.id, "amount": refund.amount},
        },
    )


# ---------------------------------------------------------------------------
# payout_completed
# ---------------------------------------------------------------------------


async def _payout_completed(ctx: _Context, event: PaymentEvent) -> str | None:
    booking = await _locked_booking(ctx, event.booking_id)
    payout = await _locked_payment(ctx, event.payment_reference)
    if payout is not None and (
        payout.booking_id != booking.id or payout.type != PaymentType.payout
    ):
        raise WebhookProcessingError("payout_not_found")
    if payout is not None and payout.payout_status == PayoutStatus.paid:
        raise WebhookProcessingError("payout_already_paid")
    if payout is not None and payout.amount != event.amount:
        raise WebhookProcessingError("amount_mismatch")
    recipient = event.recipient_code or (payout.vendor_recipient_code if payout else None)
    if recipient is None:
        raise WebhookProcessingError("recipient_missing")
    payable = await account_balance(
        ctx.session, LedgerAccount.vendor_payable, booking_id=booking.id
    )
    if event.amount > payable:
        raise WebhookProcessingError("payout_exceeds_payable")

    if payout is None:
        payout = Payment(
            booking_id=booking.id,
            type=PaymentType.payout,
            amount=event.amount,
            currency="NGN",
            provider=ctx.provider.name,
            provider_reference=event.payment_reference,
        )
        ctx.session.add(payout)
    payout.status = PaymentStatus.captured
    payout.vendor_recipient_code = recipient
    payout.platform_fee = booking_amounts(booking).commission
    payout.payout_status = PayoutStatus.paid
    payout.payout_eligible_at = payout.payout_eligible_at or ctx.now
    payout.paid_at = ctx.now
    await ctx.session.flush()
    await _post(ctx, booking, payout_postings(event.amount), "payout", payout.id)
    await _audit_payment(
        ctx,
        payout,
        "payout.paid",
        {"vendor_payable": payable},
        {
            "booking_id": booking.id,
            "amount": event.amount,
            "recipient_code": recipient,
            "vendor_payable": payable - event.amount,
        },
    )
    return None


Handler = Callable[[_Context, PaymentEvent], Awaitable[str | None]]

# The provider event types this API acts on; any other is ``unsupported_event``.
_HANDLERS: dict[str, Handler] = {
    "payment_captured": _payment_captured,
    "payout_completed": _payout_completed,
}
