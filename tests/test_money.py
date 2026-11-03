"""The pure ledger posting recipes (Task 3.3, spec 2.5)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

import pytest

from app.db.models import Direction, LedgerAccount
from app.services.ledger import Posting
from app.services.money import (
    balance_captured_postings,
    balance_refund_postings,
    commission_amount,
    deposit_captured_postings,
    deposit_deduct_postings,
    deposit_forfeit_postings,
    deposit_release_postings,
    payout_postings,
    refund_contribution_postings,
)

D = Decimal
MAX = "9999999999.99"


def _lines(postings: Sequence[Posting]) -> set[tuple[str, str, Decimal]]:
    return {(p.account.value, p.direction.value, p.amount) for p in postings}


def _sums(postings: Sequence[Posting]) -> tuple[Decimal, Decimal]:
    dr = sum((p.amount for p in postings if p.direction == Direction.dr), D(0))
    cr = sum((p.amount for p in postings if p.direction == Direction.cr), D(0))
    return dr, cr


CASES: list[tuple[str, Callable[..., list[Posting]], tuple[str, ...]]] = [
    ("captured_small", deposit_captured_postings, ("0.01", "0.01")),
    ("captured_no_contribution", deposit_captured_postings, ("150000.00", "0")),
    ("captured_max", deposit_captured_postings, ("9999999999.98", "0.01")),
    ("captured_only_contribution", deposit_captured_postings, ("0", "0.01")),
    ("balance_small", balance_captured_postings, ("0.01", "0", "0")),
    ("balance_typical", balance_captured_postings, ("150000.00", "2500.00", "22500.00")),
    ("balance_max", balance_captured_postings, ("9999999999.98", "0.01", "0.01")),
    ("balance_all_commission", balance_captured_postings, ("100.00", "0", "100.00")),
    ("balance_refund_small", balance_refund_postings, ("0.01", "0", "0")),
    ("balance_refund_typical", balance_refund_postings, ("150000.00", "7500.00", "22500.00")),
    ("balance_refund_max", balance_refund_postings, ("9999999999.98", "0.01", "0.01")),
    ("balance_refund_all_commission", balance_refund_postings, ("100.00", "0", "100.00")),
    ("release_small", deposit_release_postings, ("0.01",)),
    ("release_max", deposit_release_postings, (MAX,)),
    ("deduct_partial", deposit_deduct_postings, ("150000.00", "0.01")),
    ("deduct_capped", deposit_deduct_postings, ("0.01", MAX)),
    ("deduct_max", deposit_deduct_postings, (MAX, "0.01")),
    ("deduct_nothing", deposit_deduct_postings, ("150000.00", "0")),
    ("forfeit_small", deposit_forfeit_postings, ("0.01",)),
    ("forfeit_max", deposit_forfeit_postings, (MAX,)),
    ("refund_small", refund_contribution_postings, ("0.01",)),
    ("refund_max", refund_contribution_postings, (MAX,)),
    ("payout_small", payout_postings, ("0.01",)),
    ("payout_max", payout_postings, (MAX,)),
]


@pytest.mark.parametrize(
    ("recipe", "args"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES]
)
def test_every_recipe_balances(recipe, args) -> None:
    postings = recipe(*(D(a) for a in args))

    dr, cr = _sums(postings)
    assert len(postings) >= 2
    assert dr == cr
    assert all(p.amount > 0 for p in postings)


def test_deposit_captured_lines() -> None:
    assert _lines(deposit_captured_postings(D("150000.00"), D("7500.00"))) == {
        ("platform_cash", "dr", D("157500.00")),
        ("renter_deposit_liability", "cr", D("150000.00")),
        ("protection_pool", "cr", D("7500.00")),
    }


def test_deposit_captured_omits_zero_contribution() -> None:
    assert _lines(deposit_captured_postings(D("150000.00"), D("0.00"))) == {
        ("platform_cash", "dr", D("150000.00")),
        ("renter_deposit_liability", "cr", D("150000.00")),
    }


def test_balance_captured_lines() -> None:
    assert _lines(balance_captured_postings(D("150000.00"), D("2500.00"), D("22500.00"))) == {
        ("platform_cash", "dr", D("152500.00")),
        ("vendor_payable", "cr", D("130000.00")),
        ("platform_revenue", "cr", D("22500.00")),
    }


def test_balance_captured_rejects_commission_above_total() -> None:
    with pytest.raises(ValueError):
        balance_captured_postings(D("100.00"), D("0"), D("100.01"))


def test_balance_refund_lines() -> None:
    assert _lines(balance_refund_postings(D("150000.00"), D("7500.00"), D("22500.00"))) == {
        ("vendor_payable", "dr", D("135000.00")),
        ("platform_revenue", "dr", D("22500.00")),
        ("platform_cash", "cr", D("157500.00")),
    }


def test_balance_refund_reverses_balance_captured() -> None:
    args = (D("150000.00"), D("7500.00"), D("22500.00"))
    captured = {(p.account, p.amount) for p in balance_captured_postings(*args)}
    refunded = {(p.account, p.amount) for p in balance_refund_postings(*args)}
    assert captured == refunded
    for c, r in zip(
        sorted(balance_captured_postings(*args), key=lambda p: p.account.value),
        sorted(balance_refund_postings(*args), key=lambda p: p.account.value),
        strict=True,
    ):
        assert c.direction != r.direction


def test_balance_refund_omits_zero_commission() -> None:
    assert _lines(balance_refund_postings(D("100.00"), D("0"), D("0"))) == {
        ("vendor_payable", "dr", D("100.00")),
        ("platform_cash", "cr", D("100.00")),
    }


def test_balance_refund_rejects_commission_above_total() -> None:
    with pytest.raises(ValueError):
        balance_refund_postings(D("100.00"), D("0"), D("100.01"))


def test_release_forfeit_refund_payout_lines() -> None:
    assert _lines(deposit_release_postings(D("5.00"))) == {
        ("renter_deposit_liability", "dr", D("5.00")),
        ("platform_cash", "cr", D("5.00")),
    }
    assert _lines(deposit_forfeit_postings(D("5.00"))) == {
        ("renter_deposit_liability", "dr", D("5.00")),
        ("vendor_payable", "cr", D("5.00")),
    }
    assert _lines(refund_contribution_postings(D("5.00"))) == {
        ("protection_pool", "dr", D("5.00")),
        ("platform_cash", "cr", D("5.00")),
    }
    assert _lines(payout_postings(D("5.00"))) == {
        ("vendor_payable", "dr", D("5.00")),
        ("platform_cash", "cr", D("5.00")),
    }


def test_deduct_splits_and_caps() -> None:
    assert _lines(deposit_deduct_postings(D("100.00"), D("30.00"))) == {
        ("renter_deposit_liability", "dr", D("100.00")),
        ("vendor_payable", "cr", D("30.00")),
        ("platform_cash", "cr", D("70.00")),
    }
    capped = deposit_deduct_postings(D("100.00"), D("250.00"))
    assert _lines(capped) == {
        ("renter_deposit_liability", "dr", D("100.00")),
        ("vendor_payable", "cr", D("100.00")),
    }
    assert LedgerAccount.platform_cash not in {p.account for p in capped}


@pytest.mark.parametrize(
    ("recipe", "args"),
    [
        (deposit_captured_postings, ("0", "0")),
        (balance_captured_postings, ("0", "0", "0")),
        (balance_refund_postings, ("0", "0", "0")),
        (deposit_release_postings, ("0",)),
        (deposit_deduct_postings, ("0", "10.00")),
        (deposit_forfeit_postings, ("0",)),
        (refund_contribution_postings, ("0.00",)),
        (payout_postings, ("0",)),
    ],
)
def test_zero_total_returns_empty(recipe, args) -> None:
    assert recipe(*(D(a) for a in args)) == []


@pytest.mark.parametrize(
    ("recipe", "args"),
    [
        (deposit_captured_postings, ("-0.01", "0")),
        (deposit_captured_postings, ("1.00", "-1.00")),
        (balance_captured_postings, ("1.00", "-0.01", "0")),
        (balance_captured_postings, ("1.00", "0", "-0.01")),
        (balance_refund_postings, ("-0.01", "0", "0")),
        (balance_refund_postings, ("1.00", "0", "-0.01")),
        (deposit_release_postings, ("-1",)),
        (deposit_deduct_postings, ("10.00", "-1.00")),
        (deposit_forfeit_postings, ("-1",)),
        (refund_contribution_postings, ("-1",)),
        (payout_postings, ("-1",)),
    ],
)
def test_negative_amounts_rejected(recipe, args) -> None:
    with pytest.raises(ValueError):
        recipe(*(D(a) for a in args))


def test_recipes_reject_non_decimal() -> None:
    with pytest.raises(TypeError):
        deposit_release_postings(10.0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rental", "rate", "expected"),
    [
        ("150000.00", "0.15", "22500.00"),
        ("0.03", "0.15", "0.00"),
        ("0.10", "0.15", "0.02"),  # 0.015 rounds half up
        ("33333.33", "0.15", "5000.00"),
    ],
)
def test_commission_amount_rounds_half_up(rental, rate, expected) -> None:
    assert commission_amount(D(rental), D(rate)) == D(expected)
