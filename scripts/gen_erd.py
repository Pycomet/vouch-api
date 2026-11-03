"""Generate ``docs/erd.mmd``, a Mermaid ``erDiagram`` of the ORM schema.

Usage::

    python scripts/gen_erd.py            # rewrite docs/erd.mmd
    python scripts/gen_erd.py --check    # exit 1 if docs/erd.mmd is out of date

Then render the picture (needs Node; downloads a headless Chromium on first use)::

    npx -y @mermaid-js/mermaid-cli -i docs/erd.mmd -o docs/erd.svg

:func:`render` reads ``Base.metadata`` (every table the models register) and is
deterministic: tables, columns in table order, and relationships sorted. Each
column shows its Postgres type (simplified to a Mermaid-safe word), its name,
and ``PK`` / ``FK`` / ``UK`` markers; a nullable column says ``"null"``. Each
foreign key is one relationship, labelled with its local column names:

* ``||--o|``: a unique, NOT NULL foreign key (at most one child per parent);
* ``||--o{``: a NOT NULL foreign key (any number of children);
* ``|o--o{``: a nullable foreign key (the child may have no parent).

The ORM metadata is the source. Objects only the migrations create (the GIN
expression index, the trigger functions, the triggers) are described in
``docs/SCHEMA.md``, not in the diagram.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import Column, ForeignKeyConstraint, MetaData, Table, UniqueConstraint
from sqlalchemy.dialects import postgresql

import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base

ERD_PATH = Path(__file__).parents[1] / "docs" / "erd.mmd"

_DIALECT = postgresql.dialect()
_TYPE_ALIASES = {
    "TIMESTAMP WITH TIME ZONE": "timestamptz",
    "TIMESTAMP WITHOUT TIME ZONE": "timestamp",
    "CHARACTER VARYING": "varchar",
}


def _type_name(column: Column) -> str:
    """The column's Postgres type as one Mermaid-safe word (``numeric_12_2``)."""
    compiled = column.type.compile(dialect=_DIALECT)
    compiled = _TYPE_ALIASES.get(compiled, compiled)
    compiled = compiled.replace("[]", "_array")
    return re.sub(r"[^a-z0-9]+", "_", compiled.lower()).strip("_")


def _unique_column_sets(table: Table) -> set[frozenset[str]]:
    sets = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    sets.update(frozenset({column.name}) for column in table.columns if column.unique)
    return sets


def _keys(table: Table, column: Column) -> str:
    keys = []
    if column.primary_key:
        keys.append("PK")
    if column.foreign_keys:
        keys.append("FK")
    if frozenset({column.name}) in _unique_column_sets(table):
        keys.append("UK")
    return ", ".join(keys)


def _attribute(table: Table, column: Column) -> str:
    line = f"        {_type_name(column)} {column.name}"
    keys = _keys(table, column)
    if keys:
        line += f" {keys}"
    if column.nullable and not column.primary_key:
        line += ' "null"'
    return line


def _cardinality(table: Table, fk: ForeignKeyConstraint) -> str:
    columns = frozenset(column.name for column in fk.columns)
    if any(column.nullable for column in fk.columns):
        return "|o--o{"
    if columns in _unique_column_sets(table):
        return "||--o|"
    return "||--o{"


def _relationships(metadata: MetaData) -> list[str]:
    lines = set()
    for table in metadata.tables.values():
        for fk in table.foreign_key_constraints:
            label = ", ".join(column.name for column in fk.columns)
            parent = fk.referred_table.name
            lines.add(f'    {parent} {_cardinality(table, fk)} {table.name} : "{label}"')
    return sorted(lines)


def render(metadata: MetaData | None = None) -> str:
    """The Mermaid ``erDiagram`` source for ``metadata`` (default: ``Base.metadata``)."""
    metadata = metadata if metadata is not None else Base.metadata
    lines = ["erDiagram"]
    for name in sorted(metadata.tables):
        table = metadata.tables[name]
        lines.append(f"    {name} {{")
        lines.extend(_attribute(table, column) for column in table.columns)
        lines.append("    }")
    lines.extend(_relationships(metadata))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if docs/erd.mmd is stale")
    args = parser.parse_args(argv)
    source = render()
    if args.check:
        current = ERD_PATH.read_text(encoding="utf-8") if ERD_PATH.exists() else ""
        if current != source:
            print(f"{ERD_PATH} is out of date; run python scripts/gen_erd.py", file=sys.stderr)
            return 1
        return 0
    ERD_PATH.write_text(source, encoding="utf-8")
    print(f"wrote {ERD_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
