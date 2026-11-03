"""``POST /webhooks/{provider}``: events from a payment provider (R24).

No bearer token: the provider proves itself with ``X-Vouch-Signature``, an
HMAC-SHA256 (hex) of the raw body. ``{provider}`` must name the configured
payment provider (404 otherwise). The body is read raw, so the request size cap
(413) still applies before anything is parsed.

* bad or missing signature: the event is recorded with ``signature_ok = false``
  and the answer is 401 ``invalid_signature``;
* an event seen before: 200 ``{"status": "duplicate"}``, nothing else happens;
* otherwise the event is recorded and applied (:mod:`app.services.webhooks`):
  200 ``{"status": "processed"}``, with ``processing_error`` set when a genuine
  event cannot be applied, so the provider does not retry it forever.

The route commits in every case, the 401 included, so the record of a bad
delivery is kept.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_notifier
from app.api.examples import (
    INVALID_SIGNATURE,
    PAYLOAD_TOO_LARGE,
    UNKNOWN_PROVIDER,
    WEBHOOK_BODY_EXAMPLE,
    WEBHOOK_DUPLICATE_EXAMPLE,
    WEBHOOK_ERROR_EXAMPLE,
    WEBHOOK_PROCESSED_EXAMPLE,
    documented,
)
from app.core.clock import Clock
from app.db.session import get_session
from app.providers.payment import PaymentProvider
from app.providers.registry import payment_provider_named
from app.schemas.errors import WebhookSignatureError
from app.schemas.webhooks import WebhookAck
from app.services.notify import Notifier
from app.services.webhooks import handle_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _responses() -> dict[int | str, dict[str, Any]]:
    responses = documented(
        200, WEBHOOK_PROCESSED_EXAMPLE, INVALID_SIGNATURE, UNKNOWN_PROVIDER, PAYLOAD_TOO_LARGE, 422
    )
    # Three answers share the 200: name an example for each.
    responses[200] = {
        "description": "Recorded: processed, a duplicate, or not applicable.",
        "content": {
            "application/json": {
                "examples": {
                    "processed": {"summary": "processed", "value": WEBHOOK_PROCESSED_EXAMPLE},
                    "duplicate": {"summary": "duplicate", "value": WEBHOOK_DUPLICATE_EXAMPLE},
                    "processing_error": {
                        "summary": "processing_error",
                        "value": WEBHOOK_ERROR_EXAMPLE,
                    },
                }
            }
        },
    }
    return responses


def webhook_provider(provider: str, request: Request) -> PaymentProvider:
    """The configured payment provider if ``{provider}`` names it; 404 otherwise."""
    return payment_provider_named(provider, getattr(request.app.state, "payment_provider", None))


@router.post(
    "/{provider}",
    response_model=WebhookAck,
    summary="Receive a payment provider event",
    responses=_responses(),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"example": WEBHOOK_BODY_EXAMPLE}},
        }
    },
)
async def receive_webhook(
    request: Request,
    provider: PaymentProvider = Depends(webhook_provider),
    signature: str | None = Header(default=None, alias="X-Vouch-Signature"),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    notifier: Notifier = Depends(get_notifier),
) -> WebhookAck:
    """Payment captured or payout completed. Replays are answered ``duplicate``."""
    body = await request.body()
    result = await handle_webhook(
        session,
        provider,
        body,
        signature,
        clock=clock,
        notifier=notifier,
        settings=request.app.state.settings,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    if result.status == "invalid_signature":
        raise WebhookSignatureError()
    return WebhookAck(status=result.status, processing_error=result.processing_error)
