"""Live PostgreSQL tests for the audit writer."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActorRole, AuditEvent
from app.db.models.booking import BookingStatus
from app.services.audit import Actor, record_audit
from tests.postgres.factories import create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)


def test_actor_system_has_no_user() -> None:
    assert Actor.system() == Actor(user_id=None, role=ActorRole.system)


def test_actor_requires_user_unless_system() -> None:
    with pytest.raises(ValueError, match="user_id"):
        Actor(user_id=None, role=ActorRole.renter)


def test_actor_is_frozen() -> None:
    actor = Actor.system()
    with pytest.raises(AttributeError):
        actor.role = ActorRole.admin  # type: ignore[misc]


async def test_record_audit_writes_row(db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    entity_id = uuid.uuid4()
    other = uuid.uuid4()

    await record_audit(
        db_session,
        actor=Actor(user_id=user.id, role=ActorRole.renter),
        entity="booking",
        entity_id=entity_id,
        action="booking.accepted",
        before={"status": BookingStatus.requested},
        after={
            "status": BookingStatus.accepted,
            "amount": Decimal("150000.00"),
            "deposit_due_at": datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
            "starts_on": date(2026, 11, 5),
            "asset_id": other,
            "nested": [{"fee": Decimal("1.50")}],
        },
        reason="looks good",
        request_id="req_123",
    )

    row = (
        await db_session.execute(select(AuditEvent).where(AuditEvent.entity_id == entity_id))
    ).scalar_one()
    assert row.actor_id == user.id
    assert row.actor_role == "renter"
    assert row.entity == "booking"
    assert row.action == "booking.accepted"
    assert row.before == {"status": "requested"}
    assert row.after == {
        "status": "accepted",
        "amount": "150000.00",
        "deposit_due_at": "2026-11-03T09:00:00+00:00",
        "starts_on": "2026-11-05",
        "asset_id": str(other),
        "nested": [{"fee": "1.50"}],
    }
    assert row.reason == "looks good"
    assert row.request_id == "req_123"


async def test_record_audit_system_actor_and_empty_states(db_session: AsyncSession) -> None:
    entity_id = uuid.uuid4()

    await record_audit(
        db_session,
        actor=Actor.system(),
        entity="booking",
        entity_id=entity_id,
        action="booking.expired",
        before=None,
        after=None,
    )

    row = (
        await db_session.execute(select(AuditEvent).where(AuditEvent.entity_id == entity_id))
    ).scalar_one()
    assert row.actor_id is None
    assert row.actor_role == "system"
    assert row.before is None
    assert row.after is None
    assert row.reason is None
    assert row.request_id is None
