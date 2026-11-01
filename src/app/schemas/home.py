"""Response bodies of the home screens (spec 3.3: ``/me/home``, ``/vendor/home``).

Money is a string with exactly two decimal places (``"127500.00"``); the
utilisation ratio is a string with two decimal places (``"0.43"``).
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from app.schemas.bookings import MoneyLine


class BookingCard(BaseModel):
    """One booking as a home screen shows it, with its money line."""

    id: uuid.UUID
    status: str
    asset_id: uuid.UUID
    asset_name: str = Field(description="The asset's display name, or its make and model.")
    starts_on: datetime.date
    ends_on: datetime.date
    hold_expires_at: datetime.datetime | None = Field(
        description="When an unanswered request lapses; null once answered."
    )
    money_line: MoneyLine


class RenterHome(BaseModel):
    """``GET /me/home``."""

    active: BookingCard | None = Field(
        description="The live booking that starts soonest, or null when there is none."
    )
    past: list[BookingCard] = Field(description="Closed bookings, newest first (at most 20).")


class VendorToday(BaseModel):
    handovers: list[BookingCard] = Field(description="Paid bookings starting today.")
    returns: list[BookingCard] = Field(description="Rentals that end today.")


class VendorHome(BaseModel):
    """``GET /vendor/home``."""

    requests: list[BookingCard] = Field(
        description="Unanswered requests, the one whose hold lapses first at the top."
    )
    today: VendorToday
    next_payout: str = Field(
        description="What the vendor's closed bookings still owe the vendor (NGN), net of "
        "payouts: completed rentals, the vendor's share of the cancellation fee when a "
        "renter cancels a paid booking, and the forfeited deposit when an item is lost.",
        examples=["127500.00"],
    )
    utilisation_7d: str = Field(
        description="Booked asset-days over today and the next six days, divided by "
        "active listings x 7.",
        examples=["0.43"],
    )
