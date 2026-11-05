"""Response body of the public catalogue lookup (M3a 4.1)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class CatalogueItemOut(BaseModel):
    """One active catalogue item a vendor can list."""

    id: uuid.UUID
    category: str = Field(description="Category slug.")
    make: str
    model: str
    mount: str | None = Field(description="Lens mount; null for non-optics.")
    specs: dict[str, Any]
    replacement_value_band: str = Field(description="A, B or C.")
