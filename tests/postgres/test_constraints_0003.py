"""Live PostgreSQL tests for the constraints migration 0003 part B adds.

Booking overlap (live statuses), blocked availability windows, the booking
state-machine CHECKs, user restriction, kyc tiers, vendor references and the
payment type rename.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from dataclasses import dataclass

import asyncpg
import pytest

from tests.postgres.conftest import asyncpg_url

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

STARTS = datetime.date(2026, 12, 1)
ENDS = datetime.date(2026, 12, 4)

# Extra columns each status needs to satisfy the state-machine CHECKs.
STATUS_COLUMNS = {
    "requested": "hold_expires_at",
    "accepted": "deposit_due_at",
    "returned": "claim_window_ends_at",
    "disputed": "claim_window_ends_at",
}


@dataclass
class Parties:
    renter_id: uuid.UUID
    vendor_id: uuid.UUID
    asset_id: uuid.UUID


async def _user(conn: asyncpg.Connection, phone: str, role: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO users (phone, full_name, role, is_active) "
        "VALUES ($1, $2, $3, true) RETURNING id",
        phone,
        f"User {phone}",
        role,
    )


@pytest.fixture
async def parties(migrated_db: str, conn: asyncpg.Connection) -> Parties:
    renter_id = await _user(conn, "+2348000000301", "renter")
    vendor_id = await _user(conn, "+2348000000302", "vendor")
    category_id = await conn.fetchval("SELECT id FROM categories WHERE slug = 'camera'")
    item_id = await conn.fetchval(
        "INSERT INTO catalogue_items (category_id, make, model, replacement_value_band) "
        "VALUES ($1, 'Canon', 'R5', 'B') RETURNING id",
        category_id,
    )
    asset_id = await conn.fetchval(
        "INSERT INTO assets (vendor_id, catalogue_item_id, city_id, category_id, "
        "daily_rate, deposit_amount) "
        "VALUES ($1, $2, (SELECT id FROM cities WHERE slug = 'abuja'), $3, 50000, 100000) "
        "RETURNING id",
        vendor_id,
        item_id,
        category_id,
    )
    return Parties(renter_id=renter_id, vendor_id=vendor_id, asset_id=asset_id)


async def _book(
    conn: asyncpg.Connection,
    parties: Parties,
    status: str,
    *,
    starts: datetime.date = STARTS,
    ends: datetime.date = ENDS,
    renter_id: uuid.UUID | None = None,
    extra: dict[str, object] | None = None,
) -> uuid.UUID:
    columns = {
        "asset_id": parties.asset_id,
        "renter_id": renter_id or parties.renter_id,
        "vendor_id": parties.vendor_id,
        "starts_on": starts,
        "ends_on": ends,
        "status": status,
        "total_rental_fee": 150000,
        "deposit_amount": 100000,
        "price_snapshot": '{"basis": "daily"}',
        "fulfilment_choice": "pickup",
    }
    if status in STATUS_COLUMNS:
        columns[STATUS_COLUMNS[status]] = datetime.datetime.now(datetime.UTC)
    columns.update(extra or {})
    names = ", ".join(columns)
    params = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    return await conn.fetchval(
        f"INSERT INTO bookings ({names}) VALUES ({params}) RETURNING id", *columns.values()
    )


# ---------------------------------------------------------------------------
# Booking overlap
# ---------------------------------------------------------------------------


async def test_concurrent_requested_overlap_one_wins(parties: Parties) -> None:
    # Two open transactions on separate connections. The first inserts and stays
    # open until pg_stat_activity shows the second backend waiting on a Lock (so the
    # second INSERT provably blocked on the first's uncommitted row); only then does
    # the first commit, and the second fails with an exclusion violation. (When both
    # INSERTs reach the constraint check at the same instant, Postgres may instead
    # abort one with a deadlock error; the booking service must treat that as a
    # conflict.)
    first_inserted = asyncio.Event()
    order: list[str] = []
    observed_lock_wait: list[bool] = []

    async def wait_for_blocked_backend(first_pid: int) -> bool:
        monitor = await asyncpg.connect(asyncpg_url())
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                waiting = await monitor.fetchval(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> $1 "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' "
                    "AND query LIKE 'INSERT INTO bookings%'",
                    first_pid,
                )
                if waiting:
                    return True
                await asyncio.sleep(0.05)
            return False
        finally:
            await monitor.close()

    async def attempt(name: str, offset: int) -> uuid.UUID:
        connection = await asyncpg.connect(asyncpg_url())
        try:
            async with connection.transaction():
                if name == "second":
                    await first_inserted.wait()
                booking_id = await _book(
                    connection, parties, "requested", starts=STARTS + datetime.timedelta(offset)
                )
                if name == "first":
                    first_inserted.set()
                    pid = await connection.fetchval("SELECT pg_backend_pid()")
                    observed_lock_wait.append(await wait_for_blocked_backend(pid))
                    order.append("first commits")
                return booking_id
        except asyncpg.PostgresError:
            order.append(f"{name} failed")
            raise
        finally:
            await connection.close()

    results = await asyncio.gather(
        attempt("first", 0), attempt("second", 1), return_exceptions=True
    )
    winners = [r for r in results if isinstance(r, uuid.UUID)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], asyncpg.ExclusionViolationError)
    assert "ex_bookings_no_live_overlap" in str(errors[0])
    # The loser was seen waiting on a Lock before the winner committed.
    assert observed_lock_wait == [True]
    assert order == ["first commits", "second failed"]


async def test_declined_does_not_block(parties: Parties, conn: asyncpg.Connection) -> None:
    await _book(conn, parties, "declined")
    for status in ("expired", "cancelled_by_renter", "cancelled_by_vendor", "completed"):
        await _book(conn, parties, status)
    assert await _book(conn, parties, "requested") is not None


async def test_returned_still_blocks(parties: Parties, conn: asyncpg.Connection) -> None:
    await _book(conn, parties, "returned")
    with pytest.raises(asyncpg.ExclusionViolationError):
        await _book(conn, parties, "requested", starts=ENDS, ends=ENDS)


@pytest.mark.parametrize(
    "status", ["requested", "accepted", "deposit_paid", "confirmed", "in_progress", "returned"]
)
async def test_every_live_status_blocks(
    parties: Parties, conn: asyncpg.Connection, status: str
) -> None:
    await _book(conn, parties, status)
    with pytest.raises(asyncpg.ExclusionViolationError):
        await _book(conn, parties, "confirmed")


async def test_self_booking_rejected_by_db(parties: Parties, conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="ck_bookings_not_self"):
        await _book(conn, parties, "declined", renter_id=parties.vendor_id)


# ---------------------------------------------------------------------------
# Blocked availability windows
# ---------------------------------------------------------------------------


async def test_blocked_windows_cannot_overlap(parties: Parties, conn: asyncpg.Connection) -> None:
    insert = (
        "INSERT INTO availability_windows (asset_id, starts_at, ends_at, is_blocked) "
        "VALUES ($1, $2, $3, $4)"
    )
    await conn.execute(insert, parties.asset_id, STARTS, ENDS, True)
    # Unblocked windows may overlap anything; adjacent blocked windows are fine.
    await conn.execute(insert, parties.asset_id, STARTS, ENDS, False)
    await conn.execute(
        insert, parties.asset_id, ENDS + datetime.timedelta(1), ENDS + datetime.timedelta(3), True
    )
    with pytest.raises(asyncpg.ExclusionViolationError):
        await conn.execute(insert, parties.asset_id, ENDS, ENDS, True)


# ---------------------------------------------------------------------------
# State-machine CHECKs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "column", "constraint"),
    [
        ("requested", "hold_expires_at", "ck_bookings_requested_has_hold"),
        ("accepted", "deposit_due_at", "ck_bookings_accepted_has_deposit_due"),
        ("returned", "claim_window_ends_at", "ck_bookings_returned_has_claim_window"),
        ("disputed", "claim_window_ends_at", "ck_bookings_returned_has_claim_window"),
    ],
)
async def test_status_requires_its_timestamp(
    parties: Parties, conn: asyncpg.Connection, status: str, column: str, constraint: str
) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        await _book(conn, parties, status, extra={column: None})


async def test_claim_columns_are_all_or_nothing(parties: Parties, conn: asyncpg.Connection) -> None:
    now = datetime.datetime.now(datetime.UTC)
    full = {
        "claim_amount": 25000,
        "claim_description": "Cracked LCD",
        "claim_evidence": '["evidence://1"]',
        "claimed_at": now,
    }
    await _book(conn, parties, "disputed", extra=full)
    for missing in ("claim_amount", "claim_description", "claimed_at"):
        with pytest.raises(asyncpg.CheckViolationError, match="ck_bookings_claim_complete"):
            await _book(conn, parties, "disputed", extra={**full, missing: None})
    with pytest.raises(asyncpg.CheckViolationError, match="ck_bookings_claim_complete"):
        await _book(conn, parties, "returned", extra={"claim_evidence": "[]"})
    # Evidence is optional once the claim itself is complete.
    await _book(conn, parties, "disputed", extra={**full, "claim_evidence": None})
    with pytest.raises(asyncpg.CheckViolationError, match="ck_bookings_claim_amount_positive"):
        await _book(conn, parties, "disputed", extra={**full, "claim_amount": 0})


async def test_fulfilment_choice_and_price_snapshot_required(
    parties: Parties, conn: asyncpg.Connection
) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="ck_bookings_fulfilment_choice_valid"):
        await _book(conn, parties, "declined", extra={"fulfilment_choice": "courier"})
    await _book(conn, parties, "declined", extra={"fulfilment_choice": "delivery"})
    with pytest.raises(asyncpg.NotNullViolationError):
        await _book(conn, parties, "declined", extra={"price_snapshot": None})


async def test_status_defaults_to_requested(parties: Parties, conn: asyncpg.Connection) -> None:
    status = await conn.fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'bookings' AND column_name = 'status'"
    )
    assert status.startswith("'requested'")


# ---------------------------------------------------------------------------
# Users, kyc, vendor references, payments
# ---------------------------------------------------------------------------


async def test_user_restriction_requires_reason(parties: Parties, conn: asyncpg.Connection) -> None:
    update = "UPDATE users SET restricted_at = now(), restricted_reason = $2 WHERE id = $1"
    with pytest.raises(asyncpg.CheckViolationError, match="ck_users_restriction_has_reason"):
        await conn.execute(update, parties.renter_id, None)
    await conn.execute(update, parties.renter_id, "No contact 48h after start")


async def test_kyc_tier_is_zero_or_one_and_matches_status(
    parties: Parties, conn: asyncpg.Connection
) -> None:
    insert = (
        "INSERT INTO kyc_profiles (user_id, provider, status, tier, verified_at) "
        "VALUES ($1, 'fake', $2, $3, $4)"
    )
    now = datetime.datetime.now(datetime.UTC)
    with pytest.raises(asyncpg.CheckViolationError, match="ck_kyc_profiles_supported_tier"):
        await conn.execute(insert, parties.renter_id, "verified", 2, now)
    with pytest.raises(
        asyncpg.CheckViolationError, match="ck_kyc_profiles_verification_consistency"
    ):
        await conn.execute(insert, parties.renter_id, "verified", 0, now)
    with pytest.raises(
        asyncpg.CheckViolationError, match="ck_kyc_profiles_verification_consistency"
    ):
        await conn.execute(insert, parties.renter_id, "submitted", 1, None)
    await conn.execute(insert, parties.renter_id, "verified", 1, now)
    await conn.execute(insert, parties.vendor_id, "submitted", 0, None)


async def test_vendor_reference_unique_and_tied_to_the_booking(
    parties: Parties, conn: asyncpg.Connection
) -> None:
    booking_id = await _book(conn, parties, "completed")
    other_renter = await _user(conn, "+2348000000303", "renter")
    insert = "INSERT INTO vendor_references (renter_id, vendor_id, booking_id) VALUES ($1, $2, $3)"
    # The booking must belong to this renter and vendor.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(insert, other_renter, parties.vendor_id, booking_id)
    await conn.execute(insert, parties.renter_id, parties.vendor_id, booking_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(insert, parties.renter_id, parties.vendor_id, booking_id)
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute(insert, other_renter, parties.vendor_id, None)


async def test_paymenttype_renamed_damage_contribution(
    parties: Parties, conn: asyncpg.Connection
) -> None:
    booking_id = await _book(conn, parties, "confirmed")
    insert = "INSERT INTO payments (booking_id, type, status, amount, currency, provider) "
    await conn.execute(
        insert + "VALUES ($1, 'damage_contribution', 'pending', 7500, 'NGN', 'fake')",
        booking_id,
    )
    with pytest.raises(asyncpg.InvalidTextRepresentationError):
        await conn.execute(
            insert + "VALUES ($1, 'protection_waiver', 'pending', 7500, 'NGN', 'fake')",
            booking_id,
        )
