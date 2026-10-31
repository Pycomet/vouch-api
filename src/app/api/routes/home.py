"""Home screens (spec 3.3): ``GET /me/home`` (renter) and ``GET /vendor/home`` (vendor).

Both need a bearer token (401 ``unauthenticated``) with the right role (403
``forbidden``). "Today" for the vendor is the business date of the request clock
(``get_clock``), in ``Settings.business_timezone``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_clock, require_role
from app.api.examples import RENTER_HOME_EXAMPLE, VENDOR_HOME_EXAMPLE, documented
from app.core.clock import Clock, business_date
from app.core.config import Settings
from app.db.models import ActorRole
from app.db.session import get_session
from app.schemas.home import RenterHome, VendorHome
from app.services.audit import Actor
from app.services.home import renter_home, vendor_home

router = APIRouter(tags=["home"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get(
    "/me/home",
    response_model=RenterHome,
    summary="The renter's home screen",
    responses=documented(200, RENTER_HOME_EXAMPLE, 401, 403),
)
async def get_renter_home(
    request: Request,
    actor: Actor = Depends(require_role(ActorRole.renter)),
    session: AsyncSession = Depends(get_session),
) -> RenterHome:
    """The live booking that starts soonest, and up to 20 closed bookings."""
    return await renter_home(session, actor.user_id, settings=_settings(request))


@router.get(
    "/vendor/home",
    response_model=VendorHome,
    summary="The vendor's home screen",
    responses=documented(200, VENDOR_HOME_EXAMPLE, 401, 403),
)
async def get_vendor_home(
    request: Request,
    actor: Actor = Depends(require_role(ActorRole.vendor)),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> VendorHome:
    """Open requests (the hold that lapses first on top), today's handovers and
    returns, the next payout and the 7-day utilisation."""
    settings = _settings(request)
    return await vendor_home(
        session,
        actor.user_id,
        today=business_date(clock.now(), settings.business_zone),
        settings=settings,
    )
