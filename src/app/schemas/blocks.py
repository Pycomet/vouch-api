"""Request and response bodies of availability blocks (M3a 4.6).

A block is an ``availability_windows`` row with ``is_blocked = true``. The table's
columns are ``starts_at``/``ends_at`` (dates); the API calls them ``starts_on`` and
``ends_on``, like a booking, and both ends are inclusive.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class BlockCreate(BaseModel):
    """The dates to block, inclusive. Unknown fields are 422."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"starts_on": "2026-12-24", "ends_on": "2026-12-27"}]},
    )

    starts_on: datetime.date
    ends_on: datetime.date


class Block(BaseModel):
    """One blocked span of a listing."""

    id: uuid.UUID
    asset_id: uuid.UUID
    starts_on: datetime.date
    ends_on: datetime.date
    created_at: datetime.datetime


class BlockList(BaseModel):
    """A listing's blocks, ordered by start date."""

    items: list[Block]
