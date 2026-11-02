"""Renter identity verification: sets the verified tier (spec 2.3, 4, R27).

:func:`apply_kyc` asks the provider to check the id and the address document.
Both must pass for tier 1 (``status = verified``); otherwise the profile is
``failed`` at tier 0. Only ``national_id_type``, the provider name and one
provider reference are stored: the selfie, the document and the id number are
passed to the provider and never kept, audited or logged.

An already verified renter is left alone (idempotent: the provider is not called
and no audit row is written). A failed one may try again. The service flushes and
never commits; the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActorRole, KYCProfile, User
from app.db.models.user import KYCStatus, TrustTier
from app.providers.kyc import KycProvider
from app.services.audit import Actor, record_audit


async def apply_kyc(
    session: AsyncSession,
    user_id: uuid.UUID,
    provider: KycProvider,
    *,
    id_type: str,
    id_number: str,
    selfie: bytes,
    address_doc: bytes,
    request_id: str | None = None,
) -> KYCProfile:
    """Verify ``user_id`` with ``provider`` and return their KYC profile."""
    # Serialise concurrent attempts for one renter on the user row.
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    profile = await session.scalar(select(KYCProfile).where(KYCProfile.user_id == user_id))
    if profile is not None and profile.status == KYCStatus.verified:
        return profile

    identity = await provider.verify_identity(id_type, id_number, selfie)
    address = await provider.verify_address(address_doc)
    passed = identity.passed and address.passed
    reasons = [*identity.reasons, *address.reasons]

    before = None if profile is None else {"status": profile.status, "tier": int(profile.tier)}
    if profile is None:
        profile = KYCProfile(user_id=user_id)
        session.add(profile)
    profile.provider = provider.name
    profile.provider_reference = identity.reference
    profile.national_id_type = id_type
    if passed:
        profile.status = KYCStatus.verified
        profile.tier = TrustTier.identity_verified
        profile.verified_at = datetime.now(UTC)
    else:
        profile.status = KYCStatus.failed
        profile.tier = TrustTier.phone_verified
        profile.verified_at = None
    await session.flush()

    await record_audit(
        session,
        actor=Actor(user_id, ActorRole.renter),
        entity="kyc_profile",
        entity_id=profile.id,
        action="kyc.verified" if passed else "kyc.failed",
        before=before,
        after={
            "status": profile.status,
            "tier": int(profile.tier),
            "id_type": id_type,
            "provider": profile.provider,
            "provider_reference": profile.provider_reference,
            "reasons": reasons,
        },
        request_id=request_id,
    )
    return profile
