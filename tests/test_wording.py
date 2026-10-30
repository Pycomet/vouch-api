"""Guard the product wording rule: deposits are a "deposit hold", never the banned terms.

The banned words are assembled from fragments so this file does not trip its own check.
``docs/specs/M2.md`` is the verbatim Milestone 2 spec and is excluded.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNED = ("src", "docs", "migrations", "tests", "README.md", ".env.example")
EXCLUDED = {ROOT / "docs" / "specs" / "M2.md"}
TEXT_SUFFIXES = {".py", ".md", ".mako", ".ini", ".toml", ".yml", ".yaml", ".example", ""}
BANNED = re.compile(r"\b(" + "esc" + "row|" + "insur" + r"ance)\b", re.IGNORECASE)


def _files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED:
        path = ROOT / entry
        candidates = [path] if path.is_file() else path.rglob("*")
        for candidate in candidates:
            if (
                candidate.is_file()
                and candidate.suffix in TEXT_SUFFIXES
                and "__pycache__" not in candidate.parts
                and candidate not in EXCLUDED
            ):
                files.append(candidate)
    return files


def test_no_banned_words() -> None:
    hits = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in _files()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if BANNED.search(line)
    ]
    assert hits == []
