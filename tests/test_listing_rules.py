"""The listing state machine and completeness rules (M3a 4.5), without a database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models.asset import Asset, ListingStatus
from app.db.models.catalogue import CatalogueItem
from app.schemas.errors import InvalidTransition
from app.services.listings import (
    COMMERCIAL_FIELDS,
    IDENTITY_FIELDS,
    TRANSITIONS,
    ListingEvent,
    band_default_tier,
    missing_for_review,
    next_status,
)

S, E = ListingStatus, ListingEvent


def test_the_table_is_the_spec_diagram() -> None:
    assert dict(TRANSITIONS) == {
        (S.draft, E.submit): S.review,
        (S.rejected, E.submit): S.review,
        (S.review, E.approve): S.active,
        (S.review, E.reject): S.rejected,
        (S.active, E.pause): S.paused,
        (S.paused, E.resume): S.active,
    }


@pytest.mark.parametrize("status", list(ListingStatus))
@pytest.mark.parametrize("event", list(ListingEvent))
def test_next_status_refuses_everything_else(status, event) -> None:
    if (status, event) in TRANSITIONS:
        assert next_status(status, event) == TRANSITIONS[(status, event)]
        return
    with pytest.raises(InvalidTransition) as raised:
        next_status(status, event)
    assert raised.value.error_details == {"from": status.value, "event": event.value}
    assert raised.value.detail_message == "This listing cannot make that transition."


def test_field_classes_do_not_overlap() -> None:
    assert IDENTITY_FIELDS == {"catalogue_item_id", "serial_number", "city"}
    assert not IDENTITY_FIELDS & COMMERCIAL_FIELDS
    assert "minimum_trust_tier" not in IDENTITY_FIELDS | COMMERCIAL_FIELDS


@pytest.mark.parametrize(("band", "tier"), [("A", 1), ("B", 2), ("C", 3)])
def test_band_default_tier(band, tier) -> None:
    assert band_default_tier(CatalogueItem(replacement_value_band=band)) == tier


def _asset(**overrides) -> Asset:
    values = {
        "serial_number": "SN-1",
        "condition_grade": "good",
        "daily_rate": Decimal("1.00"),
        "deposit_amount": Decimal("0.00"),
        "fulfilment": {"pickup": True, "delivery": False},
    }
    values.update(overrides)
    return Asset(**values)


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({}, []),
        ({"fulfilment": {"pickup": False, "delivery": True, "delivery_fee": "0.00"}}, []),
        ({"serial_number": ""}, ["serial_number"]),
        ({"condition_grade": None}, ["condition_grade"]),
        ({"daily_rate": Decimal("0")}, ["daily_rate"]),
        ({"deposit_amount": Decimal("-1")}, ["deposit_amount"]),
        ({"fulfilment": {"pickup": False, "delivery": False}}, ["fulfilment"]),
        ({"fulfilment": {"pickup": True, "delivery": True}}, ["delivery_fee"]),
        ({"fulfilment": None}, ["fulfilment"]),
    ],
)
def test_missing_for_review(overrides, missing) -> None:
    assert missing_for_review(_asset(**overrides)) == missing
