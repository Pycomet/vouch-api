"""Authentication dependencies: who is calling, and may they call this route.

``get_current_actor`` reads the ``Authorization: Bearer <token>`` header and
verifies the token with the settings the running app was built with
(``app.state.settings``), never the process-wide cache. ``require_role(*roles)``
builds a dependency that also checks the caller's role.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_token
from app.db.models import ActorRole
from app.schemas.errors import ForbiddenError, UnauthenticatedError
from app.services.audit import Actor

# auto_error=False: a missing or non-Bearer header becomes our own 401 envelope
# instead of FastAPI's default 403. Declaring the scheme documents it in OpenAPI.
_bearer = HTTPBearer(auto_error=False)


async def get_current_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> Actor:
    """The caller named by the bearer token; 401 ``unauthenticated`` otherwise."""
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError()
    return decode_token(credentials.credentials, request.app.state.settings)


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
