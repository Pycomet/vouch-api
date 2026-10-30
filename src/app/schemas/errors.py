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
defined here.  Auth-specific (401/403) and write-command-specific (409
idempotency_key_reused) codes are added alongside the features that produce them.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Domain exception classes
# ---------------------------------------------------------------------------


class VouchAPIError(Exception):
    """Base for all application-level exceptions that map to HTTP responses."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

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
