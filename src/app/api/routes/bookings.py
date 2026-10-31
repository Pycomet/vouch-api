"""Booking endpoints (spec 3.3): create a booking and move it through its states.

Every route needs a bearer token (401 ``unauthenticated`` otherwise). Routes on
an existing booking check, in this order (R19), before any body is validated or
any transition runs:

1. **Visibility.** The caller must be the booking's renter (renter role), its
   vendor (vendor role) or an admin. Anyone else gets 404 ``resource_not_found``,
   exactly as for an unknown id.
2. **Role.** A party whose role may not take this action gets 403 ``forbidden``.
3. **Transition.** The booking service applies the event; an illegal one is 409
   ``invalid_transition`` and a failed guard 409 with the guard's code.

Services flush and never commit: each write route commits after the service
returns, then answers with the booking and its freshly computed money line.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_current_actor, get_notifier, require_role
from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import ActorRole, Booking
from app.db.models.handover import HandoverDirection
from app.db.session import get_session
from app.schemas.bookings import (
    BookingCreate,
    BookingOut,
    ClaimCreate,
    HandoverSubmit,
    PriceOut,
    ReasonBody,
    Resolve,
    format_money,
)
from app.schemas.errors import ErrorEnvelope, ForbiddenError, ResourceNotFoundError
from app.services.audit import Actor
from app.services.booking import BookingEvent, request_booking, transition
from app.services.handover import submit_handover
from app.services.money_line import compute_money_line
from app.services.notify import Notifier
from app.services.pricing import money

router = APIRouter(prefix="/bookings", tags=["bookings"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid bearer token."},
    403: {"model": ErrorEnvelope, "description": "Your role may not take this action."},
    404: {"model": ErrorEnvelope, "description": "No such booking, or not yours to see."},
    409: {"model": ErrorEnvelope, "description": "Not allowed in the booking's current state."},
}

_PARTIES = (ActorRole.renter, ActorRole.vendor)
_PRICE_KEYS = ("rental_total", "delivery_fee", "deposit", "damage_contribution")


# ---------------------------------------------------------------------------
# Visibility and role (R19)
# ---------------------------------------------------------------------------


def _can_see(actor: Actor, booking: Booking) -> bool:
    if actor.role == ActorRole.admin:
        return True
    if actor.role == ActorRole.renter:
        return actor.user_id == booking.renter_id
    if actor.role == ActorRole.vendor:
        return actor.user_id == booking.vendor_id
    return False


def booking_for(*roles: ActorRole) -> Callable[..., Awaitable[Booking]]:
    """A dependency loading the path's booking for a caller with one of ``roles``.

    404 when the caller may not see the booking (or it does not exist), then 403
    when they may see it but their role cannot take this action.
    """
    allowed = frozenset(roles)

    async def dependency(
        booking_id: uuid.UUID,
        actor: Actor = Depends(get_current_actor),
        session: AsyncSession = Depends(get_session),
    ) -> Booking:
        booking = await session.get(Booking, booking_id)
        if booking is None or not _can_see(actor, booking):
            raise ResourceNotFoundError()
        if actor.role not in allowed:
            raise ForbiddenError()
        return booking

    return dependency


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def _out(session: AsyncSession, booking: Booking, settings: Settings) -> BookingOut:
    snapshot = booking.price_snapshot or {}
    price = PriceOut(
        **{key: format_money(money(snapshot.get(key, "0"))) for key in _PRICE_KEYS},
        currency=snapshot.get("currency", "NGN"),
    )
    return BookingOut(
        id=booking.id,
        status=booking.status.value,
        asset_id=booking.asset_id,
        renter_id=booking.renter_id,
        vendor_id=booking.vendor_id,
        starts_on=booking.starts_on,
        ends_on=booking.ends_on,
        fulfilment_choice=booking.fulfilment_choice,
        hold_expires_at=booking.hold_expires_at,
        deposit_due_at=booking.deposit_due_at,
        claim_window_ends_at=booking.claim_window_ends_at,
        vendor_approved_at=booking.vendor_approved_at,
        price=price,
        money_line=await compute_money_line(session, booking, settings=settings),
    )


async def _apply(
    request: Request,
    session: AsyncSession,
    booking: Booking,
    actor: Actor,
    event: BookingEvent,
    payload: dict[str, Any] | None,
    clock: Clock,
    notifier: Notifier,
) -> BookingOut:
    settings = _settings(request)
    updated = await transition(
        session,
        booking.id,
        actor,
        event,
        payload,
        clock=clock,
        notifier=notifier,
        settings=settings,
        request_id=_request_id(request),
    )
    await session.commit()
    return await _out(session, updated, settings)


def _reason(body: ReasonBody | None) -> dict[str, Any]:
    return {} if body is None or body.reason is None else {"reason": body.reason}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request a booking",
    responses={**_ERRORS, 400: {"model": ErrorEnvelope, "description": "Invalid dates."}},
)
async def create_booking(
    body: BookingCreate,
    request: Request,
    actor: Actor = Depends(require_role(ActorRole.renter)),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """The renter asks for an item; the unit is held for four hours (``requested``)."""
    settings = _settings(request)
    booking = await request_booking(
        session,
        renter=actor,
        asset_id=body.asset_id,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        fulfilment=body.fulfilment,
        damage_contribution=body.damage_contribution,
        clock=clock,
        notifier=notifier,
        settings=settings,
        request_id=_request_id(request),
    )
    await session.commit()
    return await _out(session, booking, settings)


@router.get(
    "/{booking_id}",
    response_model=BookingOut,
    summary="Get a booking with its money line",
    responses=_ERRORS,
)
async def get_booking(
    request: Request,
    booking: Booking = Depends(booking_for(*_PARTIES, ActorRole.admin)),
    session: AsyncSession = Depends(get_session),
) -> BookingOut:
    """A party or an admin reads the booking; the money line is computed now."""
    return await _out(session, booking, _settings(request))


@router.post(
    "/{booking_id}/accept",
    response_model=BookingOut,
    summary="Accept a request (vendor)",
    responses=_ERRORS,
)
async def accept_booking(
    request: Request,
    booking: Booking = Depends(booking_for(ActorRole.vendor)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Accept before the hold expires; payment is then due within 12 hours."""
    return await _apply(
        request, session, booking, actor, BookingEvent.accept, None, clock, notifier
    )


@router.post(
    "/{booking_id}/decline",
    response_model=BookingOut,
    summary="Decline a request (vendor)",
    responses=_ERRORS,
)
async def decline_booking(
    request: Request,
    body: ReasonBody | None = None,
    booking: Booking = Depends(booking_for(ActorRole.vendor)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    return await _apply(
        request, session, booking, actor, BookingEvent.decline, _reason(body), clock, notifier
    )


@router.post(
    "/{booking_id}/approve",
    response_model=BookingOut,
    summary="Approve the renter of a tier-3 item (vendor)",
    responses=_ERRORS,
)
async def approve_booking(
    request: Request,
    booking: Booking = Depends(booking_for(ActorRole.vendor)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Only for items that need the vendor's approval, and only while the hold lasts."""
    return await _apply(
        request, session, booking, actor, BookingEvent.approve_vendor, None, clock, notifier
    )


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingOut,
    summary="Cancel or withdraw (renter or vendor)",
    responses=_ERRORS,
)
async def cancel_booking(
    request: Request,
    body: ReasonBody | None = None,
    booking: Booking = Depends(booking_for(*_PARTIES)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Before payment the renter withdraws for free; after it the renter pays the
    cancellation fee. A vendor must give a reason. The policy is applied here, on
    the server, never by the client."""
    return await _apply(
        request, session, booking, actor, BookingEvent.cancel, _reason(body), clock, notifier
    )


async def _handover(
    request: Request,
    session: AsyncSession,
    booking: Booking,
    actor: Actor,
    direction: HandoverDirection,
    body: HandoverSubmit,
    clock: Clock,
    notifier: Notifier,
) -> BookingOut:
    settings = _settings(request)
    updated = await submit_handover(
        session,
        booking.id,
        actor,
        direction,
        checklist=body.checklist,
        evidence_urls=[str(url) for url in body.evidence_urls],
        sign=body.sign,
        clock=clock,
        notifier=notifier,
        settings=settings,
        request_id=_request_id(request),
    )
    await session.commit()
    return await _out(session, updated, settings)


@router.post(
    "/{booking_id}/handover",
    response_model=BookingOut,
    summary="Fill in and sign the pickup handover (renter or vendor)",
    responses=_ERRORS,
)
async def handover_booking(
    body: HandoverSubmit,
    request: Request,
    booking: Booking = Depends(booking_for(*_PARTIES)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Once both parties have signed, the rental starts (``in_progress``)."""
    return await _handover(
        request, session, booking, actor, HandoverDirection.pickup, body, clock, notifier
    )


@router.post(
    "/{booking_id}/return",
    response_model=BookingOut,
    summary="Fill in and sign the return (renter or vendor)",
    responses=_ERRORS,
)
async def return_booking(
    body: HandoverSubmit,
    request: Request,
    booking: Booking = Depends(booking_for(*_PARTIES)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Once both parties have signed, the 24-hour claim window opens (``returned``)."""
    return await _handover(
        request, session, booking, actor, HandoverDirection.return_, body, clock, notifier
    )


@router.post(
    "/{booking_id}/claim",
    response_model=BookingOut,
    summary="Claim against the deposit (vendor)",
    responses=_ERRORS,
)
async def claim_booking(
    body: ClaimCreate,
    request: Request,
    booking: Booking = Depends(booking_for(ActorRole.vendor)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Within the claim window; the deposit and the payout are frozen for review."""
    payload = {
        "amount": body.amount,
        "description": body.description,
        "evidence": [str(url) for url in body.evidence_urls],
    }
    return await _apply(
        request, session, booking, actor, BookingEvent.claim_raised, payload, clock, notifier
    )


@router.post(
    "/{booking_id}/resolve",
    response_model=BookingOut,
    summary="Settle a disputed booking (admin)",
    responses=_ERRORS,
)
async def resolve_booking(
    body: Resolve,
    request: Request,
    booking: Booking = Depends(booking_for(ActorRole.admin)),
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> BookingOut:
    """Release, deduct (capped at the deposit) or forfeit; a reason is required."""
    payload: dict[str, Any] = {"decision": body.decision, "reason": body.reason}
    if body.amount is not None:
        payload["amount"] = body.amount
    return await _apply(
        request, session, booking, actor, BookingEvent.resolve, payload, clock, notifier
    )
