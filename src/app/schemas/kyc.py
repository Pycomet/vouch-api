"""Request and response bodies of ``POST /me/kyc`` (R27).

The selfie and the address document arrive base64-encoded, at most 2 MB each once
decoded; bad base64, an empty file or an oversize one is 422. ``selfie_b64`` and
``address_doc_b64`` hold the decoded bytes once validated. 422 bodies carry no
``input`` (see ``app.main._validation_response``), so nothing submitted is echoed.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from app.db.models.user import KYCStatus

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# 4 base64 characters per 3 bytes, padded: the longest text that can decode to the limit.
_MAX_B64_CHARS = 4 * ((MAX_DOCUMENT_BYTES + 2) // 3)


def _decode(value: object) -> bytes:
    """The decoded file: base64 is checked and decoded exactly once, here."""
    if not isinstance(value, str):
        raise ValueError("must be a base64 string")
    if len(value) > _MAX_B64_CHARS:
        raise ValueError("must be at most 2 MB once decoded")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("must be valid base64") from None
    if not raw:
        raise ValueError("must not be empty")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("must be at most 2 MB once decoded")
    return raw


# The model holds the decoded bytes; the wire format (and OpenAPI) is a base64 string.
Base64File = Annotated[
    bytes,
    BeforeValidator(_decode),
    WithJsonSchema({"type": "string", "description": "Base64 of the file, at most 2 MB decoded."}),
]


class KycRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "id_type": "nin",
                    "id_number": "12345678901",
                    "selfie_b64": "c2VsZmllLWJ5dGVz",
                    "address_doc_b64": "dXRpbGl0eS1iaWxsLWJ5dGVz",
                }
            ]
        },
    )

    id_type: Literal["nin", "bvn", "passport", "drivers_licence"]
    id_number: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{6,24}$")]
    selfie_b64: Base64File
    address_doc_b64: Base64File


class KycOut(BaseModel):
    tier: int = Field(description="0 or 1.")
    status: KYCStatus
