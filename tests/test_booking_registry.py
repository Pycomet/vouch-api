"""Shape of the booking transition registry and the booking error types (Task 3.2)."""

from __future__ import annotations

import pytest

from app.db.models.audit import ActorRole
from app.db.models.booking import BookingStatus
from app.schemas.errors import BookingGuardFailed, InvalidTransition
from app.services import booking as booking_service
from app.services.booking import BookingEvent


def test_requested_rows_registered_with_their_roles() -> None:
    table = booking_service._TRANSITIONS
    expected = {
        BookingEvent.accept: {ActorRole.vendor},
        BookingEvent.decline: {ActorRole.vendor},
        BookingEvent.approve_vendor: {ActorRole.vendor},
        BookingEvent.expire_hold: {ActorRole.system},
    }
    for event, roles in expected.items():
        rule = table[(BookingStatus.requested, event)]
        assert rule.roles == roles
        assert rule.party_only is True


def test_money_rows_registered_with_their_roles() -> None:
    table = booking_service._TRANSITIONS
    expected = {
        (BookingStatus.accepted, BookingEvent.deposit_captured): {ActorRole.system},
        (BookingStatus.accepted, BookingEvent.expire_deposit): {ActorRole.system},
        (BookingStatus.confirmed, BookingEvent.cancel): {ActorRole.renter, ActorRole.vendor},
    }
    for key, roles in expected.items():
        assert table[key].roles == roles
        assert table[key].party_only is True


def test_lifecycle_rows_registered_with_their_roles() -> None:
    table = booking_service._TRANSITIONS
    parties = {ActorRole.renter, ActorRole.vendor, ActorRole.system}
    expected = {
        (BookingStatus.confirmed, BookingEvent.handover_completed): parties,
        (BookingStatus.confirmed, BookingEvent.balance_captured): {ActorRole.system},
        (BookingStatus.in_progress, BookingEvent.return_completed): parties,
        (BookingStatus.returned, BookingEvent.claim_raised): {ActorRole.vendor},
        (BookingStatus.returned, BookingEvent.complete): {ActorRole.system},
        (BookingStatus.disputed, BookingEvent.resolve): {ActorRole.admin},
        (BookingStatus.in_progress, BookingEvent.flag_at_risk): {ActorRole.system},
        (BookingStatus.in_progress, BookingEvent.mark_lost): {ActorRole.admin},
    }
    for key, roles in expected.items():
        assert table[key].roles == roles
        assert table[key].party_only is True


def test_handover_incomplete_is_a_guard_code() -> None:
    error = BookingGuardFailed("handover_incomplete")

    assert (error.status_code, error.code) == (409, "handover_incomplete")


def test_handover_signed_is_a_guard_code() -> None:
    error = BookingGuardFailed("handover_signed")

    assert (error.status_code, error.code) == (409, "handover_signed")


def test_duplicate_registration_rejected() -> None:
    register = booking_service._register(
        BookingStatus.requested, BookingEvent.accept, ActorRole.vendor
    )

    async def handler(ctx):  # pragma: no cover - never called
        raise AssertionError

    with pytest.raises(ValueError, match="already registered"):
        register(handler)


def test_guard_failed_carries_its_code() -> None:
    error = BookingGuardFailed("dates_unavailable", {"asset_id": "x"})

    assert error.status_code == 409
    assert error.code == "dates_unavailable"
    assert error.error_details == {"asset_id": "x"}
    assert BookingGuardFailed.code == "booking_guard_failed"


def test_guard_failed_rejects_unknown_code() -> None:
    with pytest.raises(ValueError, match="unknown booking guard code"):
        BookingGuardFailed("nope")


def test_invalid_transition_details() -> None:
    error = InvalidTransition("requested", "cancel")

    assert error.status_code == 409
    assert error.code == "invalid_transition"
    assert error.error_details == {"from": "requested", "event": "cancel"}
