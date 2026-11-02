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

    def __init__(self, from_status: str | None, event: str) -> None:
        super().__init__(details={"from": from_status, "event": event})


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
