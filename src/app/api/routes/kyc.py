"""``POST /me/kyc`` (renter): verify identity and address through the KYC provider (R27).

Answers 200 ``{tier, status}``: ``verified`` at tier 1 when both checks pass,
``failed`` at tier 0 otherwise (the renter may try again). Re-posting after a
success is an idempotent 200. 503 ``kyc_unavailable`` when the app has no provider.
The documents and the id number are never stored or logged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, get_kyc_provider, require_role
from app.api.examples import KYC_EXAMPLE, KYC_UNAVAILABLE, documented
from app.core.clock import Clock
from app.db.models import ActorRole
from app.db.session import get_session
from app.providers.kyc import KycProvider
from app.schemas.kyc import KycOut, KycRequest
from app.services.audit import Actor
from app.services.kyc import apply_kyc

router = APIRouter(tags=["kyc"])


@router.post(
    "/me/kyc",
    response_model=KycOut,
    summary="Verify the renter's identity and address",
    responses=documented(200, KYC_EXAMPLE, 401, 403, KYC_UNAVAILABLE, 422),
)
async def submit_kyc(
    body: KycRequest,
    request: Request,
    actor: Actor = Depends(require_role(ActorRole.renter)),
    session: AsyncSession = Depends(get_session),
    provider: KycProvider = Depends(get_kyc_provider),
    clock: Clock = Depends(get_clock),
) -> KycOut:
    """Tier 1 needs both the identity check and the address check to pass."""
    profile = await apply_kyc(
        session,
        actor.user_id,
        provider,
        clock=clock,
        id_type=body.id_type,
        id_number=body.id_number,
        selfie=body.selfie_b64,
        address_doc=body.address_doc_b64,
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return KycOut(tier=int(profile.tier), status=profile.status)
