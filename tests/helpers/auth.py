"""Authorization headers for API tests."""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.core.security import issue_token
from app.db.models import ActorRole


def bearer(user_id: uuid.UUID, role: ActorRole, settings: Settings) -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}`` for ``user_id`` acting as ``role``.

    Pass the same ``settings`` the app under test was built with.
    """
    return {"Authorization": f"Bearer {issue_token(user_id, role, settings)}"}
