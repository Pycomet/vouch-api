"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request


def get_launched_cities(request: Request) -> list[str]:
    """City slugs open to the public (``VOUCH_LAUNCHED_CITIES``).

    Read from the settings the running app was built with (``app.state.settings``),
    not from the process-wide cache. A dependency so tests can override it through
    ``app.dependency_overrides``.
    """
    return list(request.app.state.settings.launched_cities)
