"""Live PostgreSQL tests for ``python -m app.cli seed-catalogue`` (M3a §4.2).

The command owns its transaction, so these tests use ``migrated_db`` and check the
committed rows with a direct asyncpg connection.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import cli
from tests.postgres.factories import create_asset

pytestmark = pytest.mark.skipif(
    not os.getenv("VOUCH_TEST_DATABASE_URL"),
    reason="VOUCH_TEST_DATABASE_URL is required for live PostgreSQL checks",
)

ROOT = Path(__file__).parents[2]
SAMPLE = ROOT / "data" / "catalogue_seed.sample.csv"
HEADER = "category,make,model,mount,band,aliases\n"


def _file(tmp_path: Path, body: str, header: str = HEADER) -> str:
    path = tmp_path / "seed.csv"
    path.write_text(header + body, encoding="utf-8")
    return str(path)


async def _items(url: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(
            "SELECT c.slug AS category, i.make, i.model, i.mount, "
            "i.replacement_value_band AS band, i.aliases "
            "FROM catalogue_items i JOIN categories c ON c.id = i.category_id "
            "ORDER BY i.make, i.model"
        )
    finally:
        await connection.close()


async def _audit_count(url: str) -> int:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetchval(
            "SELECT count(*) FROM audit_events WHERE entity = 'catalogue_item'"
        )
    finally:
        await connection.close()


async def _seed(path: str) -> int:
    return await cli.run(["seed-catalogue", "--file", path])


async def test_inserts_then_reruns_unchanged(migrated_db: str, tmp_path, capsys) -> None:
    path = _file(
        tmp_path,
        "camera,Acme,Body 1,,B,body one|b1\nlens,Acme,Prime 50,E,A,\n",
    )

    assert await _seed(path) == 0
    assert capsys.readouterr().out.strip() == "inserted=2 updated=0 unchanged=0"
    rows = await _items(migrated_db)
    assert [(r["category"], r["make"], r["model"], r["mount"], r["band"]) for r in rows] == [
        ("camera", "Acme", "Body 1", None, "B"),
        ("lens", "Acme", "Prime 50", "E", "A"),
    ]
    assert rows[0]["aliases"] == ["body one", "b1"]
    assert rows[1]["aliases"] == []

    assert await _seed(path) == 0
    assert capsys.readouterr().out.strip() == "inserted=0 updated=0 unchanged=2"
    assert len(await _items(migrated_db)) == 2


async def test_changed_row_is_updated(migrated_db: str, tmp_path, capsys) -> None:
    assert await _seed(_file(tmp_path, "camera,Acme,Body 1,,B,\nlens,Acme,Prime,E,A,\n")) == 0
    capsys.readouterr()

    changed = _file(tmp_path, "camera,Acme,Body 1,,C,new alias\nlens,Acme,Prime,E,A,\n")
    assert await _seed(changed) == 0

    assert capsys.readouterr().out.strip() == "inserted=0 updated=1 unchanged=1"
    rows = {r["model"]: r for r in await _items(migrated_db)}
    assert rows["Body 1"]["band"] == "C" and rows["Body 1"]["aliases"] == ["new alias"]


async def test_comment_lines_and_blank_lines_are_skipped(
    migrated_db: str, tmp_path, capsys
) -> None:
    body = "# SAMPLE data\n\ncamera,Acme,Body 1,,B,\n"
    assert await _seed(_file(tmp_path, body, header="# note\n" + HEADER)) == 0
    assert capsys.readouterr().out.strip() == "inserted=1 updated=0 unchanged=0"


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("nope,Acme,Bad,,B,", "category"),
        ("camera,Acme,Bad,,Z,", "band"),
        ("camera,,Bad,,B,", "make"),
        ("camera,Acme,,,B,", "model"),
        ("camera,Acme,Bad,B", "columns"),
    ],
)
async def test_bad_row_rejects_whole_file_naming_the_line(
    migrated_db: str, tmp_path, capsys, row: str, fragment: str
) -> None:
    before = len(await _items(migrated_db))
    # Line 1 header, line 2 good, line 3 bad.
    path = _file(tmp_path, f"camera,Acme,Good,,B,\n{row}\n")

    assert await _seed(path) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "line 3" in captured.err and fragment in captured.err
    assert len(await _items(migrated_db)) == before
    assert await _audit_count(migrated_db) == 0


async def test_duplicate_make_model_in_file_is_rejected(migrated_db: str, tmp_path, capsys) -> None:
    path = _file(tmp_path, "camera,Acme,Dup,,B,\nlens,Acme,Dup,E,A,\n")

    assert await _seed(path) == 1

    assert "line 3" in capsys.readouterr().err
    assert await _items(migrated_db) == []


async def test_bad_header_is_rejected(migrated_db: str, tmp_path, capsys) -> None:
    path = _file(tmp_path, "camera,Acme,Body,,B,\n", header="category,make,model\n")

    assert await _seed(path) == 1
    assert "line 1" in capsys.readouterr().err


async def test_missing_file_is_a_usage_error(migrated_db: str, tmp_path, capsys) -> None:
    assert await _seed(str(tmp_path / "absent.csv")) == 2
    assert "error" in capsys.readouterr().err


async def test_writes_one_audit_event_per_changed_row(migrated_db: str, tmp_path) -> None:
    path = _file(tmp_path, "camera,Acme,A,,B,\ncamera,Acme,B,,B,\n")
    assert await _seed(path) == 0
    assert await _audit_count(migrated_db) == 2
    assert await _seed(path) == 0
    assert await _audit_count(migrated_db) == 2


async def test_sample_file_loads_ten_rows_across_all_categories(migrated_db: str, capsys) -> None:
    assert await _seed(str(SAMPLE)) == 0
    assert capsys.readouterr().out.strip() == "inserted=10 updated=0 unchanged=0"
    rows = await _items(migrated_db)
    assert len(rows) == 10
    assert {r["category"] for r in rows} == {
        "camera",
        "lens",
        "lighting",
        "audio",
        "drone",
        "gimbal",
        "accessory",
    }
    assert "sample" in SAMPLE.read_text(encoding="utf-8").lower()


async def test_category_change_of_a_used_item_is_refused_and_nothing_is_written(
    migrated_db: str, tmp_path, capsys
) -> None:
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with AsyncSession(engine) as session:
            await create_asset(session, category_slug="camera", make="Acme", model="Body 1")
            await session.commit()
    finally:
        await engine.dispose()

    # Line 2 is new and valid; line 3 moves the used item from camera to lens.
    path = _file(tmp_path, "camera,Acme,Fresh,,B,\nlens,Acme,Body 1,E,B,\n")

    assert await _seed(path) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "line 3" in captured.err and "assets use this item" in captured.err
    assert "SELECT" not in captured.err and "INSERT" not in captured.err
    rows = await _items(migrated_db)
    assert [(r["model"], r["category"]) for r in rows] == [("Body 1", "camera")]
    assert await _audit_count(migrated_db) == 0


async def test_unchanged_category_of_a_used_item_is_fine(
    migrated_db: str, tmp_path, capsys
) -> None:
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with AsyncSession(engine) as session:
            await create_asset(session, category_slug="camera", make="Acme", model="Body 1")
            await session.commit()
    finally:
        await engine.dispose()

    assert await _seed(_file(tmp_path, "camera,Acme,Body 1,,C,\n")) == 0
    assert capsys.readouterr().out.strip() == "inserted=0 updated=1 unchanged=0"


async def test_quoted_fields_with_commas_and_newlines_keep_line_numbers(
    migrated_db: str, tmp_path, capsys
) -> None:
    # Header is line 1; the quoted aliases span lines 2-3; the bad row is line 4.
    body = 'camera,Acme,Body 1,,B,"one, with comma|two\nsecond line"\ncamera,Acme,Bad,,Z,\n'
    assert await _seed(_file(tmp_path, body)) == 1
    assert "line 4" in capsys.readouterr().err
    assert await _items(migrated_db) == []

    good = 'camera,Acme,Body 1,,B,"one, with comma|two"\n'
    assert await _seed(_file(tmp_path, good)) == 0
    assert (await _items(migrated_db))[0]["aliases"] == ["one, with comma", "two"]


@pytest.mark.parametrize(
    ("row", "fragment"),
    [("Camera,Acme,X,,B,", "case-sensitive"), ("camera,Acme,X,,b,", "case-sensitive")],
)
async def test_case_errors_list_the_allowed_values(
    migrated_db: str, tmp_path, capsys, row: str, fragment: str
) -> None:
    assert await _seed(_file(tmp_path, row + "\n")) == 1
    err = capsys.readouterr().err
    assert fragment in err and ("accessory" in err or "A, B, C" in err)
