"""Request dependencies: who is calling, may they call this route, and the
clock and notifier the booking services run with.

``get_current_actor`` reads the ``Authorization: Bearer <token>`` header and
verifies the token with the settings the running app was built with
(``app.state.settings``), never the process-wide cache. It then loads the token's
user on the request's own session (R25): a missing, inactive or soft-deleted user,
or one whose stored role is not the token's role, gets the same 401 as a bad
token. A restricted user still authenticates; the booking guards stop them
booking. ``require_role(*roles)`` builds a dependency that also checks the
caller's role.

``get_clock`` and ``get_notifier`` give routes the real :class:`SystemClock` and a
:class:`LoggingNotifier`; ``get_otp_sender``, ``get_kyc_provider`` and
``get_payment_provider`` give the app's OTP sender, KYC provider and payment
provider (``app.state``, chosen by ``Settings``), each 503 when the app has none.
Tests swap in ``FrozenClock`` / ``RecordingNotifier`` / the fakes through
``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.security import decode_token
from app.db.models import ActorRole, User
from app.db.session import get_session
from app.providers.kyc import KycProvider
from app.providers.otp import OtpSender
from app.providers.payment import PaymentProvider
from app.schemas.errors import (
    ForbiddenError,
    KycUnavailableError,
    OtpUnavailableError,
    PaymentUnavailableError,
    UnauthenticatedError,
)
from app.services.audit import Actor
from app.services.notify import LoggingNotifier, Notifier

# auto_error=False: a missing or non-Bearer header becomes our own 401 envelope
# instead of FastAPI's default 403. Declaring the scheme documents it in OpenAPI.
_bearer = HTTPBearer(auto_error=False)


async def get_current_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Actor:
    """The caller named by the bearer token; 401 ``unauthenticated`` otherwise.

    The token must verify, and its user must exist, be active, not be
    soft-deleted and hold the token's role (one query, on the request's session).
    """
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError()
    actor = decode_token(credentials.credentials, request.app.state.settings)
    user = await session.get(User, actor.user_id, populate_existing=True)
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or user.role.value != actor.role.value
    ):
        raise UnauthenticatedError()
    return actor


def require_role(*roles: ActorRole) -> Callable[..., Awaitable[Actor]]:
    """A dependency returning the caller when their role is one of ``roles``.

    No token or a bad one → 401 ``unauthenticated``; a valid token with another
    role → 403 ``forbidden``.
    """
    if not roles:
        raise ValueError("require_role needs at least one role")
    allowed = frozenset(ActorRole(role) for role in roles)

    async def dependency(actor: Actor = Depends(get_current_actor)) -> Actor:  # noqa: B008
        if actor.role not in allowed:
            raise ForbiddenError()
        return actor

    return dependency


def get_clock() -> Clock:
    """The time source for booking services (the wall clock)."""
    return SystemClock()


def get_notifier() -> Notifier:
    """Where booking notifications go (logged until a real channel exists)."""
    return LoggingNotifier()


def get_otp_sender(request: Request) -> OtpSender:
    """Where OTP codes go: the sender the running app was built with.

    503 ``otp_unavailable`` when it has none (no real sender exists yet).
    """
    sender = request.app.state.otp_sender
    if sender is None:
        raise OtpUnavailableError()
    return sender


def get_kyc_provider(request: Request) -> KycProvider:
    """The KYC provider the running app was built with.

    503 ``kyc_unavailable`` when it has none (no real provider exists yet).
    """
    provider = request.app.state.kyc_provider
    if provider is None:
        raise KycUnavailableError()
    return provider


def get_payment_provider(request: Request) -> PaymentProvider:
    """The payment provider the running app was built with.

    503 ``payment_unavailable`` when it has none (no real provider exists yet), so a
    booking is never accepted without the renter's payment account (R21).
    """
    provider = request.app.state.payment_provider
    if provider is None:
        raise PaymentUnavailableError()
    return provider
