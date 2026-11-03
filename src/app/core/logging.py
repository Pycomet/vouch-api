"""Application logging and request-correlation middleware."""

from __future__ import annotations

import logging
import re
import time
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint

logger = logging.getLogger("vouch.http")

# Valid request-ID: 1–128 printable ASCII characters (no control chars).
_REQUEST_ID_RE = re.compile(r"^[\x21-\x7E]{1,128}$")


def _safe_request_id(header_value: str | None) -> str:
    """Return the caller's request ID if valid, otherwise generate a UUID."""
    if header_value and _REQUEST_ID_RE.match(header_value):
        return header_value
    return str(uuid4())


def get_logger(name: str) -> logging.Logger:
    """A project logger under the ``vouch`` namespace (``vouch.<name>``)."""

    return logging.getLogger(f"vouch.{name}")


def configure_logging(debug: bool = False) -> None:
    """Configure a predictable process-wide logging baseline."""

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


async def request_context_middleware(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """Attach a correlation request ID and emit one completion log per request.

    The ``X-Request-ID`` header is honoured when valid (1–128 printable ASCII).
    Invalid or absent values are replaced with a generated UUID.  The resolved
    ID is written to ``request.state.request_id`` and echoed in the response
    header so both sides share the same trace token.
    """

    request_id = _safe_request_id(request.headers.get("X-Request-ID"))
    request.state.request_id = request_id
    started_at = time.perf_counter()

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "request_completed method=%s path=%s status_code=%s duration_ms=%s request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request_id,
    )
    return response
