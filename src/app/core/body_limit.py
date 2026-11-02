"""Reject oversize request bodies with 413 ``payload_too_large``.

A pure ASGI middleware. A ``Content-Length`` above the cap is refused before the
body is read. A body without one (chunked) is counted as the app reads it and
refused as soon as it passes the cap, so an unbounded stream is never buffered.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import _safe_request_id
from app.schemas.errors import PayloadTooLargeError


class _TooLarge(BaseException):  # noqa: N818
    """Not an ``Exception``: FastAPI turns body-read errors into a 400, and this must
    unwind past it to :class:`BodyLimitMiddleware`."""


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = False
            if too_big:
                await self._reject(scope, headers, send)
                return

        seen = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if started:
                raise
            await self._reject(scope, headers, send)

    async def _reject(self, scope: Scope, headers: dict[bytes, bytes], send: Send) -> None:
        request_id = _safe_request_id(headers.get(b"x-request-id", b"").decode("latin-1") or None)
        exc = PayloadTooLargeError()
        body = json.dumps(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.detail_message,
                    "details": exc.error_details,
                    "request_id": request_id,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": exc.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-request-id", request_id.encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
