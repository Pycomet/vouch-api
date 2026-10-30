"""Shared FastAPI dependencies."""

from __future__ import annotations

from app.core.config import get_settings


def get_launched_cities() -> list[str]:
    """City slugs open to the public (``VOUCH_LAUNCHED_CITIES``).

    A dependency rather than a direct settings read, so tests can override it
    through ``app.dependency_overrides``.
    """
    return list(get_settings().launched_cities)
