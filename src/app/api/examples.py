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

from app.schemas.errors import BookingGuardFailed, ErrorEnvelope, ValidationErrorResponse

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
        }
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
