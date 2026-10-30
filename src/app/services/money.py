"""Ledger posting recipes: which accounts each money movement touches (spec 2.5).

Every recipe is a pure function of ``Decimal`` amounts that returns the
``list[Posting]`` for one ``txn_group``; :func:`app.services.ledger.post_txn`
writes it. Lines whose amount is 0 are left out, and a recipe whose total is 0
returns ``[]`` (the caller then skips ``post_txn``). Negative amounts raise
``ValueError``; non-Decimal amounts raise ``TypeError``.

Symbols: D = deposit, C = damage contribution, R = rental, F = delivery fee,
K = commission (:func:`commission_amount`), X = deduction, P = payout. All come
from the booking's ``price_snapshot`` (frozen at request time), never from
recomputed prices. Money leaving the platform (refunds, releases, payouts) is a
credit to ``platform_cash``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.db.models import Direction, LedgerAccount
from app.services.ledger import Posting

CENT = Decimal("0.01")


def _amount(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _group(*lines: tuple[LedgerAccount, Direction, Decimal]) -> list[Posting]:
    """The non-zero lines as postings, or ``[]`` when nothing moves."""
    postings = [
        Posting(account, direction, amount) for account, direction, amount in lines if amount
    ]
    return postings


def commission_amount(rental: Decimal, commission_rate: Decimal) -> Decimal:
    """K = rental x commission rate, quantized to the cent, half up."""
    return (_amount(rental, "rental") * _amount(commission_rate, "commission_rate")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def payment_captured_postings(
    deposit: Decimal,
    contribution: Decimal,
    rental: Decimal,
    delivery_fee: Decimal,
    commission: Decimal,
) -> list[Posting]:
    """The one upfront payment (A14): everything the renter owes, in one group.

    dr platform_cash D+C+R+F / cr renter_deposit_liability D / cr protection_pool C
    / cr vendor_payable R+F-K / cr platform_revenue K.
    """
    d = _amount(deposit, "deposit")
    c = _amount(contribution, "contribution")
    r = _amount(rental, "rental")
    f = _amount(delivery_fee, "delivery_fee")
    k = _amount(commission, "commission")
    if k > r + f:
        raise ValueError("commission must not exceed rental plus delivery fee")
    return _group(
        (LedgerAccount.platform_cash, Direction.dr, d + c + r + f),
        (LedgerAccount.renter_deposit_liability, Direction.cr, d),
        (LedgerAccount.protection_pool, Direction.cr, c),
        (LedgerAccount.vendor_payable, Direction.cr, r + f - k),
        (LedgerAccount.platform_revenue, Direction.cr, k),
    )


def cancellation_fee_amount(rental: Decimal, fee_rate: Decimal) -> Decimal:
    """The renter cancellation fee (A13): rental x fee rate, to the cent, half up."""
    return (_amount(rental, "rental") * _amount(fee_rate, "fee_rate")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def cancellation_fee_split(fee: Decimal, vendor_share: Decimal) -> tuple[Decimal, Decimal]:
    """``(vendor_part, platform_part)`` of the fee; the parts always sum to ``fee``.

    vendor_part = fee x vendor_share, to the cent, half up; Vouch gets the rest.
    """
    total = _amount(fee, "fee")
    vendor_part = (total * _amount(vendor_share, "vendor_share")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    return vendor_part, total - vendor_part


def cancellation_fee_postings(vendor_part: Decimal, platform_part: Decimal) -> list[Posting]:
    """The fee kept from a renter's refund (R15).

    dr platform_cash fee / cr vendor_payable vendor_part / cr platform_revenue
    platform_part. Posted after the capture group has been reversed in full.
    """
    v = _amount(vendor_part, "vendor_part")
    p = _amount(platform_part, "platform_part")
    return _group(
        (LedgerAccount.platform_cash, Direction.dr, v + p),
        (LedgerAccount.vendor_payable, Direction.cr, v),
        (LedgerAccount.platform_revenue, Direction.cr, p),
    )


def deposit_captured_postings(deposit: Decimal, contribution: Decimal) -> list[Posting]:
    """dr platform_cash D+C / cr renter_deposit_liability D / cr protection_pool C."""
    d = _amount(deposit, "deposit")
    c = _amount(contribution, "contribution")
    return _group(
        (LedgerAccount.platform_cash, Direction.dr, d + c),
        (LedgerAccount.renter_deposit_liability, Direction.cr, d),
        (LedgerAccount.protection_pool, Direction.cr, c),
    )


def balance_captured_postings(
    rental: Decimal, delivery_fee: Decimal, commission: Decimal
) -> list[Posting]:
    """dr platform_cash R+F / cr vendor_payable R+F-K / cr platform_revenue K."""
    r = _amount(rental, "rental")
    f = _amount(delivery_fee, "delivery_fee")
    k = _amount(commission, "commission")
    if k > r + f:
        raise ValueError("commission must not exceed rental plus delivery fee")
    return _group(
        (LedgerAccount.platform_cash, Direction.dr, r + f),
        (LedgerAccount.vendor_payable, Direction.cr, r + f - k),
        (LedgerAccount.platform_revenue, Direction.cr, k),
    )


def balance_refund_postings(
    rental: Decimal, delivery_fee: Decimal, commission: Decimal
) -> list[Posting]:
    """dr vendor_payable R+F-K / dr platform_revenue K / cr platform_cash R+F.

    The exact reverse of :func:`balance_captured_postings`: the rental balance
    goes back to the renter (a cancel after the balance was captured).
    """
    r = _amount(rental, "rental")
    f = _amount(delivery_fee, "delivery_fee")
    k = _amount(commission, "commission")
    if k > r + f:
        raise ValueError("commission must not exceed rental plus delivery fee")
    return _group(
        (LedgerAccount.vendor_payable, Direction.dr, r + f - k),
        (LedgerAccount.platform_revenue, Direction.dr, k),
        (LedgerAccount.platform_cash, Direction.cr, r + f),
    )


def deposit_release_postings(deposit: Decimal) -> list[Posting]:
    """dr renter_deposit_liability D / cr platform_cash D (the deposit goes back)."""
    d = _amount(deposit, "deposit")
    return _group(
        (LedgerAccount.renter_deposit_liability, Direction.dr, d),
        (LedgerAccount.platform_cash, Direction.cr, d),
    )


def deposit_deduct_postings(deposit: Decimal, deduction: Decimal) -> list[Posting]:
    """dr liability D / cr vendor_payable X / cr platform_cash D-X, with X capped at D."""
    d = _amount(deposit, "deposit")
    x = min(_amount(deduction, "deduction"), d)
    return _group(
        (LedgerAccount.renter_deposit_liability, Direction.dr, d),
        (LedgerAccount.vendor_payable, Direction.cr, x),
        (LedgerAccount.platform_cash, Direction.cr, d - x),
    )


def deposit_forfeit_postings(deposit: Decimal) -> list[Posting]:
    """dr renter_deposit_liability D / cr vendor_payable D."""
    d = _amount(deposit, "deposit")
    return _group(
        (LedgerAccount.renter_deposit_liability, Direction.dr, d),
        (LedgerAccount.vendor_payable, Direction.cr, d),
    )


def refund_contribution_postings(contribution: Decimal) -> list[Posting]:
    """dr protection_pool C / cr platform_cash C (the damage contribution goes back)."""
    c = _amount(contribution, "contribution")
    return _group(
        (LedgerAccount.protection_pool, Direction.dr, c),
        (LedgerAccount.platform_cash, Direction.cr, c),
    )


def payout_postings(payout: Decimal) -> list[Posting]:
    """dr vendor_payable P / cr platform_cash P."""
    p = _amount(payout, "payout")
    return _group(
        (LedgerAccount.vendor_payable, Direction.dr, p),
        (LedgerAccount.platform_cash, Direction.cr, p),
    )
