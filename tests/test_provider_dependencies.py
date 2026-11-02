"""No payment, KYC or SMS provider SDK is a dependency (R28, global constraints).

Every provider sits behind an interface with a fake; the real adapters come later
and must not arrive as a vendor SDK pulled in early.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"
PROVIDER_SDKS = frozenset(
    {
        "paystack",
        "paystackapi",
        "pypaystack",
        "flutterwave",
        "rave",
        "smile-id",
        "smileid",
        "dojah",
        "youverify",
        "prembly",
        "termii",
        "twilio",
    }
)


def _name(requirement: str) -> str:
    """The normalized distribution name of a PEP 508 requirement."""
    name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement).group(1)
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared() -> set[str]:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return {_name(requirement) for requirement in requirements}


def test_no_provider_sdk_dependencies() -> None:
    declared = _declared()

    assert "fastapi" in declared  # the parse found the real list
    assert declared & {_name(sdk) for sdk in PROVIDER_SDKS} == set()


def test_requirement_names_are_normalized() -> None:
    assert _name("Smile_ID>=1.0") == "smile-id"
    assert _name("sqlalchemy[asyncio]>=2.0") == "sqlalchemy"
