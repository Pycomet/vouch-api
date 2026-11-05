"""Cursors that decode as base64 JSON with the right keys but the wrong values.

Every paginated route must answer each of them with 400 ``invalid_cursor``, never a
500 from a later ``fromisoformat`` or ``UUID`` call.
"""

from __future__ import annotations

import base64
import json

_ID = "6e7f8a9b-0c1d-4e2f-9a3b-4c5d6e7f8a9b"
_AT = "2026-10-01T12:00:00+00:00"


def _cursor(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


CRAFTED_CURSORS = {
    "both-garbage": _cursor({"created_at": "x", "id": "y"}),
    "bad-id": _cursor({"created_at": _AT, "id": "y"}),
    "bad-timestamp": _cursor({"created_at": "x", "id": _ID}),
    "naive-timestamp": _cursor({"created_at": "2026-10-01T12:00:00", "id": _ID}),
    "number-timestamp": _cursor({"created_at": 5, "id": _ID}),
    "not-an-object": _cursor([_AT, _ID]),
}
