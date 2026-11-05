"""Phone OTP challenge ORM model (spec 4, R26): one row per code sent.

Only an HMAC-SHA256 of the code is stored, keyed by the JWT secret, so a database
read never yields a usable code. ``attempts`` counts wrong guesses; at
:data:`MAX_OTP_ATTEMPTS` the challenge is exhausted. ``consumed_at`` marks the one
successful use.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, SmallInteger, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MAX_OTP_ATTEMPTS = 5


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    __table_args__ = (
        CheckConstraint(
            f"attempts BETWEEN 0 AND {MAX_OTP_ATTEMPTS}", name="ck_otp_challenges_attempts_range"
        ),
        Index("ix_otp_challenges_phone_created_at", "phone", "created_at"),
        Index(
            "ix_otp_challenges_ip_created",
            "ip_hash",
            "created_at",
            postgresql_where=text("ip_hash IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # HMAC of the client IP, keyed like ``code_hash`` (0006); NULL when unknown.
    ip_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        # The phone is personal data and the hash is secret-derived: neither is shown.
        return f"<OtpChallenge id={self.id} attempts={self.attempts}>"
