"""The price snapshot frozen on a booking at request time (spec 2.4 and 3.1).

Rate basis, in order:

* ``weekly`` when the span is at least seven days and the asset has a weekly rate:
  ``full_weeks * weekly_rate + remaining_days * daily_rate``.
* ``weekend`` when the span is exactly Friday to Sunday and the asset has a
  weekend rate: the weekend rate.
* ``daily`` otherwise: ``daily_rate * days``.

Days are inclusive of both ends. ``rental_total`` is the rental alone; the
delivery fee (from ``assets.fulfilment.delivery_fee``, default 0, charged only
for delivery) and the damage contribution (``damage_contribution_rate`` of the
rental, when chosen) are separate keys. Every value is a string, money as
``"150000.00"`` (amendment A8 placeholders come from settings).
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.core.config import Settings
from app.db.models import Asset

CENT = Decimal("0.01")
CURRENCY = "NGN"
_FRIDAY = 4
_WEEK = 7

Fulfilment = Literal["pickup", "delivery"]


def money(value: Decimal | int | str) -> Decimal:
    """``value`` as a Decimal quantized to the cent, half up."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else str(money(value))


def _rental(asset: Asset, starts_on: date, days: int) -> tuple[str, Decimal]:
    daily = Decimal(asset.daily_rate)
    if days >= _WEEK and asset.weekly_rate is not None:
        full_weeks, remaining = divmod(days, _WEEK)
        return "weekly", full_weeks * Decimal(asset.weekly_rate) + remaining * daily
    if days == 3 and starts_on.weekday() == _FRIDAY and asset.weekend_rate is not None:
        return "weekend", Decimal(asset.weekend_rate)
    return "daily", daily * days


def price_snapshot(
    asset: Asset,
    starts_on: date,
    ends_on: date,
    fulfilment: Fulfilment,
    damage_contribution: bool,
    settings: Settings,
) -> dict[str, str]:
    """The booking's frozen prices for ``[starts_on, ends_on]``, all as strings."""
    if ends_on < starts_on:
        raise ValueError("ends_on must not be before starts_on")
    days = (ends_on - starts_on).days + 1
    basis, rental = _rental(asset, starts_on, days)
    rental = money(rental)

    delivery_fee = Decimal(0)
    if fulfilment == "delivery":
        delivery_fee = Decimal(str((asset.fulfilment or {}).get("delivery_fee", 0)))

    contribution = (
        money(rental * settings.damage_contribution_rate) if damage_contribution else money(0)
    )

    snapshot = {
        "rate_basis": basis,
        "days": str(days),
        "daily_rate": _fmt(asset.daily_rate),
        "weekend_rate": _fmt(asset.weekend_rate),
        "weekly_rate": _fmt(asset.weekly_rate),
        "rental_total": str(rental),
        "delivery_fee": str(money(delivery_fee)),
        "deposit": str(money(asset.deposit_amount)),
        "commission_rate": str(settings.commission_rate),
        "damage_contribution": str(contribution),
        "currency": CURRENCY,
    }
    return {key: value for key, value in snapshot.items() if value is not None}
