"""Phone OTP login (spec 4, R25, R26).

:func:`request_otp` makes a 6-digit code (``secrets.randbelow``, or
``Settings.fake_otp_code`` with the fake sender in development and test), stores only its
HMAC-SHA256 under the JWT secret with a 10-minute life, and hands the code to the
:class:`~app.providers.otp.OtpSender`. The new challenge supersedes the phone's
open ones (their ``consumed_at`` is set), so only the newest code ever works.
More than :data:`REQUESTS_PER_HOUR`
requests for one phone in a rolling hour raise :class:`RateLimitedError`, counted
from the stored challenges. It never reads ``users``, so its answer is the same
for a known and an unknown phone.

:func:`verify_otp` checks a code against the phone's latest unconsumed, unexpired
challenge in constant time. A wrong code spends one of :data:`MAX_OTP_ATTEMPTS`
attempts; at the last one the challenge is exhausted. A right code consumes every
open challenge of the phone (single use; no older code can come back) and returns
the phone's user, created on first login as a renter. It returns ``None`` for
every failure (no challenge, wrong, expired, exhausted, or an inactive or deleted
user) so the caller answers each one the same.

``consumed_at`` therefore means "can no longer be used": used, superseded or
cleared by a successful login.

Both take a per-phone transaction-scoped advisory lock first (and read the clock
only after it), so concurrent
requests cannot overrun the limit, and concurrent verifies cannot spend one
attempt twice or create the same user twice. Neither commits: the route commits,
including after a failed verify, so a wrong guess is always counted.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.db.models import OtpChallenge, User
from app.db.models.otp import MAX_OTP_ATTEMPTS
from app.db.models.user import RenterRole
from app.providers.otp import OtpSender
from app.schemas.errors import RateLimitedError

CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=10)
REQUESTS_PER_HOUR = 5
RATE_WINDOW = timedelta(hours=1)

# ASCII digits only: ``\d`` would also match other scripts' digits.
# The national number is 10 digits and never starts with 0 (that is the trunk prefix).
_NIGERIAN_PHONE = re.compile(r"(?:0|234|\+234)([1-9][0-9]{9})")


def normalize_phone(raw: str) -> str:
    """A Nigerian number as E.164 ``+234`` + 10 digits; ``ValueError`` otherwise.

    Accepts exactly ``0XXXXXXXXXX``, ``234XXXXXXXXXX`` and ``+234XXXXXXXXXX``, where
    the 10-digit national number starts with 1-9: no spaces, dashes or other
    characters.
    """
    if not isinstance(raw, str):
        raise ValueError("phone must be a string")
    match = _NIGERIAN_PHONE.fullmatch(raw)
    if match is None:
        raise ValueError("must be a Nigerian phone number: 0XXXXXXXXXX or +234XXXXXXXXXX")
    return "+234" + match.group(1)


def hash_code(phone: str, code: str, settings: Settings) -> str:
    """HMAC-SHA256 (hex) of ``code`` bound to ``phone``, keyed by the JWT secret."""
    key = settings.jwt_secret.get_secret_value().encode("utf-8")
    return hmac.new(key, f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def _new_code(settings: Settings) -> str:
    # The fixed development code applies only to the fake sender (config refuses
    # it outside development and test), so a real sender always gets a random code.
    if settings.fake_otp_code is not None and settings.effective_otp_sender == "fake":
        return settings.fake_otp_code
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


async def _lock_phone(session: AsyncSession, phone: str) -> None:
    """Serialize OTP work on one phone until the transaction ends."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"otp:{phone}", 0)))
    )


async def _close_open_challenges(session: AsyncSession, phone: str, now) -> None:
    """Make every open challenge of ``phone`` unusable from ``now``."""
    await session.execute(
        update(OtpChallenge)
        .where(OtpChallenge.phone == phone, OtpChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )


async def request_otp(
    session: AsyncSession,
    phone: str,
    *,
    sender: OtpSender,
    clock: Clock,
    settings: Settings,
) -> None:
    """Store a new challenge for the normalized ``phone`` and send its code.

    Raises :class:`RateLimitedError` (with the seconds until the oldest request in
    the window leaves it) when the phone already made :data:`REQUESTS_PER_HOUR`
    requests in the last hour; nothing is stored or sent then.
    """
    await _lock_phone(session, phone)
    now = clock.now()
    recent = (
        await session.scalars(
            select(OtpChallenge.created_at)
            .where(OtpChallenge.phone == phone, OtpChallenge.created_at > now - RATE_WINDOW)
            .order_by(OtpChallenge.created_at)
        )
    ).all()
    if len(recent) >= REQUESTS_PER_HOUR:
        # The request that must leave the window before one more is allowed.
        frees_at = recent[len(recent) - REQUESTS_PER_HOUR] + RATE_WINDOW
        raise RateLimitedError(retry_after=math.ceil((frees_at - now).total_seconds()))

    await _close_open_challenges(session, phone, now)
    code = _new_code(settings)
    session.add(
        OtpChallenge(
            phone=phone,
            code_hash=hash_code(phone, code, settings),
            expires_at=now + CODE_TTL,
            attempts=0,
            created_at=now,
        )
    )
    await session.flush()
    await sender.send(phone, code)


async def verify_otp(
    session: AsyncSession,
    phone: str,
    code: str,
    *,
    full_name: str | None,
    clock: Clock,
    settings: Settings,
) -> User | None:
    """The user ``code`` logs in as, or ``None`` for any failure (see module doc)."""
    await _lock_phone(session, phone)
    now = clock.now()
    # Hash before looking anything up, so every path does the same work.
    offered = hash_code(phone, code, settings)
    challenge = (
        await session.scalars(
            select(OtpChallenge)
            .where(
                OtpChallenge.phone == phone,
                OtpChallenge.consumed_at.is_(None),
                OtpChallenge.expires_at > now,
            )
            .order_by(OtpChallenge.created_at.desc(), OtpChallenge.id.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    if challenge is None or challenge.attempts >= MAX_OTP_ATTEMPTS:
        return None
    if not hmac.compare_digest(offered, challenge.code_hash):
        challenge.attempts += 1
        await session.flush()
        return None

    challenge.consumed_at = now
    await _close_open_challenges(session, phone, now)
    user = (
        await session.scalars(
            select(User).where(User.phone == phone).execution_options(populate_existing=True)
        )
    ).first()
    if user is None:
        user = User(phone=phone, full_name=full_name, role=RenterRole.renter, is_active=True)
        session.add(user)
    elif not user.is_active or user.deleted_at is not None:
        await session.flush()
        return None
    elif user.full_name is None and full_name is not None:
        user.full_name = full_name
    if user.phone_verified_at is None:
        user.phone_verified_at = now
    await session.flush()
    return user
