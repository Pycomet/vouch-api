"""Vendor-account guards shared by the listing and booking services (M3a 4.4, 4.7).

A vendor whose profile is not ``active`` (``pending``, ``suspended``, or with no
profile row at all) may still read their listings and bookings and sign handovers
and returns, but takes no new work: no listing write at all (create, edit, submit,
pause, resume, delete; amendment A23) and no accepting of a booking request.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.vendor import VendorProfile, VendorStatus
from app.schemas.errors import VendorNotActiveError


async def vendor_status(session: AsyncSession, vendor_id: uuid.UUID) -> VendorStatus | None:
    """The vendor's current profile status (``None`` without a profile).

    The row is read ``FOR SHARE``, so a concurrent suspension waits for the
    caller's transaction instead of slipping in between the check and the write.
    """
    return await session.scalar(
        select(VendorProfile.status)
        .where(VendorProfile.user_id == vendor_id)
        .with_for_update(read=True)
    )


async def ensure_vendor_active(session: AsyncSession, vendor_id: uuid.UUID) -> None:
    """Raise 409 ``vendor_not_active`` unless the vendor's profile is ``active``."""
    status = await vendor_status(session, vendor_id)
    if status != VendorStatus.active:
        raise VendorNotActiveError(details={"status": status.value if status else None})
