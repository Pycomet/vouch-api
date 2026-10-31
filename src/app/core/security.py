"""Access tokens: HS256 JWTs carrying the caller's user id and role.

A token's claims are ``sub`` (the user id), ``role`` (an :class:`ActorRole` value),
``iat`` and ``exp``. Tokens are issued after phone OTP verification; this module
only signs and checks them. ``system`` is never a token role: the platform acts
through timers and provider webhooks, not over HTTP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings
from app.db.models import ActorRole
from app.schemas.errors import UnauthenticatedError
from app.services.audit import Actor

ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ["sub", "role", "exp", "iat"]
TOKEN_ROLES = frozenset(role for role in ActorRole if role != ActorRole.system)


def issue_token(
    user_id: uuid.UUID,
    role: ActorRole,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    """Sign an access token for ``user_id`` acting as ``role``.

    ``now`` defaults to the current time; tests pass it to mint expired tokens.
    """
    role = ActorRole(role)
    if role not in TOKEN_ROLES:
        raise ValueError(f"no access token for role {role.value!r}")
    issued_at = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "role": role.value,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_token(token: str, settings: Settings) -> Actor:
    """Verify ``token`` and return the :class:`Actor` it names.

    Raises :class:`UnauthenticatedError` for an expired, wrongly signed, malformed
    or incomplete token, or one whose ``sub`` is not a UUID or whose ``role`` is
    not a token role.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": _REQUIRED_CLAIMS},
        )
        user_id = uuid.UUID(str(claims["sub"]))
        role = ActorRole(claims["role"])
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise UnauthenticatedError() from exc
    if role not in TOKEN_ROLES:
        raise UnauthenticatedError()
    return Actor(user_id=user_id, role=role)
