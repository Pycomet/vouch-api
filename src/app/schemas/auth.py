"""Request and response bodies of the phone OTP endpoints (spec 4, R26).

``phone`` accepts a Nigerian number written ``0XXXXXXXXXX``, ``234XXXXXXXXXX`` or
``+234XXXXXXXXXX`` and is normalized to ``+234XXXXXXXXXX``; anything else is 422.
The bodies refuse unknown fields, so a client cannot send a ``role``: the token's
role always comes from the stored user.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.services.auth import normalize_phone

PHONE_EXAMPLE = "08031234567"

Phone = Annotated[
    str,
    AfterValidator(normalize_phone),
    Field(
        description="A Nigerian number: 0XXXXXXXXXX, 234XXXXXXXXXX or +234XXXXXXXXXX.",
        examples=[PHONE_EXAMPLE],
    ),
]
OtpCode = Annotated[str, Field(pattern=r"^[0-9]{6}$", description="The 6-digit code sent.")]
FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class OtpRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"phone": PHONE_EXAMPLE}]}
    )

    phone: Phone


class OtpVerify(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"phone": PHONE_EXAMPLE, "code": "482913", "full_name": "Ada Obi"}]
        },
    )

    phone: Phone
    code: OtpCode
    full_name: FullName | None = Field(
        default=None, description="Used only when this login creates the account."
    )


class OtpSent(BaseModel):
    """Always the same body, whether or not the phone has an account."""

    status: Literal["sent"] = "sent"


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires.")
