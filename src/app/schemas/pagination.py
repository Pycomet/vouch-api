"""Opaque cursor-based pagination helpers.

Cursors are base64-encoded JSON objects so the client never inspects the
internals.  The payload currently encodes ``created_at`` (ISO-8601) and ``id``
(UUID string) of the last item in a page, matching the deterministic ordering
used by inventory queries.

Security note: cursors are not signed.  A tampered cursor produces a decode
error (400) or returns an unexpected page — neither outcome is dangerous for a
read-only public endpoint.
"""

from __future__ import annotations

import base64
import datetime
import json
import uuid
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.schemas.errors import CursorDecodeError

T = TypeVar("T")

# Maximum items a caller may request per page.
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 20


class PageMeta(BaseModel):
    """Pagination metadata included in every collection response."""

    limit: int = Field(..., description="Items per page (≤100).")
    next_cursor: str | None = Field(
        None,
        description="Opaque cursor for the next page.  Absent when this is the last page.",
    )


class Page(BaseModel, Generic[T]):
    """Generic page wrapper for collection responses."""

    items: list[T]
    pagination: PageMeta


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def encode_cursor(created_at_iso: str, id_str: str) -> str:
    """Return a URL-safe base64 string encoding the page-position marker."""
    payload = json.dumps({"created_at": created_at_iso, "id": id_str}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime.datetime, uuid.UUID]:
    """Decode a cursor and return ``(created_at, id)`` as typed values.

    ``created_at`` must be an ISO-8601 string with an offset (every cursor the API
    issues has one) and ``id`` a UUID string. Raises :class:`CursorDecodeError` on
    any failure, including a well-formed cursor whose values are wrong, so callers
    receive a stable 400 response rather than an unhandled exception.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode() + b"==")
        data = json.loads(raw)
        created_at_iso, id_str = data["created_at"], data["id"]
        if not isinstance(created_at_iso, str) or not isinstance(id_str, str):
            raise TypeError("cursor values must be strings")
        created_at = datetime.datetime.fromisoformat(created_at_iso)
        if created_at.utcoffset() is None:
            raise ValueError("cursor timestamp has no offset")
        return created_at, uuid.UUID(id_str)
    except Exception as exc:
        raise CursorDecodeError() from exc
