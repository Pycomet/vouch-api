"""Live PostgreSQL tests for KYC: ``apply_kyc`` and ``POST /me/kyc`` (R27).

Re-verifying an already verified renter is idempotent: 200 with the same
``{tier: 1, status: verified}``, the provider is not called again and no new audit
row is written. A renter whose last attempt failed may try again.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_kyc_provider
from app.core.config import get_settings
from app.db.models import ActorRole, AuditEvent, KYCProfile
from app.db.models.user import KYCStatus, RenterRole
from app.db.session import get_session
from app.main import create_app
from app.providers.kyc import FakeKycProvider, KycProvider
from app.services.kyc import apply_kyc
from app.services.trust import tier_effective
from tests.helpers.auth import bearer
from tests.postgres.factories import create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
URL = "/api/v1/me/kyc"
ID_NUMBER = "12345678901"
SELFIE = b"SELFIE-BYTES-" + b"\x01\x02" * 40
DOC = b"ADDRESS-DOC-BYTES-" + b"\x03\x04" * 40


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def body(**over) -> dict:
    payload = {
        "id_type": "nin",
        "id_number": ID_NUMBER,
        "selfie_b64": b64(SELFIE),
        "address_doc_b64": b64(DOC),
    }
    payload.update(over)
    return payload


@pytest.fixture
def provider() -> FakeKycProvider:
    return FakeKycProvider()


@pytest.fixture
async def client(db_session: AsyncSession, provider: KycProvider) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_kyc_provider] = lambda: provider
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def _renter(session: AsyncSession) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(session)
    return user.id, bearer(user.id, ActorRole.renter, SETTINGS)


async def _apply(session, user_id, provider):
    return await apply_kyc(
        session,
        user_id,
        provider,
        id_type="nin",
        id_number=ID_NUMBER,
        selfie=SELFIE,
        address_doc=DOC,
        request_id="req-kyc-1",
    )


async def _audits(session, user_id) -> list[AuditEvent]:
    rows = await session.scalars(
        select(AuditEvent)
        .where(AuditEvent.actor_id == user_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return list(rows)


async def test_both_checks_pass_gives_tier_1_verified(db_session: AsyncSession) -> None:
    user = await create_user(db_session)

    profile = await _apply(db_session, user.id, FakeKycProvider())

    assert profile.status == KYCStatus.verified
    assert profile.tier == 1
    assert profile.verified_at is not None
    assert profile.national_id_type == "nin"
    assert profile.provider == "fake"
    assert profile.provider_reference.startswith("fake-")
    assert await tier_effective(db_session, user.id) == 1


@pytest.mark.parametrize("failing", ["identity", "address"])
async def test_a_failed_check_leaves_tier_0_failed(db_session: AsyncSession, failing) -> None:
    user = await create_user(db_session)

    profile = await _apply(db_session, user.id, FakeKycProvider(**{failing: False}))

    assert profile.status == KYCStatus.failed
    assert profile.tier == 0
    assert profile.verified_at is None
    assert await tier_effective(db_session, user.id) == 0


async def test_audit_rows_carry_the_request_id(db_session: AsyncSession) -> None:
    ok = await create_user(db_session)
    bad = await create_user(db_session)

    await _apply(db_session, ok.id, FakeKycProvider())
    await _apply(db_session, bad.id, FakeKycProvider(identity=False))

    (verified,) = await _audits(db_session, ok.id)
    (failed,) = await _audits(db_session, bad.id)
    assert (verified.action, verified.entity, verified.request_id) == (
        "kyc.verified",
        "kyc_profile",
        "req-kyc-1",
    )
    assert verified.actor_role == "renter"
    assert (failed.action, failed.request_id) == ("kyc.failed", "req-kyc-1")
    assert failed.after["reasons"] == ["identity_mismatch"]


async def test_a_failed_renter_can_retry_and_verify(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    await _apply(db_session, user.id, FakeKycProvider(address=False))

    profile = await _apply(db_session, user.id, FakeKycProvider())

    assert (profile.status, profile.tier) == (KYCStatus.verified, 1)
    count = await db_session.scalar(
        select(func.count()).select_from(KYCProfile).where(KYCProfile.user_id == user.id)
    )
    assert count == 1
    assert {a.action for a in await _audits(db_session, user.id)} == {"kyc.failed", "kyc.verified"}


async def test_reverifying_a_verified_renter_is_idempotent(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    profile = await _apply(db_session, user.id, FakeKycProvider())
    reference = profile.provider_reference

    again = FakeKycProvider(identity=False)
    profile = await _apply(db_session, user.id, again)

    assert (profile.status, profile.tier, profile.provider_reference) == (
        KYCStatus.verified,
        1,
        reference,
    )
    assert (again.identity_calls, again.address_calls) == (0, 0)
    assert len(await _audits(db_session, user.id)) == 1


async def test_endpoint_verifies_the_renter(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id, headers = await _renter(db_session)

    response = await client.post(URL, json=body(), headers={**headers, "X-Request-ID": "rid-7"})

    assert response.status_code == 200
    assert response.json() == {"tier": 1, "status": "verified"}
    assert await tier_effective(db_session, user_id) == 1
    (audit,) = await _audits(db_session, user_id)
    assert (audit.action, audit.request_id) == ("kyc.verified", "rid-7")

    again = await client.post(URL, json=body(), headers=headers)
    assert again.status_code == 200
    assert again.json() == {"tier": 1, "status": "verified"}


async def test_endpoint_reports_a_failed_check(
    client: AsyncClient, db_session: AsyncSession, provider: FakeKycProvider
) -> None:
    _, headers = await _renter(db_session)
    provider.address = False

    response = await client.post(URL, json=body(), headers=headers)

    assert response.status_code == 200
    assert response.json() == {"tier": 0, "status": "failed"}


async def test_oversize_documents_are_422(client: AsyncClient, db_session: AsyncSession) -> None:
    _, headers = await _renter(db_session)
    big = b64(b"x" * (2 * 1024 * 1024 + 1))

    for field in ("selfie_b64", "address_doc_b64"):
        response = await client.post(URL, json=body(**{field: big}), headers=headers)
        assert response.status_code == 422, field

    ok = b64(b"x" * (2 * 1024 * 1024))
    response = await client.post(URL, json=body(selfie_b64=ok), headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "override",
    [
        {"selfie_b64": "not base64 !!"},
        {"address_doc_b64": "abc"},
        {"selfie_b64": ""},
        {"id_type": "ssn"},
        {"id_number": "12"},
        {"id_number": "1234 5678 901"},
        {"extra": "field"},
    ],
)
async def test_bad_input_is_422(client: AsyncClient, db_session: AsyncSession, override) -> None:
    _, headers = await _renter(db_session)

    response = await client.post(URL, json=body(**override), headers=headers)

    assert response.status_code == 422
    assert ID_NUMBER not in response.text


async def test_vendor_is_403_and_no_token_is_401(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor)
    headers = bearer(vendor.id, ActorRole.vendor, SETTINGS)

    forbidden = await client.post(URL, json=body(), headers=headers)
    anonymous = await client.post(URL, json=body())

    assert forbidden.status_code == 403
    assert anonymous.status_code == 401


async def test_no_provider_is_503(db_session: AsyncSession) -> None:
    app = create_app(SETTINGS)
    app.state.kyc_provider = None
    user_id, headers = await _renter(db_session)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        yield db_session

    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(URL, json=body(), headers=headers)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "kyc_unavailable"
    assert await tier_effective(db_session, user_id) == 0


async def test_no_document_bytes_or_full_id_number_are_stored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, headers = await _renter(db_session)
    assert (await client.post(URL, json=body(), headers=headers)).status_code == 200

    profiles = await db_session.execute(text("SELECT to_jsonb(k)::text FROM kyc_profiles k"))
    audits = await db_session.execute(text("SELECT to_jsonb(a)::text FROM audit_events a"))
    dump = " ".join(row[0] for row in [*profiles, *audits])
    assert str(user_id) in dump  # the scan really saw this user's rows
    for secret in (ID_NUMBER, "SELFIE-BYTES", "ADDRESS-DOC", b64(SELFIE), b64(DOC)):
        assert secret not in dump
