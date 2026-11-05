"""Shared API error envelope and domain exception types.

All error responses from the Vouch API use the shape::

    {
        "error": {
            "code": "resource_not_found",
            "message": "The requested resource was not found.",
            "details": {},
            "request_id": "req_..."
        }
    }

Error codes are stable machine-readable strings.  HTTP status codes follow the
contract defined in ``docs/API_CONTRACT.md``.

Only the error envelope, minimal exception classes, and exception handlers are
defined here.  Write-command-specific codes (409 idempotency_key_reused) are added
alongside the features that produce them.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Inner error object carried by every ``ErrorEnvelope``."""

    code: str = Field(..., description="Stable machine-readable error code.")
    message: str = Field(..., description="Human-readable summary.")
    details: dict = Field(default_factory=dict, description="Optional structured context.")
    request_id: str = Field(..., description="Echoes the X-Request-ID header value.")


class ErrorEnvelope(BaseModel):
    """Top-level error wrapper returned on all non-2xx responses."""

    error: ErrorDetail


class ValidationIssue(BaseModel):
    """One problem FastAPI found while validating the request."""

    type: str
    loc: list[str | int]
    msg: str
    input: object | None = None


class ValidationErrorResponse(BaseModel):
    """The 422 body FastAPI returns when a request fails validation."""

    detail: list[ValidationIssue]


# ---------------------------------------------------------------------------
# Domain exception classes
# ---------------------------------------------------------------------------


class BodyFieldError(Exception):
    """One request-body field is wrong in a way the schema cannot see.

    For example an unknown ``catalogue_item_id`` or a trust tier below the
    item's band default. Framework-free, so services can raise it; the app
    answers 422 in exactly FastAPI's ``{"detail": [...]}`` validation shape, so
    every 422 looks the same. Not a :class:`VouchAPIError`: it never uses the
    error envelope.
    """

    def __init__(self, field: str, type: str, msg: str, input: object = None) -> None:
        super().__init__(msg)
        self.field = field
        self.type = type
        self.msg = msg
        self.input = input

    def errors(self) -> list[dict[str, object]]:
        """The validation issues, as FastAPI's ``RequestValidationError.errors()``."""
        return [
            {"type": self.type, "loc": ("body", self.field), "msg": self.msg, "input": self.input}
        ]


class VouchAPIError(Exception):
    """Base for all application-level exceptions that map to HTTP responses."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."
    # Extra response headers (e.g. ``WWW-Authenticate`` on a 401); read-only.
    headers: ClassVar[Mapping[str, str]] = MappingProxyType({})

    def __init__(self, message: str | None = None, details: dict | None = None) -> None:
        super().__init__(message or self.message)
        self._message = message or self.message
        self._details = details or {}

    @property
    def detail_message(self) -> str:
        return self._message

    @property
    def error_details(self) -> dict:
        return self._details


class ResourceNotFoundError(VouchAPIError):
    """Raised when a resource is absent or hidden from the caller.

    Returns 404 ``resource_not_found``.  Never disclose whether the resource
    exists versus is simply inaccessible to this caller.
    """

    status_code = 404
    code = "resource_not_found"
    message = "The requested resource was not found."


class UnauthenticatedError(VouchAPIError):
    """Raised when the bearer token is missing, expired, tampered or malformed.

    Returns 401 ``unauthenticated`` with ``WWW-Authenticate: Bearer``. The message
    never says which check failed.
    """

    status_code = 401
    code = "unauthenticated"
    message = "Authentication is required."
    headers: ClassVar[Mapping[str, str]] = MappingProxyType({"WWW-Authenticate": "Bearer"})


class OtpRejectedError(UnauthenticatedError):
    """Raised when an OTP verify fails, for any reason (R26).

    No challenge, a wrong, expired, used or exhausted code, and an inactive or
    deleted account all get this one 401 and message, so the answer reveals
    nothing about the phone.
    """

    message = "The code is wrong or has expired."


class WebhookSignatureError(VouchAPIError):
    """Raised when a provider webhook's ``X-Vouch-Signature`` does not verify (R24).

    Returns 401 ``invalid_signature``. The event is still recorded, with
    ``signature_ok = false``, and nothing else happens.
    """

    status_code = 401
    code = "invalid_signature"
    message = "The webhook signature is invalid."


class PayloadTooLargeError(VouchAPIError):
    """Raised (by the body-size middleware) when a request body exceeds the cap.

    Returns 413 ``payload_too_large``.
    """

    status_code = 413
    code = "payload_too_large"
    message = "The request body is too large."


class KycUnavailableError(VouchAPIError):
    """Raised when the app has no KYC provider configured (staging/production in M2).

    Returns 503 ``kyc_unavailable``.
    """

    status_code = 503
    code = "kyc_unavailable"
    message = "Identity verification is not available right now."


class PaymentUnavailableError(VouchAPIError):
    """Raised when the app has no payment provider configured (staging/production in M2).

    Returns 503 ``payment_unavailable``: a booking cannot be accepted without the
    renter's payment account (R21).
    """

    status_code = 503
    code = "payment_unavailable"
    message = "Payments are not available right now."


class PaymentAccountFailedError(VouchAPIError):
    """Raised when the payment provider fails to open the renter's payment account.

    Returns 502 ``payment_provider_error``; nothing about the booking changes (R21).
    """

    status_code = 502
    code = "payment_provider_error"
    message = "The payment provider could not open a payment account. Try again."


class ForbiddenError(VouchAPIError):
    """Raised when an authenticated caller's role may not use this route.

    Returns 403 ``forbidden``.
    """

    status_code = 403
    code = "forbidden"
    message = "You do not have permission to do this."


class RateLimitedError(VouchAPIError):
    """Raised when a caller has made too many requests of one kind.

    Returns 429 ``rate_limited`` with ``Retry-After`` (whole seconds, at least 1).
    The message never says which limit was hit.
    """

    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Try again later."

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = max(1, int(retry_after))
        # Per instance (the wait differs per caller); read-only like the class default.
        self.headers = MappingProxyType({"Retry-After": str(self.retry_after)})


class OtpUnavailableError(VouchAPIError):
    """Raised when the app has no OTP sender configured (staging/production in M2).

    Returns 503 ``otp_unavailable``.
    """

    status_code = 503
    code = "otp_unavailable"
    message = "Phone login is not available right now."


class InvalidRequestError(VouchAPIError):
    """Raised for bad client input that does not fit schema validation.

    Returns 400 ``invalid_request``.
    """

    status_code = 400
    code = "invalid_request"
    message = "The request is invalid."


class CityNotLaunchedError(InvalidRequestError):
    """Raised when a request names a city that is not in the launched set.

    Unknown cities get the same answer, so the response reveals nothing about
    which cities exist but are not yet open.
    """

    code = "city_not_launched"
    message = "This city is not available yet."

    def __init__(self, city: str) -> None:
        super().__init__(details={"city": city})


class CursorDecodeError(InvalidRequestError):
    """Raised when a pagination cursor cannot be decoded."""

    code = "invalid_cursor"
    message = "The pagination cursor is invalid or has expired."


class InvalidTransition(VouchAPIError):
    """Raised when a booking event is not allowed from its current status.

    Also raised when the actor's role may not send the event, or the actor is not
    a party to the booking. Returns 409 ``invalid_transition`` with details
    ``{"from": <status>, "event": <event>}``.
    """

    status_code = 409
    code = "invalid_transition"
    message = "This booking cannot make that transition."

    def __init__(self, from_status: str | None, event: str, message: str | None = None) -> None:
        super().__init__(message, details={"from": from_status, "event": event})


LISTING_TRANSITION_MESSAGE = "This listing cannot make that transition."


class VendorNotActiveError(VouchAPIError):
    """Raised when a vendor whose profile is not ``active`` tries a vendor write.

    Every listing write (create, edit, submit, pause, resume, delete, adding or
    removing a block; amendment A23) and accepting a booking request need an
    ``active`` profile (M3a 4.4, 4.6, 4.7). Returns 409 ``vendor_not_active``.
    Reads, handovers and returns stay allowed.
    """

    status_code = 409
    code = "vendor_not_active"
    message = "Your vendor account is not active."


class ListingLockedError(VouchAPIError):
    """Raised when a vendor edits a listing, or a field, its status locks (M3a 4.5).

    Returns 409 ``listing_locked`` with ``details: {status}`` and, for an identity
    field outside ``draft``/``rejected``, ``details.fields``.
    """

    status_code = 409
    code = "listing_locked"
    message = "This listing cannot be edited in its current status."


class ListingIncompleteError(VouchAPIError):
    """Raised when a listing is submitted for review without everything it needs.

    Returns 409 ``listing_incomplete`` with ``details: {missing: [...]}``.
    """

    status_code = 409
    code = "listing_incomplete"
    message = "This listing is missing details needed for review."

    def __init__(self, missing: list[str]) -> None:
        super().__init__(details={"missing": missing})


class CatalogueItemInactiveError(VouchAPIError):
    """Raised when a listing names a catalogue item that is no longer active.

    Returns 409 ``catalogue_item_inactive``.
    """

    status_code = 409
    code = "catalogue_item_inactive"
    message = "This catalogue item is not available for new listings."


class DuplicateSerialError(VouchAPIError):
    """Raised when the vendor already lists this serial for this catalogue item.

    Translates the unique index ``uq_assets_vendor_item_serial``. Returns 409
    ``duplicate_serial``.
    """

    status_code = 409
    code = "duplicate_serial"
    message = "You already list a unit of this item with this serial number."


class HasLiveBookingsError(VouchAPIError):
    """Raised when a listing with a live or disputed booking is deleted.

    Returns 409 ``has_live_bookings``.
    """

    status_code = 409
    code = "has_live_bookings"
    message = "This listing has bookings in progress and cannot be deleted."


class DatesBlockedError(VouchAPIError):
    """Raised when a vendor's new block overlaps another block on the listing (M3a 4.6).

    Also the translation of the exclusion constraint
    ``ex_availability_windows_no_blocked_overlap``. Returns 409 ``dates_blocked``,
    the code a booking request on blocked dates gets too.
    """

    status_code = 409
    code = "dates_blocked"
    message = "These dates overlap another block on this listing."


class DatesUnavailableError(VouchAPIError):
    """Raised when a vendor's new block overlaps a live booking (M3a 4.6).

    Returns 409 ``dates_unavailable``, the code a booking request gets when another
    live booking holds the dates.
    """

    status_code = 409
    code = "dates_unavailable"
    message = "A live booking holds some of these dates."


_GUARD_MESSAGES = {
    "tier_insufficient": "Your trust tier does not allow booking this item.",
    "vendor_approval_required": "The vendor must approve this renter before accepting.",
    "dates_blocked": "The vendor has blocked some of these dates.",
    "dates_unavailable": "These dates are no longer available.",
    "renter_restricted": "This account cannot make bookings right now.",
    "hold_expired": "The request hold has expired.",
    "amount_mismatch": "The amount does not match the booking.",
    "self_booking": "You cannot book your own item.",
    "cancel_window_closed": "This booking can no longer be cancelled.",
    "claim_window_closed": "The claim window has closed.",
    "reason_required": "A reason is required.",
    "handover_incomplete": "Both parties have not signed the handover yet.",
    "handover_signed": "The item has been handed over; the booking can no longer be cancelled.",
    "approval_not_required": "This item does not need the vendor's approval of the renter.",
    "handover_already_signed": "You have already signed this handover record.",
    "no_show_too_early": "A no-show can only be reported from the start day of the rental.",
    "vendor_signed_handover": (
        "The vendor has signed the pickup handover; a no-show cannot be reported."
    ),
}


class BookingGuardFailed(VouchAPIError):
    """Raised when a legal booking transition fails one of its guards.

    Returns 409 with the guard's own code (``tier_insufficient``,
    ``dates_unavailable``, ``hold_expired``, ...).
    """

    status_code = 409
    code = "booking_guard_failed"
    message = "The booking cannot proceed."

    def __init__(self, code: str, details: dict | None = None) -> None:
        if code not in _GUARD_MESSAGES:
            raise ValueError(f"unknown booking guard code: {code!r}")
        super().__init__(_GUARD_MESSAGES[code], details)
        self.code = code
