"""Who did what: the acting party and the append-only audit writer (spec 2.6).

``before`` and ``after`` are stored as JSONB. :func:`record_audit` converts the
values services naturally hold: ``Decimal`` becomes a string (money is always
serialised as ``"150000.00"``), UUIDs become strings, dates and datetimes become
ISO 8601, enums become their values.

The writer flushes and never commits: the caller owns the transaction.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActorRole, AuditEvent


@dataclass(frozen=True)
class Actor:
    user_id: uuid.UUID | None
    role: ActorRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", ActorRole(self.role))
        if self.role != ActorRole.system and self.user_id is None:
            raise ValueError("user_id is required unless the actor is the system")

    @classmethod
    def system(cls) -> Actor:
        """The platform itself (timers, provider webhooks)."""
        return cls(user_id=None, role=ActorRole.system)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item) for item in value]
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


async def record_audit(
    session: AsyncSession,
    *,
    actor: Actor,
    entity: str,
    entity_id: uuid.UUID,
    action: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
    """Append one ``audit_events`` row."""
    session.add(
        AuditEvent(
            actor_id=actor.user_id,
            actor_role=actor.role.value,
            entity=entity,
            entity_id=entity_id,
            action=action,
            before=None if before is None else _jsonable(before),
            after=None if after is None else _jsonable(after),
            reason=reason,
            request_id=request_id,
        )
    )
    await session.flush()
