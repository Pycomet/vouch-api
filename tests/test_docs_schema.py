"""The schema docs stay in step with the ORM metadata (Milestone 2, Step 6).

* ``docs/erd.mmd`` is exactly what ``scripts/gen_erd.py`` renders from
  ``Base.metadata``: regenerate it with ``python scripts/gen_erd.py`` after a
  schema change (then re-render ``docs/erd.svg`` with mermaid-cli, see README).
* Every table has an entity in the ERD and every foreign key a relationship.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base

ROOT = Path(__file__).parents[1]
ERD_SOURCE = ROOT / "docs" / "erd.mmd"
GEN_ERD = ROOT / "scripts" / "gen_erd.py"


def _gen_erd() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_erd", GEN_ERD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_erd_is_current() -> None:
    assert _gen_erd().render() == ERD_SOURCE.read_text(encoding="utf-8")


def test_erd_render_is_deterministic() -> None:
    gen_erd = _gen_erd()
    assert gen_erd.render() == gen_erd.render()


def test_schema_doc_lists_every_table() -> None:
    """Every table is a heading in SCHEMA.md (``### `name` ``) and an ERD entity."""
    schema = (ROOT / "docs" / "SCHEMA.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^### `([a-z_]+)`", schema, flags=re.MULTILINE))
    erd = ERD_SOURCE.read_text(encoding="utf-8")
    entities = set(re.findall(r"^    ([a-z_]+) \{$", erd, flags=re.MULTILINE))
    tables = set(Base.metadata.tables)
    assert tables - headings == set()
    assert headings - tables == set()
    assert tables == entities


def _error_codes() -> set[str]:
    from app.schemas import errors

    codes: set[str] = set()
    pending = [errors.VouchAPIError]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        codes.add(cls.code)
    codes.update(errors._GUARD_MESSAGES)
    # The base classes' defaults: never sent (a guard always has its own code).
    return codes - {errors.VouchAPIError.code, errors.BookingGuardFailed.code}


def test_api_contract_lists_every_error_code() -> None:
    contract = (ROOT / "docs" / "API_CONTRACT.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| \d{3} \| `([a-z_]+)` \|", contract, flags=re.MULTILINE))
    assert _error_codes() - documented == set()


def _path_shape(path: str) -> str:
    """``/bookings/{booking_id}`` and ``/bookings/{id}`` both become ``/bookings/{}``."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def test_api_contract_lists_every_endpoint() -> None:
    """Every OpenAPI operation is a row of the contract's endpoint table."""
    from app.core.config import Settings
    from app.main import create_app

    settings = Settings()
    spec = create_app(settings).openapi()
    prefix = settings.api_v1_prefix
    operations = {
        (method.upper(), _path_shape(path.removeprefix(prefix)))
        for path, methods in spec["paths"].items()
        for method in methods
    }
    contract = (ROOT / "docs" / "API_CONTRACT.md").read_text(encoding="utf-8")
    rows = re.findall(
        r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \|", contract, flags=re.MULTILINE
    )
    documented = {(method, _path_shape(path)) for method, path in rows}
    assert operations - documented == set()


def test_schema_doc_lists_every_transition() -> None:
    """Every ``(status, event)`` of the booking registry is a row of SCHEMA.md §5."""
    from app.services.booking import _TRANSITIONS

    schema = (ROOT / "docs" / "SCHEMA.md").read_text(encoding="utf-8")
    rows = set(re.findall(r"^\| `([a-z_]+)` \| `([a-z_]+)` \|", schema, flags=re.MULTILINE))
    registered = {(status.value, event.value) for status, event in _TRANSITIONS}
    assert registered - rows == set()


def test_erd_has_every_table_and_foreign_key() -> None:
    text = ERD_SOURCE.read_text(encoding="utf-8")
    entities = set(re.findall(r"^    ([a-z_]+) \{$", text, flags=re.MULTILINE))
    assert entities == set(Base.metadata.tables)

    relationships = re.findall(
        r"^    ([a-z_]+) \S+ ([a-z_]+) : \"([a-z_, ]+)\"$", text, flags=re.MULTILINE
    )
    found = {(parent, child, label) for parent, child, label in relationships}
    for table in Base.metadata.tables.values():
        for fk in table.foreign_key_constraints:
            label = ", ".join(column.name for column in fk.columns)
            assert (fk.referred_table.name, table.name, label) in found, (table.name, label)
