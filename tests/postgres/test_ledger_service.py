"""Live PostgreSQL tests for the ledger service and the ``db_session`` fixture."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Direction, LedgerAccount, LedgerEntry
from app.db.models.booking import BookingStatus
from app.services.ledger import Posting, UnbalancedTxn, account_balance, post_txn
from tests.postgres.factories import create_asset, create_booking, create_user

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

CASH = LedgerAccount.platform_cash
DEPOSITS = LedgerAccount.renter_deposit_liability
DR = Direction.dr
CR = Direction.cr


async def _booking_id(session: AsyncSession) -> uuid.UUID:
    renter = await create_user(session, kyc_verified=True)
    asset = await create_asset(session)
    booking = await create_booking(
        session, asset=asset, renter=renter, status=BookingStatus.deposit_paid
    )
    return booking.id


async def _entry_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()


def _deposit_held(amount: str) -> list[Posting]:
    return [Posting(CASH, DR, Decimal(amount)), Posting(DEPOSITS, CR, Decimal(amount))]


# ---------------------------------------------------------------------------
# Posting validation (no database)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["0", "0.00", "-1.00", "10.001", "0.005", "NaN", "Infinity"])
def test_posting_rejects_bad_amounts(amount: str) -> None:
    with pytest.raises(ValueError):
        Posting(CASH, DR, Decimal(amount))


def test_posting_rejects_non_decimal_amount() -> None:
    with pytest.raises(TypeError):
        Posting(CASH, DR, 10)  # type: ignore[arg-type]


def test_posting_rejects_amount_over_column_precision() -> None:
    with pytest.raises(ValueError):
        Posting(CASH, DR, Decimal("10000000000.00"))


def test_posting_accepts_two_decimal_places() -> None:
    posting = Posting(CASH, DR, Decimal("150000.5"))

    assert posting.amount == Decimal("150000.5")


# ---------------------------------------------------------------------------
# post_txn
# ---------------------------------------------------------------------------


async def test_post_txn_writes_balanced_group(db_session: AsyncSession) -> None:
    booking_id = await _booking_id(db_session)

    group = await post_txn(
        db_session,
        postings=_deposit_held("150000.00"),
        booking_id=booking_id,
        memo="deposit held",
    )

    rows = (
        (await db_session.execute(select(LedgerEntry).where(LedgerEntry.txn_group == group)))
        .scalars()
        .all()
    )
    assert isinstance(group, uuid.UUID)
    assert sorted((row.account, row.direction, row.amount) for row in rows) == [
        ("platform_cash", "dr", Decimal("150000.00")),
        ("renter_deposit_liability", "cr", Decimal("150000.00")),
    ]
    assert {row.booking_id for row in rows} == {booking_id}
    assert {row.payment_id for row in rows} == {None}
    assert {row.memo for row in rows} == {"deposit held"}
    assert {row.currency for row in rows} == {"NGN"}


async def test_post_txn_uses_one_insert_statement(db_session: AsyncSession) -> None:
    booking_id = await _booking_id(db_session)
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if "ledger_entries" in statement and statement.lstrip().upper().startswith("INSERT"):
            statements.append(statement)

    sync_engine = db_session.bind.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        await post_txn(
            db_session,
            postings=[
                Posting(CASH, DR, Decimal("300.00")),
                Posting(DEPOSITS, CR, Decimal("100.00")),
                Posting(LedgerAccount.vendor_payable, CR, Decimal("150.00")),
                Posting(LedgerAccount.platform_revenue, CR, Decimal("50.00")),
            ],
            booking_id=booking_id,
            memo="split",
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)

    assert len(statements) == 1


@pytest.mark.parametrize(
    "postings",
    [
        [Posting(CASH, DR, Decimal("100.00")), Posting(DEPOSITS, CR, Decimal("99.99"))],
        [Posting(CASH, DR, Decimal("100.00"))],
        [Posting(CASH, DR, Decimal("100.00")), Posting(DEPOSITS, DR, Decimal("100.00"))],
        [],
    ],
    ids=["unequal", "single-row", "all-debits", "empty"],
)
async def test_post_txn_rejects_unbalanced_group(
    db_session: AsyncSession, postings: list[Posting]
) -> None:
    booking_id = await _booking_id(db_session)

    with pytest.raises(UnbalancedTxn):
        await post_txn(db_session, postings=postings, booking_id=booking_id, memo="bad")

    assert await _entry_count(db_session) == 0


async def test_db_trigger_rejects_unbalanced_insert_under_fixture(
    db_session: AsyncSession,
) -> None:
    """The deferred balance trigger fires per statement because the fixture made it
    IMMEDIATE; without that, nothing would fail until a COMMIT that never comes."""
    group = uuid.uuid4()

    with pytest.raises(DBAPIError, match="txn_group"):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(LedgerEntry).values(
                    [
                        {
                            "account": CASH.value,
                            "direction": "dr",
                            "amount": 100,
                            "txn_group": group,
                        },
                        {
                            "account": DEPOSITS.value,
                            "direction": "cr",
                            "amount": 90,
                            "txn_group": group,
                        },
                    ]
                )
            )

    assert await _entry_count(db_session) == 0


# ---------------------------------------------------------------------------
# account_balance
# ---------------------------------------------------------------------------


async def test_account_balance_is_credit_normal_decimal(db_session: AsyncSession) -> None:
    first = await _booking_id(db_session)
    second = await _booking_id(db_session)

    assert await account_balance(db_session, DEPOSITS) == Decimal("0.00")

    await post_txn(db_session, postings=_deposit_held("150000.00"), booking_id=first, memo="held")
    await post_txn(db_session, postings=_deposit_held("80000.50"), booking_id=second, memo="held")
    await post_txn(
        db_session,
        postings=[
            Posting(DEPOSITS, DR, Decimal("50000.00")),
            Posting(CASH, CR, Decimal("50000.00")),
        ],
        booking_id=first,
        memo="partial release",
    )

    total = await account_balance(db_session, DEPOSITS)
    assert isinstance(total, Decimal)
    assert total == Decimal("180000.50")
    assert await account_balance(db_session, DEPOSITS, booking_id=first) == Decimal("100000.00")
    assert await account_balance(db_session, DEPOSITS, booking_id=second) == Decimal("80000.50")
    # Cash is debit-normal, so its credit-normal balance is negative.
    assert await account_balance(db_session, CASH) == Decimal("-180000.50")
    assert await account_balance(db_session, LedgerAccount.protection_pool) == Decimal("0.00")
