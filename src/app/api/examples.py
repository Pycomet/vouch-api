"""OpenAPI response examples for every ``/api/v1`` route (Step 4 done-when).

``documented(status, example, *errors)`` builds a route's ``responses=`` mapping:
the success status with ``example``, and each error status with an example of the
error envelope (``ErrorEnvelope``) the API really returns. An error is either a
bare status (its usual code, see :data:`_DEFAULT_ERRORS`) or an :class:`Err` with
a specific code; several errors on one status become named ``examples`` (keyed by
the error's ``name``, or its code). :func:`guard` builds the 409 for a booking
guard code with the message the API really sends. 422 is FastAPI's
request-validation body (:class:`ValidationErrorResponse`).

The example payloads are sample data only (a listing priced in NGN, in a
placeholder city slug); the code never reads them. Request bodies carry their own
examples in the request models' ``json_schema_extra``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.errors import (
    LISTING_TRANSITION_MESSAGE,
    BookingGuardFailed,
    CatalogueItemInactiveError,
    DatesBlockedError,
    DatesUnavailableError,
    DuplicateSerialError,
    ErrorEnvelope,
    HasLiveBookingsError,
    InvalidTransition,
    ListingIncompleteError,
    ListingLockedError,
    OtpRejectedError,
    ValidationErrorResponse,
    VendorNotActiveError,
    VouchAPIError,
)

REQUEST_ID = "4b9f7c1e-2d3a-4e5f-8a6b-7c8d9e0f1a2b"


@dataclass(frozen=True)
class Err:
    """One documented error: its HTTP status, code, message and details."""

    status: int
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    description: str | None = None
    # The key among several examples on one status (defaults to the code).
    name: str | None = None

    @property
    def key(self) -> str:
        return self.name or self.code


def guard(code: str, details: dict[str, Any] | None = None, description: str = "") -> Err:
    """The 409 a booking guard ``code`` returns, with the API's own message."""
    error = BookingGuardFailed(code, details)
    return Err(409, code, error.detail_message, error.error_details, description or None)


def raised(
    error: type[VouchAPIError] | VouchAPIError,
    details: dict[str, Any] | None = None,
    description: str = "",
    name: str | None = None,
) -> Err:
    """The error an exception class (or instance) returns, with its own message."""
    instance = error if isinstance(error, VouchAPIError) else error(details=details)
    return Err(
        instance.status_code,
        instance.code,
        instance.detail_message,
        details if details is not None else instance.error_details,
        description or None,
        name,
    )


_DEFAULT_ERRORS: dict[int, Err] = {
    400: Err(400, "invalid_request", "The request is invalid.", description="Bad input."),
    401: Err(
        401,
        "unauthenticated",
        "Authentication is required.",
        description="Missing or invalid bearer token.",
    ),
    403: Err(
        403,
        "forbidden",
        "You do not have permission to do this.",
        description="Your role may not take this action.",
    ),
    404: Err(
        404,
        "resource_not_found",
        "The requested resource was not found.",
        description="Not found, or not yours to see.",
    ),
    429: Err(
        429,
        "rate_limited",
        "Too many requests. Try again later.",
        description="Too many requests; retry after the seconds in Retry-After.",
    ),
    409: Err(
        409,
        "invalid_transition",
        "This booking cannot make that transition.",
        {"from": "completed", "event": "cancel"},
        description="Not allowed in the booking's current state.",
    ),
}

_VALIDATION_EXAMPLE = {
    "detail": [
        {
            "type": "value_error",
            "loc": ["body", "amount"],
            "msg": 'Value error, must be a string amount such as "150000.00"',
            "input": 150000,
        }
    ]
}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """An ``ErrorEnvelope`` as the API returns it."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": REQUEST_ID,
        }
    }


def _error_entries(errors: tuple[int | Err, ...]) -> dict[int | str, dict[str, Any]]:
    grouped: dict[int, list[Err]] = {}
    out: dict[int | str, dict[str, Any]] = {}
    for item in errors:
        if item == 422:
            out[422] = {
                "model": ValidationErrorResponse,
                "description": "The request failed validation.",
                "content": {"application/json": {"example": _VALIDATION_EXAMPLE}},
            }
            continue
        err = item if isinstance(item, Err) else _DEFAULT_ERRORS[item]
        grouped.setdefault(err.status, []).append(err)
    for status, errs in grouped.items():
        if len(errs) == 1:
            content = {"example": error_body(errs[0].code, errs[0].message, errs[0].details)}
        else:
            content = {
                "examples": {
                    err.key: {
                        "summary": err.key,
                        "value": error_body(err.code, err.message, err.details),
                    }
                    for err in errs
                }
            }
        description = "; ".join(err.description or err.message for err in errs)
        out[status] = {
            "model": ErrorEnvelope,
            "description": description,
            "content": {"application/json": content},
        }
    return out


def documented(status: int, example: Any, *errors: int | Err) -> dict[int | str, dict[str, Any]]:
    """``responses=`` for a route: the success example and each error's example."""
    return {
        status: {"content": {"application/json": {"example": example}}},
        **_error_entries(errors),
    }


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

ASSET_ID = "4f0c2d8e-5b1a-4c3e-9d7f-2a6b8c0e1f23"
BOOKING_ID = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"
RENTER_ID = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
VENDOR_ID = "6e7f8a9b-0c1d-4e2f-9a3b-4c5d6e7f8a9b"

PUBLIC_ASSET_EXAMPLE: dict[str, Any] = {
    "id": ASSET_ID,
    "name": None,
    "display_name": "Sony FX3",
    "make": "Sony",
    "model": "FX3",
    "description": "Full-frame cinema body with two batteries and a 128 GB card.",
    "category": "camera",
    "condition_grade": "excellent",
    "daily_rate": "50000.00",
    "weekend_rate": "90000.00",
    "weekly_rate": "250000.00",
    "deposit_amount": "150000.00",
    "currency": "NGN",
    "city": "city-slug",
    "is_available": True,
    "minimum_trust_tier": 2,
    "created_at": "2026-10-20T09:15:00Z",
    "updated_at": "2026-10-28T16:40:00Z",
}

PAGE_EXAMPLE: dict[str, Any] = {
    "items": [PUBLIC_ASSET_EXAMPLE],
    "pagination": {"limit": 20, "next_cursor": None},
}

SEARCH_EXAMPLE: dict[str, Any] = {**PAGE_EXAMPLE, "equivalents": False}

MONEY_LINE_EXAMPLE: dict[str, Any] = {
    "deposit": {"amount": "150000.00", "status": "held", "held_by": "platform"},
    "contribution": {"amount": "7500.00", "status": "received"},
    "rental": {"amount": "150000.00", "status": "received"},
    "next": {"label": "Handover Fri 13 Nov", "amount_due": "0.00"},
}

REQUESTED_MONEY_LINE: dict[str, Any] = {
    "deposit": {"amount": "150000.00", "status": "due", "held_by": "platform"},
    "contribution": {"amount": "7500.00", "status": "due"},
    "rental": {"amount": "150000.00", "status": "due"},
    "next": {"label": "Awaiting vendor response", "amount_due": "0.00"},
}

BOOKING_EXAMPLE: dict[str, Any] = {
    "id": BOOKING_ID,
    "status": "confirmed",
    "asset_id": ASSET_ID,
    "renter_id": RENTER_ID,
    "vendor_id": VENDOR_ID,
    "starts_on": "2026-11-13",
    "ends_on": "2026-11-15",
    "fulfilment_choice": "pickup",
    "hold_expires_at": None,
    "deposit_due_at": "2026-11-02T21:00:00Z",
    "claim_window_ends_at": None,
    "vendor_approved_at": None,
    "price": {
        "rental_total": "150000.00",
        "delivery_fee": "0.00",
        "deposit": "150000.00",
        "damage_contribution": "7500.00",
        "currency": "NGN",
    },
    "money_line": MONEY_LINE_EXAMPLE,
    "payment_instructions": None,
}

# A paid renter cancellation: refund + cancellation fee == everything paid.
CANCELLED_MONEY_LINE: dict[str, Any] = {
    "deposit": {"amount": "150000.00", "status": "released", "held_by": "platform"},
    "contribution": {"amount": "7500.00", "status": "refunded"},
    "rental": {"amount": "150000.00", "status": "refunded"},
    "cancellation_fee": {
        "amount": "15000.00",
        "vendor_part": "7500.00",
        "platform_part": "7500.00",
    },
    "refund": {"amount": "292500.00"},
    "next": {"label": "Closed", "amount_due": "0.00"},
}

CANCELLED_BOOKING_EXAMPLE: dict[str, Any] = {
    **BOOKING_EXAMPLE,
    "status": "cancelled_by_renter",
    "money_line": CANCELLED_MONEY_LINE,
}

# A renter-reported vendor no-show (A17): everything paid comes back, no fee.
NO_SHOW_MONEY_LINE: dict[str, Any] = {
    "deposit": {"amount": "150000.00", "status": "released", "held_by": "platform"},
    "contribution": {"amount": "7500.00", "status": "refunded"},
    "rental": {"amount": "150000.00", "status": "refunded"},
    "refund": {"amount": "307500.00"},
    "next": {"label": "Closed", "amount_due": "0.00"},
}

NO_SHOW_BOOKING_EXAMPLE: dict[str, Any] = {
    **BOOKING_EXAMPLE,
    "status": "cancelled_by_vendor",
    "money_line": NO_SHOW_MONEY_LINE,
}

# Closed before any payment (A18): every part is "cancelled" and nothing is owed.
CLOSED_UNPAID_MONEY_LINE: dict[str, Any] = {
    "deposit": {"amount": "150000.00", "status": "cancelled", "held_by": "platform"},
    "contribution": {"amount": "7500.00", "status": "cancelled"},
    "rental": {"amount": "150000.00", "status": "cancelled"},
    "next": {"label": "Closed", "amount_due": "0.00"},
}

# Expired unpaid, then the payment arrived late: it was never applied, so every
# part stays "cancelled", and the whole amount is listed as returned.
LATE_PAYMENT_MONEY_LINE: dict[str, Any] = {
    **CLOSED_UNPAID_MONEY_LINE,
    "returned_payments": [{"amount": "307500.00", "reason": "late_payment"}],
}

DECLINED_BOOKING_EXAMPLE: dict[str, Any] = {
    **BOOKING_EXAMPLE,
    "status": "declined",
    "hold_expires_at": "2026-11-02T13:00:00Z",
    "deposit_due_at": None,
    "money_line": CLOSED_UNPAID_MONEY_LINE,
}

REQUESTED_BOOKING_EXAMPLE: dict[str, Any] = {
    **BOOKING_EXAMPLE,
    "status": "requested",
    "hold_expires_at": "2026-11-02T13:00:00Z",
    "deposit_due_at": None,
    "money_line": REQUESTED_MONEY_LINE,
}

# The renter's view of an accepted booking: the whole payment is due, and where
# to pay it (R21). The vendor (who accepts) and admins get null instructions.
PAYMENT_INSTRUCTIONS_EXAMPLE: dict[str, Any] = {
    "bank": "Fake Bank",
    "account_number": "0123456789",
    "reference": "fake-booking-1c2d3e4f5a6b4c7d8e9f0a1b2c3d4e5f",
    "amount": "307500.00",
    "due_by": "2026-11-02T21:00:00Z",
}
_ACCEPTED_MONEY_LINE: dict[str, Any] = {
    **REQUESTED_MONEY_LINE,
    "next": {"label": "Payment due Mon 2 Nov 22:00", "amount_due": "307500.00"},
}
ACCEPTED_BOOKING_EXAMPLE: dict[str, Any] = {
    **BOOKING_EXAMPLE,
    "status": "accepted",
    "money_line": _ACCEPTED_MONEY_LINE,
}
RENTER_ACCEPTED_BOOKING_EXAMPLE: dict[str, Any] = {
    **ACCEPTED_BOOKING_EXAMPLE,
    "payment_instructions": PAYMENT_INSTRUCTIONS_EXAMPLE,
}

_CARD: dict[str, Any] = {
    "id": BOOKING_ID,
    "status": "confirmed",
    "asset_id": ASSET_ID,
    "asset_name": "Sony FX3",
    "starts_on": "2026-11-13",
    "ends_on": "2026-11-15",
    "hold_expires_at": None,
    "money_line": MONEY_LINE_EXAMPLE,
}

RENTER_HOME_EXAMPLE: dict[str, Any] = {
    "active": _CARD,
    "past": [
        {
            **_CARD,
            "id": "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
            "status": "completed",
            "starts_on": "2026-10-02",
            "ends_on": "2026-10-04",
            "money_line": {
                "deposit": {"amount": "150000.00", "status": "released", "held_by": "platform"},
                "contribution": {"amount": "7500.00", "status": "received"},
                "rental": {"amount": "150000.00", "status": "paid_out"},
                "next": {"label": "Closed", "amount_due": "0.00"},
            },
        },
        {
            **_CARD,
            "id": "4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a",
            "status": "expired",
            "starts_on": "2026-10-16",
            "ends_on": "2026-10-18",
            "money_line": LATE_PAYMENT_MONEY_LINE,
        },
    ],
}

VENDOR_HOME_EXAMPLE: dict[str, Any] = {
    "requests": [
        {
            **_CARD,
            "id": "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f",
            "status": "requested",
            "starts_on": "2026-11-20",
            "ends_on": "2026-11-22",
            "hold_expires_at": "2026-11-02T13:00:00Z",
            "money_line": REQUESTED_MONEY_LINE,
        }
    ],
    "today": {"handovers": [_CARD], "returns": []},
    "next_payout": "127500.00",
    "utilisation_7d": "0.43",
}

CATALOGUE_ITEMS_EXAMPLE: dict[str, Any] = {
    "items": [
        {
            "id": "7b8c9d0e-1f2a-4b3c-8d4e-5f6a7b8c9d0e",
            "category": "camera",
            "make": "Sony",
            "model": "FX3",
            "mount": "E",
            "specs": {},
            "replacement_value_band": "C",
        }
    ],
    "pagination": {"limit": 20, "next_cursor": None},
}

UNKNOWN_CATEGORY = Err(
    400,
    "invalid_request",
    "Unknown category.",
    {"category": "no-such-category"},
    description="The category slug does not exist.",
)

VENDOR_PROFILE_EXAMPLE: dict[str, Any] = {
    "status": "active",
    "business_type": "rental_house",
    "is_sub_broker": False,
    "business_name": "Example Rentals Ltd",
    "bank_name": "Example Bank",
    "account_name": "EXAMPLE RENTALS LTD",
    "account_last4": "4321",
    "has_recipient_code": True,
}

# The vendor's own listings (M3a 4.4).
OWN_ASSET_EXAMPLE: dict[str, Any] = {
    "id": ASSET_ID,
    "catalogue_item_id": "7b8c9d0e-1f2a-4b3c-8d4e-5f6a7b8c9d0e",
    "make": "Sony",
    "model": "FX3",
    "category": "camera",
    "city": "city-slug",
    "serial_number": "SN-0001",
    "condition_grade": "excellent",
    "condition_notes": "Light wear on the grip.",
    "description": "Full-frame cinema body with two batteries.",
    "daily_rate": "50000.00",
    "weekend_rate": "90000.00",
    "weekly_rate": None,
    "deposit_amount": "150000.00",
    "currency": "NGN",
    "included_accessories": ["2 batteries", "128 GB card"],
    "fulfilment": {"pickup": True, "delivery": True, "delivery_fee": "5000.00"},
    "listing_status": "draft",
    "minimum_trust_tier": 3,
    "paused_by": None,
    "review_reason": None,
    "reviewed_at": None,
    "is_available": True,
    "created_at": "2026-11-05T09:15:00Z",
    "updated_at": "2026-11-05T09:15:00Z",
}
OWN_ASSET_PAGE_EXAMPLE: dict[str, Any] = {
    "items": [OWN_ASSET_EXAMPLE],
    "pagination": {"limit": 20, "next_cursor": None},
}
REVIEW_ASSET_EXAMPLE: dict[str, Any] = {**OWN_ASSET_EXAMPLE, "listing_status": "review"}
PAUSED_ASSET_EXAMPLE: dict[str, Any] = {
    **OWN_ASSET_EXAMPLE,
    "listing_status": "paused",
    "paused_by": "vendor",
}
ACTIVE_ASSET_EXAMPLE: dict[str, Any] = {**OWN_ASSET_EXAMPLE, "listing_status": "active"}

LISTING_LOCKED = raised(
    ListingLockedError, {"status": "review"}, "Locked while in review.", name="listing_locked"
)
IDENTITY_LOCKED = raised(
    ListingLockedError,
    {"status": "active", "fields": ["serial_number"]},
    "An identity field outside draft or rejected.",
    name="identity_locked",
)
LISTING_INCOMPLETE = raised(
    ListingIncompleteError(["serial_number", "delivery_fee"]),
    description="Something the review needs is missing.",
)
CATALOGUE_ITEM_INACTIVE = raised(
    CatalogueItemInactiveError,
    {"catalogue_item_id": "7b8c9d0e-1f2a-4b3c-8d4e-5f6a7b8c9d0e"},
    "The catalogue item is no longer active.",
)
DUPLICATE_SERIAL = raised(DuplicateSerialError, description="You already list this serial.")
HAS_LIVE_BOOKINGS = raised(HasLiveBookingsError, description="A booking still holds the unit.")


# Availability blocks (M3a 4.6).
BLOCK_EXAMPLE: dict[str, Any] = {
    "id": "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a",
    "asset_id": ASSET_ID,
    "starts_on": "2026-12-24",
    "ends_on": "2026-12-27",
    "created_at": "2026-11-05T09:15:00Z",
}
BLOCK_LIST_EXAMPLE: dict[str, Any] = {"items": [BLOCK_EXAMPLE]}
BLOCK_DATES_INVALID = Err(
    400,
    "invalid_request",
    "The start date must not be in the past.",
    description="The start is after the end, or before today (business zone).",
)
BLOCK_OVERLAPS_BLOCK = raised(DatesBlockedError, description="Another block overlaps these dates.")
BLOCK_OVERLAPS_BOOKING = raised(
    DatesUnavailableError, description="A live booking holds some of these dates."
)


def listing_transition(from_status: str, event: str, description: str) -> Err:
    """The 409 ``invalid_transition`` an illegal listing event returns."""
    return raised(
        InvalidTransition(from_status, event, message=LISTING_TRANSITION_MESSAGE),
        description=description,
    )


LIVE_EXAMPLE: dict[str, Any] = {"status": "ok", "service": "Vouch API", "version": "0.1.0"}
READY_EXAMPLE: dict[str, Any] = {**LIVE_EXAMPLE, "checks": {"database": "ok"}}
NOT_READY_EXAMPLE: dict[str, Any] = {
    **LIVE_EXAMPLE,
    "status": "unavailable",
    "checks": {"database": "unavailable"},
}

CITY_NOT_LAUNCHED = Err(
    400,
    "city_not_launched",
    "This city is not available yet.",
    {"city": "other-city-slug"},
    description="The city is not launched.",
)
INVALID_DATES = Err(
    400,
    "invalid_request",
    "Both dates are required together for date filtering.",
    {"from": "2026-11-13"},
    description="Dates missing or reversed.",
)
INVALID_CURSOR = Err(
    400,
    "invalid_cursor",
    "The pagination cursor is invalid or has expired.",
    description="Bad cursor.",
)

# Phone OTP (R26).
OTP_SENT_EXAMPLE: dict[str, Any] = {"status": "sent"}
TOKEN_EXAMPLE: dict[str, Any] = {
    # A placeholder, deliberately not JWT-shaped, so secret scanners don't flag the docs.
    "access_token": "<signed-jwt>",
    "token_type": "bearer",
    "expires_in": 604800,
}
OTP_UNAVAILABLE = Err(
    503,
    "otp_unavailable",
    "Phone login is not available right now.",
    description="No OTP sender is configured.",
)
OTP_REJECTED = Err(
    401,
    "unauthenticated",
    OtpRejectedError.message,
    description="Wrong, expired, exhausted or used code, or no code was requested.",
)

# KYC (R27).
KYC_EXAMPLE: dict[str, Any] = {"tier": 1, "status": "verified"}
KYC_UNAVAILABLE = Err(
    503,
    "kyc_unavailable",
    "Identity verification is not available right now.",
    description="No KYC provider is configured.",
)

# Payments (R21).
PAYMENT_UNAVAILABLE = Err(
    503,
    "payment_unavailable",
    "Payments are not available right now.",
    description="No payment provider is configured; the booking stays requested.",
)
# Provider webhooks (R24).
WEBHOOK_PROCESSED_EXAMPLE: dict[str, Any] = {"status": "processed", "processing_error": None}
WEBHOOK_DUPLICATE_EXAMPLE: dict[str, Any] = {"status": "duplicate", "processing_error": None}
WEBHOOK_ERROR_EXAMPLE: dict[str, Any] = {
    "status": "processed",
    "processing_error": "amount_mismatch",
}
WEBHOOK_BODY_EXAMPLE: dict[str, Any] = {
    "id": "evt_payment_captured_fake-booking-9a1b2c3d4e5f4a6b8c7d0e1f2a3b4c5d",
    "type": "payment_captured",
    "data": {
        "booking_id": BOOKING_ID,
        "reference": "fake-booking-9a1b2c3d4e5f4a6b8c7d0e1f2a3b4c5d",
        "amount": "312500.00",
    },
}
INVALID_SIGNATURE = Err(
    401,
    "invalid_signature",
    "The webhook signature is invalid.",
    description="X-Vouch-Signature missing or wrong; the event is recorded and ignored.",
)
UNKNOWN_PROVIDER = Err(
    404,
    "resource_not_found",
    "The requested resource was not found.",
    description="No such payment provider.",
)
PAYLOAD_TOO_LARGE = Err(
    413,
    "payload_too_large",
    "The request body is too large.",
    description="The body is over the size cap.",
)
VENDOR_NOT_ACTIVE = raised(
    VendorNotActiveError,
    {"status": "suspended"},
    "Your vendor profile is pending or suspended.",
)

PAYMENT_PROVIDER_FAILED = Err(
    502,
    "payment_provider_error",
    "The payment provider could not open a payment account. Try again.",
    description="The provider failed to open the virtual account; the booking stays requested.",
)
