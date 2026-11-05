"""The vendor's own profile (M3a 4.3): ``GET /vendor/profile``, read-only.

Vendors cannot edit bank details (a changed payout account is a fraud vector);
only an admin changes them. The provider's recipient code is never returned,
only whether one is set.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.api.examples import VENDOR_PROFILE_EXAMPLE, documented
from app.db.models import ActorRole, VendorProfile
from app.db.session import get_session
from app.schemas.errors import ResourceNotFoundError
from app.schemas.vendor import VendorProfileOut
from app.services.audit import Actor

router = APIRouter(prefix="/vendor", tags=["vendor"])


@router.get(
    "/profile",
    response_model=VendorProfileOut,
    summary="The vendor's own profile",
    responses=documented(200, VENDOR_PROFILE_EXAMPLE, 401, 403, 404),
)
async def get_vendor_profile(
    actor: Actor = Depends(require_role(ActorRole.vendor)),
    session: AsyncSession = Depends(get_session),
) -> VendorProfileOut:
    """Status, business and payout-account summary. 404 when there is no profile row."""
    profile = await session.get(VendorProfile, actor.user_id, populate_existing=True)
    if profile is None:
        raise ResourceNotFoundError()
    return VendorProfileOut(
        status=profile.status.value,
        business_type=profile.business_type.value,
        is_sub_broker=profile.is_sub_broker,
        business_name=profile.business_name,
        bank_name=profile.bank_name,
        account_name=profile.account_name,
        account_last4=profile.account_last4,
        has_recipient_code=profile.recipient_code is not None,
    )
