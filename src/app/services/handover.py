"""Handover and return records: each party fills in and signs its own side.

A booking has at most one ``handover_records`` row per direction (``pickup`` for
the handover, ``return`` for the return). :func:`submit_handover` upserts the
caller's side of it:

* the caller's checklist is stored under its role
  (``condition_checklist = {"vendor": {...}, "renter": {...}}``), so neither party
  can overwrite what the other recorded;
* the evidence URLs are added to the row's list (duplicates dropped, order kept),
  at most :data:`MAX_EVIDENCE_PER_RECORD` in all (more is a 400);
* with ``sign``, the caller's signer id and signing time are set. A side that has
  signed is final: submitting it again is refused (``handover_already_signed``).

The row's status follows the signatures (``draft``, ``vendor_signed``,
``renter_signed``, ``completed``). When both parties have signed it is
``completed``, which satisfies the table's CHECK (both signers, both times and a
non-empty evidence array), and the booking transition ``handover_completed`` or
``return_completed`` fires in the same transaction.

The booking row is locked first, so two parties signing at once are serialized.
The service flushes and never commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import Booking, HandoverRecord
from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.db.models.handover import HandoverDirection, HandoverStatus
from app.schemas.errors import (
    BookingGuardFailed,
    InvalidRequestError,
    InvalidTransition,
    ResourceNotFoundError,
)
from app.services.audit import Actor, record_audit
from app.services.booking import BookingEvent, transition
from app.services.notify import Notifier

AUDIT_ENTITY = "handover_record"
# The most evidence URLs one handover record keeps (both sides together).
MAX_EVIDENCE_PER_RECORD = 40

# direction -> (the booking status it needs, the event it fires once both signed)
_STEPS = {
    HandoverDirection.pickup: (BookingStatus.confirmed, BookingEvent.handover_completed),
    HandoverDirection.return_: (BookingStatus.in_progress, BookingEvent.return_completed),
}


def _party_side(actor: Actor, booking: Booking) -> str | None:
    """``"renter"`` or ``"vendor"`` when the actor is that party of the booking."""
    if actor.role == ActorRole.renter and actor.user_id == booking.renter_id:
        return "renter"
    if actor.role == ActorRole.vendor and actor.user_id == booking.vendor_id:
        return "vendor"
    return None


def _status(record: HandoverRecord) -> HandoverStatus:
    vendor, renter = record.vendor_signed_at is not None, record.renter_signed_at is not None
    if vendor and renter:
        return HandoverStatus.completed
    if vendor:
        return HandoverStatus.vendor_signed
    if renter:
        return HandoverStatus.renter_signed
    return HandoverStatus.draft


def _merge(existing: list | None, new: Sequence[str]) -> list[str]:
    merged = list(existing or [])
    merged.extend(url for url in new if url not in merged)
    return merged


async def submit_handover(
    session: AsyncSession,
    booking_id: uuid.UUID,
    actor: Actor,
    direction: HandoverDirection,
    *,
    checklist: dict[str, Any],
    evidence_urls: Sequence[str],
    sign: bool,
    clock: Clock,
    notifier: Notifier,
    settings: Settings,
    request_id: str | None = None,
) -> Booking:
    """Record the caller's side of the ``direction`` handover; flush, never commit.

    Raises :class:`ResourceNotFoundError` for an unknown booking,
    :class:`InvalidTransition` when the caller is not a party or the booking is not
    in the status this handover needs, and :class:`BookingGuardFailed`
    (``handover_already_signed``) when the caller's side is already signed.
    """
    required, event = _STEPS[direction]
    booking = await session.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if booking is None:
        raise ResourceNotFoundError()
    side = _party_side(actor, booking)
    if side is None or booking.status != required:
        raise InvalidTransition(booking.status.value, event.value)

    record = await session.scalar(
        select(HandoverRecord)
        .where(HandoverRecord.booking_id == booking.id, HandoverRecord.direction == direction)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if record is None:
        record = HandoverRecord(
            booking_id=booking.id,
            direction=direction,
            status=HandoverStatus.draft,
            condition_checklist={},
        )
        session.add(record)
    elif getattr(record, f"{side}_signed_at") is not None:
        raise BookingGuardFailed(
            "handover_already_signed", {"direction": direction.value, "side": side}
        )

    before = None if record.id is None else {"status": record.status}
    record.condition_checklist = {**(record.condition_checklist or {}), side: checklist}
    evidence = _merge(record.evidence_urls, evidence_urls)
    if len(evidence) > MAX_EVIDENCE_PER_RECORD:
        raise InvalidRequestError(
            "This handover record already holds the most evidence it can.",
            {"max_evidence_urls": MAX_EVIDENCE_PER_RECORD},
        )
    record.evidence_urls = evidence
    if sign:
        setattr(record, f"{side}_signer_id", actor.user_id)
        setattr(record, f"{side}_signed_at", clock.now())
    record.status = _status(record)
    await session.flush()

    await record_audit(
        session,
        actor=actor,
        entity=AUDIT_ENTITY,
        entity_id=record.id,
        action="handover.signed" if sign else "handover.updated",
        before=before,
        after={
            "booking_id": booking.id,
            "direction": direction,
            "side": side,
            "status": record.status,
            "evidence_urls": record.evidence_urls,
        },
        request_id=request_id,
    )

    if record.status == HandoverStatus.completed:
        booking = await transition(
            session,
            booking.id,
            actor,
            event,
            clock=clock,
            notifier=notifier,
            settings=settings,
            request_id=request_id,
        )
    return booking
