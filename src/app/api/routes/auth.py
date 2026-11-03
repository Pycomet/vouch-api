"""Phone OTP login (spec 4, R25, R26).

``POST /auth/otp/request`` always answers 202 ``{"status": "sent"}``, whether or
not the phone has an account; more than five requests for one phone in a rolling
hour get 429 ``rate_limited`` with ``Retry-After``. ``POST /auth/otp/verify``
exchanges the code for an access token whose role is the stored user's
(``users.role``), never anything the client sent. Every failed verify is the same
401 ``unauthenticated``.

The verify route commits even when the code is wrong, so the attempt counts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_otp_sender
from app.api.examples import (
    OTP_REJECTED,
    OTP_SENT_EXAMPLE,
    OTP_UNAVAILABLE,
    TOKEN_EXAMPLE,
    documented,
)
from app.core.clock import Clock
from app.core.config import Settings
from app.core.security import issue_token
from app.db.models import ActorRole
from app.db.session import get_session
from app.providers.otp import OtpSender
from app.schemas.auth import OtpRequest, OtpSent, OtpVerify, TokenOut
from app.schemas.errors import OtpRejectedError
from app.services.auth import request_otp, verify_otp

router = APIRouter(prefix="/auth/otp", tags=["auth"])


def _request_responses() -> dict[int | str, dict[str, Any]]:
    responses = documented(status.HTTP_202_ACCEPTED, OTP_SENT_EXAMPLE, 429, OTP_UNAVAILABLE, 422)
    responses[429]["headers"] = {
        "Retry-After": {
            "description": "Seconds until another request for this phone is allowed.",
            "schema": {"type": "integer", "minimum": 1},
        }
    }
    return responses


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post(
    "/request",
    response_model=OtpSent,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a login code to a phone",
    responses=_request_responses(),
)
async def request_code(
    body: OtpRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
    sender: OtpSender = Depends(get_otp_sender),
) -> OtpSent:
    """Send a 6-digit code, valid for 10 minutes. The answer never says whether the
    phone has an account."""
    await request_otp(session, body.phone, sender=sender, clock=clock, settings=_settings(request))
    await session.commit()
    return OtpSent()


@router.post(
    "/verify",
    response_model=TokenOut,
    summary="Exchange a login code for an access token",
    responses=documented(status.HTTP_200_OK, TOKEN_EXAMPLE, OTP_REJECTED, 422),
)
async def verify_code(
    body: OtpVerify,
    request: Request,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> TokenOut:
    """A first login creates the account (a renter). Five wrong codes use the code up."""
    settings = _settings(request)
    user = await verify_otp(
        session, body.phone, body.code, full_name=body.full_name, clock=clock, settings=settings
    )
    # Commit either way: a wrong guess must stay counted.
    await session.commit()
    if user is None:
        raise OtpRejectedError()
    token = issue_token(user.id, ActorRole(user.role.value), settings)
    return TokenOut(access_token=token, expires_in=settings.jwt_ttl_minutes * 60)
