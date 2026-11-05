"""Unit tests for the opaque cursor codec."""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.schemas.errors import CursorDecodeError
from app.schemas.pagination import decode_cursor, encode_cursor
from tests.helpers.cursors import CRAFTED_CURSORS


def test_encode_then_decode_round_trips_to_typed_values() -> None:
    id_ = uuid.uuid4()
    cursor = encode_cursor("2026-10-01T12:00:00+00:00", str(id_))
    decoded_at, decoded_id = decode_cursor(cursor)
    assert decoded_at == datetime.datetime(2026, 10, 1, 12, tzinfo=datetime.UTC)
    assert decoded_at.utcoffset() == datetime.timedelta(0)
    assert decoded_id == id_


def test_encode_produces_url_safe_string() -> None:
    cursor = encode_cursor("2026-10-01T12:00:00+00:00", str(uuid.uuid4()))
    assert "/" not in cursor
    assert "+" not in cursor


def test_decode_rejects_arbitrary_string() -> None:
    with pytest.raises(CursorDecodeError):
        decode_cursor("not-a-cursor")


def test_decode_rejects_empty_string() -> None:
    with pytest.raises(CursorDecodeError):
        decode_cursor("")


def test_decode_rejects_missing_fields() -> None:
    """A base64 JSON without the required keys must raise CursorDecodeError."""
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"foo": "bar"}).encode()).decode()
    with pytest.raises(CursorDecodeError):
        decode_cursor(payload)


@pytest.mark.parametrize("cursor", CRAFTED_CURSORS.values(), ids=CRAFTED_CURSORS.keys())
def test_decode_rejects_well_formed_cursors_with_bad_values(cursor) -> None:
    with pytest.raises(CursorDecodeError):
        decode_cursor(cursor)


def test_different_timestamps_produce_different_cursors() -> None:
    id_str = str(uuid.uuid4())
    c1 = encode_cursor("2026-10-01T12:00:00+00:00", id_str)
    c2 = encode_cursor("2026-10-02T12:00:00+00:00", id_str)
    assert c1 != c2
