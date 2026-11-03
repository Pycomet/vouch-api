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
