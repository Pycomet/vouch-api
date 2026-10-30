"""Double-entry ledger posting and balances (spec 2.5).

Every money movement is one ``txn_group`` of at least two postings whose debits
equal its credits. :func:`post_txn` checks that in Python (:class:`UnbalancedTxn`)
and then writes the whole group with one multi-row INSERT, so the database's
balance trigger also holds when it fires per statement. Rows are append-only;
correct a mistake by posting a reversing group.

The service flushes and never commits: the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Direction, LedgerAccount, LedgerEntry

CENT = Decimal("0.01")
# Numeric(12, 2): at most ten digits before the point.
MAX_AMOUNT = Decimal("9999999999.99")


class UnbalancedTxn(ValueError):
    """A posting group whose debits and credits differ, or that has under two rows."""


@dataclass(frozen=True)
class Posting:
    account: LedgerAccount
    direction: Direction
    amount: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("posting amount must be a Decimal")
        if not self.amount.is_finite():
            raise ValueError("posting amount must be finite")
        if self.amount <= 0:
            raise ValueError("posting amount must be positive")
        if self.amount != self.amount.quantize(CENT):
            raise ValueError("posting amount must have at most 2 decimal places")
        if self.amount > MAX_AMOUNT:
            raise ValueError("posting amount exceeds Numeric(12, 2)")
        # Normalise plain strings to the enums so rows always carry valid values.
        object.__setattr__(self, "account", LedgerAccount(self.account))
        object.__setattr__(self, "direction", Direction(self.direction))


def _check_balanced(postings: Sequence[Posting]) -> None:
    if len(postings) < 2:
        raise UnbalancedTxn("a txn_group needs at least two postings")
    debits = sum((p.amount for p in postings if p.direction == Direction.dr), Decimal(0))
    credits = sum((p.amount for p in postings if p.direction == Direction.cr), Decimal(0))
    if debits != credits:
        raise UnbalancedTxn(f"debits {debits} <> credits {credits}")


async def post_txn(
    session: AsyncSession,
    *,
    postings: Sequence[Posting],
    booking_id: uuid.UUID | None,
    payment_id: uuid.UUID | None = None,
    memo: str,
) -> uuid.UUID:
    """Write one balanced group of postings and return its ``txn_group``."""
    _check_balanced(postings)
    txn_group = uuid.uuid4()
    rows = [
        {
            "booking_id": booking_id,
            "payment_id": payment_id,
            "account": posting.account.value,
            "direction": posting.direction.value,
            "amount": posting.amount,
            "txn_group": txn_group,
            "memo": memo,
        }
        for posting in postings
    ]
    # One multi-row INSERT ... VALUES statement (never one INSERT per row).
    await session.execute(insert(LedgerEntry).values(rows))
    return txn_group


async def account_balance(
    session: AsyncSession,
    account: LedgerAccount,
    *,
    booking_id: uuid.UUID | None = None,
) -> Decimal:
    """Credit-normal balance of ``account``: sum(cr) - sum(dr), optionally per booking."""
    signed = case(
        (LedgerEntry.direction == Direction.cr.value, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    query = select(func.coalesce(func.sum(signed), 0)).where(
        LedgerEntry.account == LedgerAccount(account).value
    )
    if booking_id is not None:
        query = query.where(LedgerEntry.booking_id == booking_id)
    balance = (await session.execute(query)).scalar_one()
    return Decimal(balance).quantize(CENT)
