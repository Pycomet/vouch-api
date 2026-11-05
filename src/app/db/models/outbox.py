"""Notification outbox ORM model (M3a spec 3.5): one row per message to send.

A service writes the row in the same transaction as the change it reports; the
dispatcher sends due ``pending`` rows later. ``user_id`` NULL means an operations
alert (template ``ops.*``). ``data`` may hold phone numbers: never log it.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OutboxStatus(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    dead = "dead"


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="ck_outbox_sent_at"),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonnegative"),
        Index(
            "ix_outbox_due",
            "next_attempt_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_notification_outbox_user_id"),
        nullable=True,
    )
    template: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outboxstatus"),
        nullable=False,
        default=OutboxStatus.pending,
        server_default=OutboxStatus.pending.value,
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        # ``data`` may hold phone numbers: it is not shown.
        return (
            f"<NotificationOutbox id={self.id} template={self.template!r} "
            f"status={self.status.value!r}>"
        )
