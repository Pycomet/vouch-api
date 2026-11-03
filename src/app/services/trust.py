"""Renter trust tiers (spec 2.3, amendments A3 and A6).

``kyc_profiles`` stores only the verified level (tier 1). Tier 2 is computed here:
tier 1 and either at least ``CLEAN_COMPLETIONS_FOR_TIER_2`` completed bookings with
no incident, or one ``vendor_references`` row. A booking "with an incident" is one
with a claim raised (``bookings.claimed_at IS NOT NULL``). Tier 3 is per booking:
effective tier 2 plus ``bookings.vendor_approved_at``; :func:`required_tier_met`
applies it.

Tier 0 means "phone verified". Phone verification arrives with OTP in a later
step; until then every user without a verified KYC profile is treated as tier 0.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking, KYCProfile, VendorReference
from app.db.models.booking import BookingStatus
from app.db.models.user import KYCStatus, TrustTier

CLEAN_COMPLETIONS_FOR_TIER_2 = 3


async def tier_effective(session: AsyncSession, user_id: uuid.UUID) -> int:
    """The renter's effective trust tier: 0, 1 or 2."""
    verified = await session.scalar(
        select(
            exists().where(
                KYCProfile.user_id == user_id,
                KYCProfile.status == KYCStatus.verified,
                KYCProfile.tier == TrustTier.identity_verified,
                KYCProfile.verified_at.is_not(None),
            )
        )
    )
    if not verified:
        return int(TrustTier.phone_verified)

    referenced = await session.scalar(select(exists().where(VendorReference.renter_id == user_id)))
    if referenced:
        return int(TrustTier.established)

    clean_completions = await session.scalar(
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.renter_id == user_id,
            Booking.status == BookingStatus.completed,
            Booking.claimed_at.is_(None),
        )
    )
    if clean_completions >= CLEAN_COMPLETIONS_FOR_TIER_2:
        return int(TrustTier.established)
    return int(TrustTier.identity_verified)


def required_tier_met(effective: int, minimum: int, vendor_approved: bool) -> bool:
    """Whether a renter at ``effective`` may book an asset needing ``minimum``.

    Minimum 3 needs effective tier 2 and the vendor's approval of this booking.
    """
    if not TrustTier.phone_verified <= effective <= TrustTier.established:
        raise ValueError(f"effective tier must be 0-2, got {effective}")
    if not TrustTier.phone_verified <= minimum <= TrustTier.vendor_approved:
        raise ValueError(f"minimum tier must be 0-3, got {minimum}")
    if minimum == TrustTier.vendor_approved:
        return effective >= TrustTier.established and vendor_approved
    return effective >= minimum
