"""Request and response bodies of ``POST /me/kyc`` (R27).

The selfie and the address document arrive base64-encoded, at most 2 MB each once
decoded; bad base64, an empty file or an oversize one is 422. Error messages never
echo the submitted values.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.db.models.user import KYCStatus

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# 4 base64 characters per 3 bytes, padded: the longest text that can decode to the limit.
_MAX_B64_CHARS = 4 * ((MAX_DOCUMENT_BYTES + 2) // 3)


def _decodable(value: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("must be valid base64") from None
    if not raw:
        raise ValueError("must not be empty")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("must be at most 2 MB once decoded")
    return value


Base64File = Annotated[
    str,
    Field(max_length=_MAX_B64_CHARS, description="Base64 of the file, at most 2 MB decoded."),
    AfterValidator(_decodable),
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

    @property
    def selfie(self) -> bytes:
        return base64.b64decode(self.selfie_b64)

    @property
    def address_doc(self) -> bytes:
        return base64.b64decode(self.address_doc_b64)


class KycOut(BaseModel):
    tier: int = Field(description="0 or 1.")
    status: KYCStatus
