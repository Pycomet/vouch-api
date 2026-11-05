"""The service layer stays free of the web framework.

Services raise domain errors (``app.schemas.errors``); ``app.main`` turns them
into HTTP responses. A service that imported FastAPI would tie the rules Step 4's
admin routes and the CLI reuse to one transport.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICES = Path(__file__).parents[1] / "src" / "app" / "services"
_IMPORT = re.compile(r"^\s*(from|import)\s+(fastapi|starlette)\b", re.MULTILINE)


def test_no_service_imports_fastapi() -> None:
    offenders = sorted(
        path.name for path in SERVICES.rglob("*.py") if _IMPORT.search(path.read_text("utf-8"))
    )
    assert offenders == []
