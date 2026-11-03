"""The answer to a provider webhook (R24)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WebhookAck(BaseModel):
    """``processed``: recorded and acted on, or recorded with a ``processing_error``
    the provider should not retry. ``duplicate``: this event was seen before."""

    status: Literal["processed", "duplicate"]
    processing_error: str | None = Field(
        default=None,
        description=(
            "Why a genuine event was not applied (e.g. unsupported_event, "
            "amount_mismatch, payout_exceeds_payable); null when it was."
        ),
    )
