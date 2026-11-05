"""Blocks against booking requests on real connections (M3a 4.6 finding 1; ruling S3-R6).

Block creation and ``request_booking`` both lock the asset row first, then check
blocks, then live bookings, then insert. The tests are deterministic: one session
does its write and keeps its transaction open (so it holds the asset lock), the
other starts as a task, and the test proves it is waiting on a lock
(``pg_stat_activity.wait_event_type = 'Lock'``, polled with a deadline) before the
first commits. Every wait has a timeout, so a deadlock or a missing lock fails the
test instead of hanging it.

The test engine sets neither ``lock_timeout`` nor ``statement_timeout``, and the
application engine (``app.db.session``) does not either, so nothing cuts a wait
short: the waiting session really waits until the holder commits.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.models import AvailabilityWindow, Booking
from app.db.models.audit import ActorRole
from app.db.models.user import RenterRole
from app.schemas.errors import (
    BookingGuardFailed,
    DatesUnavailableError,
    ResourceNotFoundError,
)
from app.services.audit import Actor
from app.services.blocks import create_block
from app.services.booking import request_booking
from app.services.listings import delete_listing
from app.services.notify import RecordingNotifier
from tests.postgres.conftest import asyncpg_url
from tests.postgres.factories import DEFAULT_NOW, create_asset, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

Factory = async_sessionmaker[AsyncSession]

TODAY = date(2026, 11, 2)  # DEFAULT_NOW in the business zone
START = date(2027, 1, 4)
END = date(2027, 1, 6)
LOCK_WAIT_DEADLINE = 5.0
TASK_TIMEOUT = 15.0


@dataclass(frozen=True)
class Parties:
    vendor_id: uuid.UUID
    renter_id: uuid.UUID
    other_renter_id: uuid.UUID
    asset_id: uuid.UUID


async def _parties(factory: Factory) -> Parties:
    async with factory() as session:
        vendor = await create_user(session, role=RenterRole.vendor, kyc_verified=True)
        renter = await create_user(session, kyc_verified=True)
        other = await create_user(session, kyc_verified=True)
        asset = await create_asset(session, vendor=vendor)
        parties = Parties(vendor.id, renter.id, other.id, asset.id)
        await session.commit()
    return parties


async def _request(
    session: AsyncSession, renter_id: uuid.UUID, asset_id: uuid.UUID, starts_on=START, ends_on=END
) -> Booking:
    return await request_booking(
        session,
        renter=Actor(user_id=renter_id, role=ActorRole.renter),
        asset_id=asset_id,
        starts_on=starts_on,
        ends_on=ends_on,
        fulfilment="pickup",
        damage_contribution=False,
        clock=FrozenClock(DEFAULT_NOW),
        notifier=RecordingNotifier(),
        settings=get_settings(),
    )


async def _block(
    session: AsyncSession, vendor_id: uuid.UUID, asset_id: uuid.UUID, starts_on=START, ends_on=END
) -> AvailabilityWindow:
    return await create_block(
        session,
        actor=Actor(user_id=vendor_id, role=ActorRole.vendor),
        asset_id=asset_id,
        starts_on=starts_on,
        ends_on=ends_on,
        today=TODAY,
    )


async def _pid(session: AsyncSession) -> int:
    """The session's backend pid (this also pins its connection for the test)."""
    return await session.scalar(text("SELECT pg_backend_pid()"))


async def _waits_on_lock(pid: int) -> bool:
    """True once backend ``pid`` is waiting on a lock; False after the deadline."""
    monitor = await asyncpg.connect(asyncpg_url())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCK_WAIT_DEADLINE
        while loop.time() < deadline:
            waiting = await monitor.fetchval(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid
            )
            if waiting:
                return True
            await asyncio.sleep(0.02)
        return False
    finally:
        await monitor.close()


async def _counts(factory: Factory, asset_id: uuid.UUID) -> tuple[int, int]:
    """(blocks, bookings) committed on the asset."""
    async with factory() as session:
        blocks = await session.scalar(
            select(func.count())
            .select_from(AvailabilityWindow)
            .where(AvailabilityWindow.asset_id == asset_id, AvailabilityWindow.is_blocked)
        )
        bookings = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.asset_id == asset_id)
        )
    return blocks, bookings


async def test_block_first_then_request_waits_and_is_dates_blocked(session_factory) -> None:
    parties = await _parties(session_factory)
    async with session_factory() as first, session_factory() as second:
        await _block(first, parties.vendor_id, parties.asset_id)  # holds the asset lock
        second_pid = await _pid(second)
        task = asyncio.create_task(_request(second, parties.renter_id, parties.asset_id))
        try:
            assert await _waits_on_lock(second_pid), "the request did not wait for the block"
            assert not task.done()
            await first.commit()
            with pytest.raises(BookingGuardFailed) as caught:
                await asyncio.wait_for(task, TASK_TIMEOUT)
        finally:
            if not task.done():
                task.cancel()
        assert caught.value.code == "dates_blocked"

    assert await _counts(session_factory, parties.asset_id) == (1, 0)


async def test_request_first_then_block_waits_and_is_dates_unavailable(session_factory) -> None:
    parties = await _parties(session_factory)
    async with session_factory() as first, session_factory() as second:
        await _request(first, parties.renter_id, parties.asset_id)  # holds the asset lock
        second_pid = await _pid(second)
        task = asyncio.create_task(_block(second, parties.vendor_id, parties.asset_id))
        try:
            assert await _waits_on_lock(second_pid), "the block did not wait for the request"
            assert not task.done()
            await first.commit()
            with pytest.raises(DatesUnavailableError):
                await asyncio.wait_for(task, TASK_TIMEOUT)
        finally:
            if not task.done():
                task.cancel()

    assert await _counts(session_factory, parties.asset_id) == (0, 1)


async def test_two_requests_for_the_same_unit_resolve_to_one(session_factory) -> None:
    parties = await _parties(session_factory)
    async with session_factory() as first, session_factory() as second:
        await _request(first, parties.renter_id, parties.asset_id)
        second_pid = await _pid(second)
        task = asyncio.create_task(_request(second, parties.other_renter_id, parties.asset_id))
        try:
            assert await _waits_on_lock(second_pid), "the second request did not wait"
            assert not task.done()
            await first.commit()
            with pytest.raises(BookingGuardFailed) as caught:
                await asyncio.wait_for(task, TASK_TIMEOUT)
        finally:
            if not task.done():
                task.cancel()
        assert caught.value.code == "dates_unavailable"

    assert await _counts(session_factory, parties.asset_id) == (0, 1)


async def test_a_request_waiting_on_a_delete_is_404_after_it(session_factory) -> None:
    """The request re-checks visibility once it holds the lock: no booking on a deleted unit."""
    parties = await _parties(session_factory)
    async with session_factory() as first, session_factory() as second:
        await delete_listing(
            first,
            actor=Actor(user_id=parties.vendor_id, role=ActorRole.vendor),
            asset_id=parties.asset_id,
            now=DEFAULT_NOW,
        )
        second_pid = await _pid(second)
        task = asyncio.create_task(_request(second, parties.renter_id, parties.asset_id))
        try:
            assert await _waits_on_lock(second_pid), "the request did not wait for the delete"
            await first.commit()
            with pytest.raises(ResourceNotFoundError):
                await asyncio.wait_for(task, TASK_TIMEOUT)
        finally:
            if not task.done():
                task.cancel()

    assert await _counts(session_factory, parties.asset_id) == (0, 0)


async def _committed(factory: Factory, work) -> object:
    async with factory() as session:
        result = await work(session)
        await session.commit()
        return result


@pytest.mark.parametrize("block_first", [True, False], ids=["block-first", "request-first"])
async def test_block_and_request_fired_together_one_wins(session_factory, block_first) -> None:
    parties = await _parties(session_factory)
    for round_ in range(5):
        starts_on = START + timedelta(days=7 * round_)
        ends_on = starts_on + timedelta(days=2)

        async def block(session, starts_on=starts_on, ends_on=ends_on):
            return await _block(session, parties.vendor_id, parties.asset_id, starts_on, ends_on)

        async def request(session, starts_on=starts_on, ends_on=ends_on):
            return await _request(session, parties.renter_id, parties.asset_id, starts_on, ends_on)

        works = (block, request) if block_first else (request, block)
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *(_committed(session_factory, work) for work in works), return_exceptions=True
            ),
            TASK_TIMEOUT,
        )
        by_kind = dict(zip([work.__name__ for work in works], outcomes, strict=True))
        failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        assert len(failures) == 1, outcomes
        if isinstance(by_kind["block"], Exception):
            assert isinstance(by_kind["block"], DatesUnavailableError), by_kind
            assert isinstance(by_kind["request"], Booking)
        else:
            assert isinstance(by_kind["block"], AvailabilityWindow)
            assert isinstance(by_kind["request"], BookingGuardFailed), by_kind
            assert by_kind["request"].code == "dates_blocked"

    blocks, bookings = await _counts(session_factory, parties.asset_id)
    assert blocks + bookings == 5
