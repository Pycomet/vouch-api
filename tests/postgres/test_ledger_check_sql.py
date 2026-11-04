"""Live PostgreSQL tests for ``scripts/ledger_check.sql`` (M3a §2.3).

The check returns zero rows on a healthy ledger. The healthy case drives real
transitions (the ``docs/SCHEMA.md`` §8 recipes) into every money-bearing end state;
the unhealthy cases break one rule each.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.db.models.booking import BookingStatus
from app.services.audit import Actor
from app.services.booking import BookingEvent
from app.services.notify import RecordingNotifier
from tests.postgres.factories import DEFAULT_NOW, create_asset, create_booking, create_user
from tests.postgres.test_booking_lifecycle import (
    _lost_from,
    _paid,
    _transition,
    admin_actor,
    renter_actor,
    vendor_actor,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

CHECK_SQL = (Path(__file__).parents[2] / "scripts" / "ledger_check.sql").read_text()


async def _problems(session: AsyncSession) -> list[tuple[str, str, str]]:
    rows = (await session.execute(text(CHECK_SQL.strip().rstrip(";")))).all()
    return [tuple(row) for row in rows]


async def test_healthy_ledger_returns_no_rows(db_session: AsyncSession) -> None:
    notifier = RecordingNotifier()
    clock = FrozenClock(DEFAULT_NOW)
    # Paid and still live: the deposit hold and the contribution are on the ledger.
    for status in (
        BookingStatus.confirmed,
        BookingStatus.in_progress,
        BookingStatus.returned,
        BookingStatus.disputed,
    ):
        await _paid(db_session, status, claimed=status == BookingStatus.disputed)
    # Never paid.
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    await create_booking(db_session, asset=asset, renter=renter, status=BookingStatus.requested)

    # Paid, then cancelled by each side (full reversal, plus the fee for the renter).
    booking = await _paid(db_session, BookingStatus.confirmed)
    await _transition(
        db_session, booking, renter_actor(booking), BookingEvent.cancel, clock, notifier
    )
    booking = await _paid(db_session, BookingStatus.confirmed)
    await _transition(
        db_session,
        booking,
        vendor_actor(booking),
        BookingEvent.cancel,
        clock,
        notifier,
        {"reason": "No go"},
    )
    # Completed by the timer after the claim window.
    booking = await _paid(db_session, BookingStatus.returned)
    later = FrozenClock(booking.claim_window_ends_at + timedelta(seconds=1))
    await _transition(db_session, booking, Actor.system(), BookingEvent.complete, later, notifier)
    # Each admin decision on a dispute.
    admin = await admin_actor(db_session)
    for payload in (
        {"decision": "release", "reason": "ok"},
        {"decision": "deduct", "amount": "40000.00", "reason": "scratch"},
        {"decision": "forfeit", "reason": "broken"},
    ):
        booking = await _paid(db_session, BookingStatus.disputed, claimed=True)
        await _transition(
            db_session, booking, admin, BookingEvent.resolve, clock, notifier, payload
        )
    # Lost.
    booking = await _paid(db_session, BookingStatus.in_progress)
    await _transition(
        db_session,
        booking,
        admin,
        BookingEvent.mark_lost,
        FrozenClock(_lost_from(booking)),
        notifier,
        {"reason": "No contact for 7 days"},
    )

    assert await _problems(db_session) == []


async def test_reports_unbalanced_txn_group(db_session: AsyncSession) -> None:
    group = uuid.uuid4()
    # Let the balance trigger wait for a COMMIT that never comes (the fixture rolls back).
    await db_session.execute(text("SET CONSTRAINTS trg_ledger_balanced DEFERRED"))
    await db_session.execute(
        text(
            "INSERT INTO ledger_entries (account, direction, amount, currency, txn_group) "
            "VALUES ('platform_cash', 'dr', 100.00, 'NGN', :g), "
            "('platform_revenue', 'cr', 90.00, 'NGN', :g)"
        ),
        {"g": group},
    )

    assert await _problems(db_session) == [
        ("unbalanced_txn_group", str(group), "debits 100.00, credits 90.00")
    ]


async def test_reports_paid_status_without_postings(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    booking = await create_booking(
        db_session,
        asset=asset,
        renter=renter,
        status=BookingStatus.confirmed,
        price_snapshot={"rate_basis": "daily", "deposit": "150000.00", "currency": "NGN"},
    )

    problems = await _problems(db_session)

    assert problems == [
        (
            "booking_balance_mismatch",
            str(booking.id),
            "status confirmed: renter_deposit_liability 0 (expected 150000.00), "
            "protection_pool 0 (expected 0)",
        )
    ]


async def test_reports_completed_booking_still_holding_the_deposit(
    db_session: AsyncSession,
) -> None:
    booking = await _paid(db_session, BookingStatus.completed)

    problems = await _problems(db_session)

    assert [(p[0], p[1]) for p in problems] == [("booking_balance_mismatch", str(booking.id))]
    assert "renter_deposit_liability 150000.00 (expected 0)" in problems[0][2]


async def test_skips_legacy_bookings(db_session: AsyncSession) -> None:
    renter = await create_user(db_session, kyc_verified=True)
    asset = await create_asset(db_session)
    await create_booking(
        db_session,
        asset=asset,
        renter=renter,
        status=BookingStatus.confirmed,
        price_snapshot={"basis": "legacy", "rental": "1.00", "deposit": "150000.00"},
    )

    assert await _problems(db_session) == []
