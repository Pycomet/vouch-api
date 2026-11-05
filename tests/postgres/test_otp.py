"""Live PostgreSQL tests for phone OTP login (spec 4, R25, R26).

The app runs in-process on the test's ``db_session`` (the same override as
``test_bookings_api.py``: commit before each request, roll back after), with a
:class:`FrozenClock` and a :class:`FakeOtpSender` that records the codes it is
asked to send.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_clock, get_otp_sender
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import decode_token
from app.db.models import ActorRole, OtpChallenge, User
from app.db.models.user import RenterRole
from app.db.session import get_session
from app.main import create_app
from app.providers.otp import FakeOtpSender
from app.schemas.errors import RateLimitedError
from app.services import auth as auth_service
from tests.postgres.factories import DEFAULT_NOW, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

SETTINGS = get_settings().model_copy(
    update={"jwt_secret": SecretStr("test-secret-that-is-long-enough-for-hs256")}
)
REQUEST = "/api/v1/auth/otp/request"
VERIFY = "/api/v1/auth/otp/verify"
PHONE = "+2348031234567"
GENERIC_401 = "The code is wrong or has expired."


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DEFAULT_NOW)


@pytest.fixture
def sender() -> FakeOtpSender:
    return FakeOtpSender()


@pytest.fixture
async def client(
    db_session: AsyncSession, clock: FrozenClock, sender: FakeOtpSender
) -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_otp_sender] = lambda: sender
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def _request(client: AsyncClient, phone: str = PHONE):
    return await client.post(REQUEST, json={"phone": phone})


async def _verify(client: AsyncClient, code: str, phone: str = PHONE, **extra):
    return await client.post(VERIFY, json={"phone": phone, "code": code, **extra})


async def _code(client: AsyncClient, sender: FakeOtpSender, phone: str = PHONE) -> str:
    response = await _request(client, phone)
    assert response.status_code == 202, response.text
    return sender.last_code(auth_service.normalize_phone(phone))


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def _assert_generic_401(response) -> None:
    assert response.status_code == 401
    error = response.json()["error"]
    assert (error["code"], error["message"], error["details"]) == (
        "unauthenticated",
        GENERIC_401,
        {},
    )


async def _challenges(session: AsyncSession, phone: str = PHONE) -> list[OtpChallenge]:
    rows = await session.execute(
        select(OtpChallenge)
        .where(OtpChallenge.phone == phone)
        .order_by(OtpChallenge.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def _user_by_phone(session: AsyncSession, phone: str = PHONE) -> User | None:
    result = await session.execute(
        select(User).where(User.phone == phone).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_otp_happy_path_returns_jwt(client, sender, db_session, clock) -> None:
    code = await _code(client, sender)

    response = await _verify(client, code, full_name="Ada Obi")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == SETTINGS.jwt_ttl_minutes * 60
    actor = decode_token(body["access_token"], SETTINGS)
    assert actor.role == ActorRole.renter
    user = await _user_by_phone(db_session)
    assert user is not None
    assert (user.id, user.role, user.full_name) == (actor.user_id, RenterRole.renter, "Ada Obi")
    assert user.phone_verified_at == clock.now()
    # The token works on a real authenticated route (404: no such booking, not 401).
    bookings = await client.get(
        f"/api/v1/bookings/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert bookings.status_code == 404


async def test_full_name_is_optional(client, sender, db_session) -> None:
    code = await _code(client, sender)

    assert (await _verify(client, code)).status_code == 200

    user = await _user_by_phone(db_session)
    assert user is not None and user.full_name is None


async def test_existing_user_keeps_their_stored_role(client, sender, db_session) -> None:
    vendor = await create_user(db_session, role=RenterRole.vendor, phone=PHONE, full_name="Kemi")
    vendor_id = vendor.id
    code = await _code(client, sender)

    response = await _verify(client, code, full_name="Someone Else")

    actor = decode_token(response.json()["access_token"], SETTINGS)
    assert (actor.user_id, actor.role) == (vendor_id, ActorRole.vendor)
    user = await _user_by_phone(db_session)
    assert user is not None and user.full_name == "Kemi"
    count = await db_session.scalar(select(func.count()).select_from(User))
    assert count == 1


async def test_existing_user_without_a_name_gets_the_one_given(client, sender, db_session) -> None:
    user = await create_user(db_session, phone=PHONE)
    user.full_name = None
    await db_session.flush()
    code = await _code(client, sender)

    assert (await _verify(client, code, full_name="Ada Obi")).status_code == 200

    stored = await _user_by_phone(db_session)
    assert stored is not None and stored.full_name == "Ada Obi"


async def test_phone_verified_at_keeps_the_first_verification(
    client, sender, db_session, clock
) -> None:
    first_at = clock.now()
    assert (await _verify(client, await _code(client, sender))).status_code == 200
    clock.advance(timedelta(days=1))

    assert (await _verify(client, await _code(client, sender))).status_code == 200

    user = await _user_by_phone(db_session)
    assert user is not None and user.phone_verified_at == first_at


async def test_client_cannot_pick_a_role(client, sender) -> None:
    code = await _code(client, sender)

    response = await _verify(client, code, role="admin")

    assert response.status_code == 422


@pytest.mark.parametrize("spelling", ["08031234567", "2348031234567", "+2348031234567"])
async def test_phone_spellings_normalize_to_one_number(
    client, sender, db_session, spelling
) -> None:
    code = await _code(client, sender, spelling)

    # Verify with a different spelling of the same number.
    response = await _verify(client, code, phone="08031234567")

    assert response.status_code == 200
    assert [c.phone for c in await _challenges(db_session)] == [PHONE]


@pytest.mark.parametrize(
    "phone",
    [
        "",
        "8031234567",
        "+234803123456",
        "+23480312345678",
        "0803123456",
        "+2340803123456",
        "2340803123456",
        "00803123456",
        "+448031234567",
        "0803 123 4567",
        "+234-803-123-4567",
        "+234803123456٧",  # an Arabic-Indic digit
        " +2348031234567",
        12345678901,
    ],
)
async def test_bad_phone_422(client, sender, phone) -> None:
    request = await client.post(REQUEST, json={"phone": phone})
    verify = await client.post(VERIFY, json={"phone": phone, "code": "123456"})

    assert (request.status_code, verify.status_code) == (422, 422)
    assert sender.sent == []


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", "12 456", "١٢٣456"])
async def test_malformed_code_422(client, code) -> None:
    assert (await _verify(client, code)).status_code == 422


# ---------------------------------------------------------------------------
# No enumeration
# ---------------------------------------------------------------------------


async def test_request_answers_the_same_for_known_and_unknown_phones(
    client, sender, db_session
) -> None:
    await create_user(db_session, phone="+2348030000001")

    known = await _request(client, "+2348030000001")
    unknown = await _request(client, "+2348030000002")

    assert (known.status_code, known.json()) == (202, {"status": "sent"})
    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    # Requesting a code creates no user.
    assert await _user_by_phone(db_session, "+2348030000002") is None


async def test_every_verify_failure_looks_the_same(client, sender, db_session, clock) -> None:
    no_challenge = await _verify(client, "123456")
    code = await _code(client, sender)
    wrong = await _verify(client, _wrong(code))
    clock.advance(timedelta(minutes=10))
    expired = await _verify(client, code)
    inactive = await create_user(db_session, phone="+2348030000009")
    inactive.is_active = False
    await db_session.flush()
    inactive_code = await _code(client, sender, "+2348030000009")
    blocked = await _verify(client, inactive_code, phone="+2348030000009")

    for response in (no_challenge, wrong, expired, blocked):
        _assert_generic_401(response)


async def test_soft_deleted_user_cannot_log_in(client, sender, db_session) -> None:
    user = await create_user(db_session, phone=PHONE)
    user.deleted_at = DEFAULT_NOW
    await db_session.flush()
    code = await _code(client, sender)

    _assert_generic_401(await _verify(client, code))


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


async def test_sixth_request_in_hour_429(client, sender, db_session, clock) -> None:
    for _ in range(5):
        assert (await _request(client)).status_code == 202
        clock.advance(timedelta(minutes=1))

    sixth = await _request(client)

    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "rate_limited"
    # The first request (at DEFAULT_NOW) leaves the window an hour after it was made.
    assert sixth.headers["Retry-After"] == str(int(timedelta(minutes=55).total_seconds()))
    assert len(await _challenges(db_session)) == 5
    assert len(sender.sent) == 5
    # Another phone is not affected.
    assert (await _request(client, "+2348030000003")).status_code == 202

    # Once the first request is an hour old, one more is allowed.
    clock.advance(timedelta(minutes=55))
    assert (await _request(client)).status_code == 202
    assert (await _request(client)).status_code == 429


async def test_rate_limit_holds_under_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(DEFAULT_NOW)
    sender = FakeOtpSender()

    async def one() -> str:
        async with session_factory() as session:
            try:
                await auth_service.request_otp(
                    session, PHONE, sender=sender, clock=clock, settings=SETTINGS
                )
            except RateLimitedError:
                await session.rollback()
                return "limited"
            await session.commit()
            return "sent"

    outcomes = await asyncio.gather(*(one() for _ in range(8)))

    assert sorted(outcomes) == ["limited"] * 3 + ["sent"] * 5
    async with session_factory() as session:
        assert len(await _challenges(session)) == 5


async def _committed_code(factory: async_sessionmaker[AsyncSession], sender, clock) -> str:
    async with factory() as session:
        await auth_service.request_otp(
            session, PHONE, sender=sender, clock=clock, settings=SETTINGS
        )
        await session.commit()
    return sender.last_code(PHONE)


async def _verify_in_own_session(factory, code: str, clock) -> uuid.UUID | None:
    """One verify as the route runs it: its own transaction, committed either way."""
    async with factory() as session:
        user = await auth_service.verify_otp(
            session, PHONE, code, full_name=None, clock=clock, settings=SETTINGS
        )
        user_id = None if user is None else user.id
        await session.commit()
        return user_id


async def test_parallel_wrong_guesses_never_pass_the_attempt_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock, sender = FrozenClock(DEFAULT_NOW), FakeOtpSender()
    code = await _committed_code(session_factory, sender, clock)

    outcomes = await asyncio.gather(
        *(_verify_in_own_session(session_factory, _wrong(code), clock) for _ in range(12))
    )

    assert outcomes == [None] * 12
    async with session_factory() as session:
        [challenge] = await _challenges(session)
        assert challenge.attempts == 5
    # Exhausted: even the right code is refused now.
    assert await _verify_in_own_session(session_factory, code, clock) is None


@pytest.mark.parametrize("existing", [True, False], ids=["existing-user", "new-phone"])
async def test_parallel_correct_verifies_log_in_exactly_once(
    session_factory: async_sessionmaker[AsyncSession], existing: bool
) -> None:
    clock, sender = FrozenClock(DEFAULT_NOW), FakeOtpSender()
    if existing:
        async with session_factory() as session:
            await create_user(session, phone=PHONE)
            await session.commit()
    code = await _committed_code(session_factory, sender, clock)

    outcomes = await asyncio.gather(
        *(_verify_in_own_session(session_factory, code, clock) for _ in range(4)),
        return_exceptions=True,
    )

    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    async with session_factory() as session:
        users = (await session.scalars(select(User).where(User.phone == PHONE))).all()
        assert [user.id for user in users] == winners


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------


async def test_wrong_code_attempts_exhaust(client, sender, db_session, clock) -> None:
    code = await _code(client, sender)

    for _ in range(5):
        _assert_generic_401(await _verify(client, _wrong(code)))

    # Five wrong guesses exhaust the challenge: the right code no longer works.
    _assert_generic_401(await _verify(client, code))
    [challenge] = await _challenges(db_session)
    assert (challenge.attempts, challenge.consumed_at) == (5, None)
    assert await _user_by_phone(db_session) is None

    # A new code works.
    clock.advance(timedelta(seconds=1))
    fresh = await _code(client, sender)
    assert (await _verify(client, fresh)).status_code == 200


async def test_fourth_wrong_guess_still_leaves_one_try(client, sender) -> None:
    code = await _code(client, sender)
    for _ in range(4):
        _assert_generic_401(await _verify(client, _wrong(code)))

    assert (await _verify(client, code)).status_code == 200


async def test_expired_code_rejected(client, sender, clock) -> None:
    code = await _code(client, sender)
    clock.advance(timedelta(minutes=10))

    _assert_generic_401(await _verify(client, code))


async def test_code_still_valid_just_before_expiry(client, sender, clock) -> None:
    code = await _code(client, sender)
    clock.advance(timedelta(minutes=10) - timedelta(seconds=1))

    assert (await _verify(client, code)).status_code == 200


async def test_code_single_use(client, sender, db_session, clock) -> None:
    code = await _code(client, sender)
    assert (await _verify(client, code)).status_code == 200

    _assert_generic_401(await _verify(client, code))
    [challenge] = await _challenges(db_session)
    assert challenge.consumed_at == clock.now()


async def test_only_the_latest_code_counts(client, sender, clock, monkeypatch) -> None:
    codes = iter([111111, 222222])
    monkeypatch.setattr(auth_service.secrets, "randbelow", lambda _: next(codes))
    first = await _code(client, sender)
    clock.advance(timedelta(seconds=1))
    second = await _code(client, sender)

    _assert_generic_401(await _verify(client, first))
    assert (await _verify(client, second)).status_code == 200


async def test_a_new_request_supersedes_the_open_codes(
    client, sender, db_session, clock, monkeypatch
) -> None:
    codes = iter([111111, 222222])
    monkeypatch.setattr(auth_service.secrets, "randbelow", lambda _: next(codes))
    first = await _code(client, sender)
    clock.advance(timedelta(seconds=1))
    await _code(client, sender)

    older, newer = await _challenges(db_session)
    assert (older.consumed_at, newer.consumed_at) == (clock.now(), None)
    _assert_generic_401(await _verify(client, first))


async def test_a_used_code_does_not_revive_an_older_one(client, sender, clock, monkeypatch) -> None:
    codes = iter([111111, 222222])
    monkeypatch.setattr(auth_service.secrets, "randbelow", lambda _: next(codes))
    first = await _code(client, sender)
    clock.advance(timedelta(seconds=1))
    second = await _code(client, sender)

    assert (await _verify(client, second)).status_code == 200
    _assert_generic_401(await _verify(client, first))


async def test_success_consumes_every_open_code(client, sender, db_session, clock) -> None:
    # Even an older challenge left open (e.g. written before superseding existed)
    # is closed by a successful login.
    await _code(client, sender)
    clock.advance(timedelta(seconds=1))
    code = await _code(client, sender)
    older, _ = await _challenges(db_session)
    older.consumed_at = None
    await db_session.flush()

    assert (await _verify(client, code)).status_code == 200

    assert all(c.consumed_at == clock.now() for c in await _challenges(db_session))


async def test_code_is_six_digits_and_stored_only_as_its_hmac(client, sender, db_session) -> None:
    code = await _code(client, sender)

    assert len(code) == 6 and code.isascii() and code.isdigit()
    [challenge] = await _challenges(db_session)
    expected = hmac.new(
        SETTINGS.jwt_secret.get_secret_value().encode(),
        f"{PHONE}:{code}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert challenge.code_hash == expected
    assert code not in challenge.code_hash
    assert challenge.expires_at == DEFAULT_NOW + timedelta(minutes=10)
    assert challenge.created_at == DEFAULT_NOW


async def test_the_code_is_never_logged(client, sender, vouch_caplog) -> None:
    vouch_caplog.set_level(logging.DEBUG)
    code = await _code(client, sender)
    await _verify(client, _wrong(code))
    await _verify(client, code)

    assert vouch_caplog.records, "expected the request log lines at least"
    assert code not in vouch_caplog.text
    assert PHONE not in vouch_caplog.text


async def test_token_role_comes_from_the_database(client, sender, db_session) -> None:
    admin = await create_user(db_session, role=RenterRole.admin, phone=PHONE)
    admin_id = admin.id
    code = await _code(client, sender)

    token = (await _verify(client, code)).json()["access_token"]

    claims = jwt.decode(token, options={"verify_signature": False})
    assert (claims["sub"], claims["role"]) == (str(admin_id), "admin")


# ---------------------------------------------------------------------------
# Fixed development code (VOUCH_FAKE_OTP_CODE)
# ---------------------------------------------------------------------------


async def test_fixed_fake_code_logs_in_without_reading_the_sender(db_session, clock) -> None:
    app = create_app(SETTINGS.model_copy(update={"fake_otp_code": "000000"}))

    async def session_override() -> AsyncIterator[AsyncSession]:
        await db_session.commit()
        try:
            yield db_session
        finally:
            await db_session.rollback()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_clock] = lambda: clock
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await _request(http)).status_code == 202
        response = await _verify(http, "000000")

    assert response.status_code == 200, response.text
    assert decode_token(response.json()["access_token"], SETTINGS).role == ActorRole.renter
