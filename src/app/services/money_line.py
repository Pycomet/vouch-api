"""The money line every booking response carries (spec 3.4).

It tells each party where the deposit and the rental money stand and what is
due next. It is computed on every read from three things, and never stored:

* the booking's ledger postings, summed per account and direction in one query
  (payout postings are those whose ``payment_id`` is a ``payout`` payment);
* the booking's last ``deposit.*`` audit row, when the deposit hold has ended;
* the booking's status and dates.

Amounts come from the booking's ``price_snapshot``: D (deposit), C (damage
contribution), R (rental) and F (delivery fee).

Deposit status: ``held`` while the booking's ``renter_deposit_liability`` is
positive; ``due`` before payment; once paid and no longer held, the suffix of the
last ``deposit.*`` audit action (``released``, ``deducted``, ``forfeited``), and
``released`` after a paid cancellation (full refund). A paid booking whose deposit
is 0.00 still counts as ``held``.

Rental status (R + F): ``due`` before payment; ``received`` once the one upfront
payment is captured (A14); ``refunded`` after a paid cancellation; ``paid_out``
when payout postings cover the booking's whole vendor payable.

Contribution (C, shown only when C > 0): ``due`` before payment, ``received``
once paid, ``refunded`` after a paid cancellation.

Cancellation fee (shown only after a paid renter cancellation): the amount and
its vendor and platform parts, read from that cancellation's audit row (the
``after`` the fee group was posted with), never stored on the booking.

Refund (shown only on a booking cancelled after payment): D + C + R + F less the
cancellation fee, so refund + fee always equals everything paid.

A booking that closed before any payment (declined, expired, withdrawn) keeps
``due`` for both parts (nothing was ever received) and its next step is
``Closed`` with nothing to pay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AuditEvent, Booking, Direction, LedgerAccount, LedgerEntry, Payment
from app.db.models.booking import BookingStatus
from app.db.models.payment import PaymentType
from app.schemas.bookings import (
    CancellationFeeLine,
    ContributionLine,
    DepositLine,
    MoneyLine,
    NextStep,
    RefundLine,
    RentalLine,
    format_money,
)
from app.services.pricing import money

ZERO = Decimal("0.00")

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_UNPAID = frozenset({BookingStatus.requested, BookingStatus.accepted})
_PAID_LIVE = frozenset(
    {
        BookingStatus.deposit_paid,
        BookingStatus.confirmed,
        BookingStatus.in_progress,
        BookingStatus.returned,
        BookingStatus.disputed,
    }
)
_CANCELLED = frozenset({BookingStatus.cancelled_by_renter, BookingStatus.cancelled_by_vendor})
_DEPOSIT_ACTIONS = {
    "deposit.released": "released",
    "deposit.deducted": "deducted",
    "deposit.forfeited": "forfeited",
}


def local_day(day: date) -> str:
    """``Fri 10 Oct``: a calendar date in English, without a leading zero."""
    return f"{_DAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def local_time(instant: datetime, zone: ZoneInfo) -> str:
    """``Fri 10 Oct 14:00``: an instant on the business clock."""
    local = instant.astimezone(zone)
    return f"{local_day(local.date())} {local:%H:%M}"


@dataclass(frozen=True)
class _Ledger:
    """The booking's postings, summed per (account, direction)."""

    sums: dict[tuple[str, str], Decimal]
    payouts: Decimal

    def total(self, account: LedgerAccount, direction: Direction) -> Decimal:
        return self.sums.get((account.value, direction.value), ZERO)

    def balance(self, account: LedgerAccount) -> Decimal:
        """Credit-normal balance, as :func:`app.services.ledger.account_balance`."""
        return self.total(account, Direction.cr) - self.total(account, Direction.dr)

    @property
    def paid(self) -> bool:
        """Whether money ever came in for this booking (the upfront payment)."""
        return self.total(LedgerAccount.platform_cash, Direction.dr) > 0


async def _ledger(session: AsyncSession, booking: Booking) -> _Ledger:
    payout = case((Payment.type == PaymentType.payout, LedgerEntry.amount), else_=ZERO)
    rows = await session.execute(
        select(
            LedgerEntry.account,
            LedgerEntry.direction,
            func.sum(LedgerEntry.amount),
            func.sum(payout),
        )
        .outerjoin(Payment, Payment.id == LedgerEntry.payment_id)
        .where(LedgerEntry.booking_id == booking.id)
        .group_by(LedgerEntry.account, LedgerEntry.direction)
    )
    sums: dict[tuple[str, str], Decimal] = {}
    payouts = ZERO
    for account, direction, amount, payout_amount in rows.all():
        sums[(account, direction)] = Decimal(amount)
        if account == LedgerAccount.vendor_payable.value and direction == Direction.dr.value:
            payouts = Decimal(payout_amount)
    return _Ledger(sums, payouts)


def _amount(booking: Booking, key: str) -> Decimal:
    """A money value frozen in ``price_snapshot`` (a missing key is 0)."""
    return money((booking.price_snapshot or {}).get(key, "0"))


async def _last_deposit_action(session: AsyncSession, booking: Booking) -> str | None:
    return await session.scalar(
        select(AuditEvent.action)
        .where(
            AuditEvent.entity == "booking",
            AuditEvent.entity_id == booking.id,
            AuditEvent.action.in_(_DEPOSIT_ACTIONS),
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )


async def _deposit_status(session: AsyncSession, booking: Booking, ledger: _Ledger) -> str:
    if ledger.balance(LedgerAccount.renter_deposit_liability) > 0:
        return "held"
    if booking.status in _UNPAID or not ledger.paid:
        return "due"
    action = await _last_deposit_action(session, booking)
    if action is not None:
        return _DEPOSIT_ACTIONS[action]
    if booking.status in _PAID_LIVE:
        return "held"  # a paid booking whose deposit is 0.00
    return "released"  # a paid cancellation refunded the deposit in full


def _rental_status(booking: Booking, ledger: _Ledger) -> str:
    payable = ledger.balance(LedgerAccount.vendor_payable) + ledger.payouts
    if ledger.payouts > 0 and ledger.payouts >= payable:
        return "paid_out"
    if not ledger.paid:
        return "due"
    if booking.status in _CANCELLED:
        return "refunded"
    return "received"


def _next_step(booking: Booking, zone: ZoneInfo, total: Decimal) -> NextStep:
    status = booking.status
    amount_due = ZERO
    if status == BookingStatus.requested:
        label = "Awaiting vendor response"
    elif status == BookingStatus.accepted:
        label = f"Payment due {local_time(booking.deposit_due_at, zone)}"
        amount_due = total
    elif status in (BookingStatus.deposit_paid, BookingStatus.confirmed):
        label = f"Handover {local_day(booking.starts_on)}"
    elif status == BookingStatus.in_progress:
        label = f"Return by {local_day(booking.ends_on)}"
    elif status == BookingStatus.returned:
        label = f"Claim window closes {local_time(booking.claim_window_ends_at, zone)}"
    elif status == BookingStatus.disputed:
        label = "Under review"
    else:
        label = "Closed"
    return NextStep(label=label, amount_due=format_money(amount_due))


def _contribution_status(booking: Booking, ledger: _Ledger) -> str:
    if not ledger.paid:
        return "due"
    if booking.status in _CANCELLED:
        return "refunded"
    return "received"


async def _cancellation_fee(session: AsyncSession, booking: Booking) -> CancellationFeeLine | None:
    """The fee a paid renter cancellation kept, from that cancellation's audit row."""
    after = await session.scalar(
        select(AuditEvent.after)
        .where(
            AuditEvent.entity == "booking",
            AuditEvent.entity_id == booking.id,
            AuditEvent.action == f"booking.{BookingStatus.cancelled_by_renter.value}",
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )
    fee = (after or {}).get("cancellation_fee")
    if not fee:
        return None
    return CancellationFeeLine(
        amount=format_money(money(fee["amount"])),
        vendor_part=format_money(money(fee["vendor_part"])),
        platform_part=format_money(money(fee["platform_part"])),
    )


async def compute_money_line(
    session: AsyncSession, booking: Booking, *, settings: Settings
) -> MoneyLine:
    """The booking's money line, from its ledger, audit trail and status."""
    deposit = _amount(booking, "deposit")
    contribution = _amount(booking, "damage_contribution")
    rental = _amount(booking, "rental_total") + _amount(booking, "delivery_fee")
    total = deposit + contribution + rental
    ledger = await _ledger(session, booking)

    contribution_line = None
    if contribution > 0:
        contribution_line = ContributionLine(
            amount=format_money(contribution), status=_contribution_status(booking, ledger)
        )
    fee_line = None
    refund_line = None
    if booking.status in _CANCELLED and ledger.paid:
        fee_line = await _cancellation_fee(session, booking)
        fee = money(fee_line.amount) if fee_line is not None else ZERO
        refund_line = RefundLine(amount=format_money(total - fee))

    return MoneyLine(
        deposit=DepositLine(
            amount=format_money(deposit),
            status=await _deposit_status(session, booking, ledger),
        ),
        contribution=contribution_line,
        rental=RentalLine(amount=format_money(rental), status=_rental_status(booking, ledger)),
        cancellation_fee=fee_line,
        refund=refund_line,
        next=_next_step(booking, settings.business_zone, total),
    )
