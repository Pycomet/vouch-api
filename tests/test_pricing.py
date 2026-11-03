"""Price snapshot rules (Task 3.2): rate basis, delivery fee, damage contribution."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.config import Settings
from app.db.models import Asset
from app.services.pricing import price_snapshot

SETTINGS = Settings(_env_file=None)
MONDAY = date(2027, 1, 4)
FRIDAY = date(2027, 1, 8)
SUNDAY = date(2027, 1, 10)


def _asset(**overrides) -> Asset:
    values = {
        "daily_rate": Decimal("10000.00"),
        "deposit_amount": Decimal("150000.00"),
        "weekend_rate": None,
        "weekly_rate": None,
        "fulfilment": {"pickup": True, "delivery": True, "delivery_fee": "2500"},
    }
    values.update(overrides)
    return Asset(**values)


def test_price_daily() -> None:
    snapshot = price_snapshot(
        _asset(), MONDAY, date(2027, 1, 6), "pickup", damage_contribution=False, settings=SETTINGS
    )

    assert snapshot["rate_basis"] == "daily"
    assert snapshot["days"] == "3"
    assert snapshot["rental_total"] == "30000.00"
    assert snapshot["delivery_fee"] == "0.00"
    assert snapshot["deposit"] == "150000.00"
    assert snapshot["commission_rate"] == str(SETTINGS.commission_rate)
    assert snapshot["damage_contribution"] == "0.00"
    assert snapshot["currency"] == "NGN"
    assert all(isinstance(value, str) for value in snapshot.values())


def test_price_weekend() -> None:
    asset = _asset(weekend_rate=Decimal("22000.00"))

    snapshot = price_snapshot(asset, FRIDAY, SUNDAY, "delivery", False, SETTINGS)

    assert snapshot["rate_basis"] == "weekend"
    assert snapshot["weekend_rate"] == "22000.00"
    assert snapshot["rental_total"] == "22000.00"
    assert snapshot["delivery_fee"] == "2500.00"


def test_price_weekend_without_rate_falls_back_to_daily() -> None:
    snapshot = price_snapshot(_asset(), FRIDAY, SUNDAY, "pickup", False, SETTINGS)

    assert snapshot["rate_basis"] == "daily"
    assert snapshot["rental_total"] == "30000.00"


def test_price_weekend_rate_ignored_off_fri_to_sun() -> None:
    asset = _asset(weekend_rate=Decimal("22000.00"))

    snapshot = price_snapshot(asset, date(2027, 1, 9), date(2027, 1, 11), "pickup", False, SETTINGS)

    assert snapshot["rate_basis"] == "daily"


def test_price_weekly_plus_days() -> None:
    asset = _asset(weekly_rate=Decimal("55000.00"))

    # 10 days inclusive: one full week and three days.
    snapshot = price_snapshot(asset, MONDAY, date(2027, 1, 13), "pickup", False, SETTINGS)

    assert snapshot["rate_basis"] == "weekly"
    assert snapshot["days"] == "10"
    assert snapshot["weekly_rate"] == "55000.00"
    assert snapshot["rental_total"] == "85000.00"


def test_price_long_span_without_weekly_rate_is_daily() -> None:
    snapshot = price_snapshot(_asset(), MONDAY, date(2027, 1, 13), "pickup", False, SETTINGS)

    assert snapshot["rate_basis"] == "daily"
    assert snapshot["rental_total"] == "100000.00"


def test_damage_contribution_quantized() -> None:
    asset = _asset(daily_rate=Decimal("3333.33"))
    settings = Settings(_env_file=None, damage_contribution_rate=Decimal("0.05"))

    snapshot = price_snapshot(asset, MONDAY, MONDAY, "pickup", True, settings)

    # 3333.33 * 0.05 = 166.6665 -> 166.67 (ROUND_HALF_UP)
    assert snapshot["damage_contribution"] == "166.67"
    assert snapshot["rental_total"] == "3333.33"


def test_delivery_fee_defaults_to_zero() -> None:
    asset = _asset(fulfilment={"pickup": True, "delivery": True})

    snapshot = price_snapshot(asset, MONDAY, MONDAY, "delivery", False, SETTINGS)

    assert snapshot["delivery_fee"] == "0.00"
