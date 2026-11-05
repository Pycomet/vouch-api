"""Catalogue seed loader (M3a 4.2): validate a CSV, then upsert on ``(make, model)``.

Columns: ``category,make,model,mount,band,aliases`` (aliases separated by ``|``,
band ``A``, ``B`` or ``C``, category a known slug). Lines that are blank or start
with ``#`` are skipped, so a file can say it is sample data. The whole file is
validated before anything is written; the first bad row raises
:class:`SeedError` naming its line number (counting every physical line).

Only the seeded columns are compared and updated. ``specs`` and ``is_active`` are
left alone, so a catalogue item an admin deactivated stays deactivated.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, CatalogueItem
from app.db.models.catalogue import ValueBand
from app.db.models.reference import Category
from app.services.audit import Actor, record_audit

COLUMNS = ["category", "make", "model", "mount", "band", "aliases"]


class SeedError(Exception):
    """The file is rejected; the message names the line and is safe to print."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass(frozen=True)
class SeedRow:
    line: int
    category: str
    make: str
    model: str
    mount: str | None
    band: str
    aliases: list[str]


@dataclass(frozen=True)
class SeedCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


async def category_ids(session: AsyncSession) -> dict[str, int]:
    """Known category slugs and their ids."""
    result = await session.execute(select(Category.slug, Category.id))
    return {slug: cid for slug, cid in result.all()}


def parse_seed(text: str, known_categories: set[str] | frozenset[str]) -> list[SeedRow]:
    """The rows of ``text``, checked in line order for shape, category, band, blanks
    and duplicates; the first bad row raises :class:`SeedError`."""
    rows: list[SeedRow] = []
    seen: dict[tuple[str, str], int] = {}
    header_seen = False
    reader = csv.reader(io.StringIO(text))
    previous_end = 0
    while True:
        # A record may span lines (a quoted field); it starts after the previous one ended.
        number = previous_end + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise SeedError(number, f"unreadable row ({exc})") from exc
        previous_end = reader.line_num
        cells = [cell.strip() for cell in cells]
        if not cells or not any(cells) or cells[0].startswith("#"):
            continue
        if not header_seen:
            if cells != COLUMNS:
                raise SeedError(number, f"the header must be {','.join(COLUMNS)}")
            header_seen = True
            continue
        if len(cells) != len(COLUMNS):
            raise SeedError(number, f"expected {len(COLUMNS)} columns, found {len(cells)}")
        category, make, model, mount, band, aliases = cells
        if category not in known_categories:
            raise SeedError(
                number,
                f"unknown category {category!r} (case-sensitive; one of "
                f"{', '.join(sorted(known_categories))})",
            )
        if not make:
            raise SeedError(number, "make is required")
        if not model:
            raise SeedError(number, "model is required")
        if band not in {b.value for b in ValueBand}:
            raise SeedError(number, f"band must be one of A, B, C (case-sensitive), not {band!r}")
        if (make, model) in seen:
            raise SeedError(
                number, f"duplicate make and model (first seen on line {seen[(make, model)]})"
            )
        seen[(make, model)] = number
        alias_list = list(dict.fromkeys(a.strip() for a in aliases.split("|") if a.strip()))
        rows.append(SeedRow(number, category, make, model, mount or None, band, alias_list))
    if not header_seen:
        raise SeedError(1, f"the file is empty; the header must be {','.join(COLUMNS)}")
    return rows


async def apply_seed(session: AsyncSession, rows: list[SeedRow]) -> SeedCounts:
    """Upsert ``rows`` in the caller's transaction (flushes; the caller commits).

    Raises :class:`SeedError` before any write for an unknown category, or for a
    category change on an item that assets use (spec 5.4; the composite foreign key
    ``fk_assets_item_category`` would refuse it too, soft-deleted assets included).
    """
    categories = await category_ids(session)
    for row in rows:
        if row.category not in categories:
            raise SeedError(row.line, f"unknown category {row.category!r}")
        existing = (
            await session.scalars(
                select(CatalogueItem).where(
                    CatalogueItem.make == row.make, CatalogueItem.model == row.model
                )
            )
        ).first()
        if existing is not None and existing.category_id != categories[row.category]:
            used = await session.scalar(
                select(exists().where(Asset.catalogue_item_id == existing.id))
            )
            if used:
                raise SeedError(row.line, "category cannot change: assets use this item")

    inserted = updated = unchanged = 0
    for row in rows:
        values = {
            "category_id": categories[row.category],
            "mount": row.mount,
            "replacement_value_band": row.band,
            "aliases": row.aliases,
        }
        item = (
            await session.scalars(
                select(CatalogueItem)
                .where(CatalogueItem.make == row.make, CatalogueItem.model == row.model)
                .with_for_update()
            )
        ).first()
        if item is None:
            item = CatalogueItem(make=row.make, model=row.model, **values)
            session.add(item)
            await session.flush()
            await record_audit(
                session,
                actor=Actor.system(),
                entity="catalogue_item",
                entity_id=item.id,
                action="catalogue_item.seeded",
                before=None,
                after={"make": row.make, "model": row.model, **values},
            )
            inserted += 1
            continue
        before = {key: getattr(item, key) for key in values}
        if before == values:
            unchanged += 1
            continue
        changed = {key: value for key, value in values.items() if before[key] != value}
        for key, value in values.items():
            setattr(item, key, value)
        await session.flush()
        await record_audit(
            session,
            actor=Actor.system(),
            entity="catalogue_item",
            entity_id=item.id,
            action="catalogue_item.seed_updated",
            before={key: before[key] for key in changed},
            after=changed,
        )
        updated += 1
    return SeedCounts(inserted, updated, unchanged)
