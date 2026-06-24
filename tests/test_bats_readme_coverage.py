"""CI guard: every BATS suite must have a row in the BATS README table.

``tests/BATS/README.md`` carries a "What's Tested" table that the team treats
as the index of shell-script coverage. It's easy to land a new ``*.bats`` file
and forget the row (or delete a suite and leave a stale row behind), so this
guard keeps the table and the directory in sync.

The check is intentionally scoped to *table rows* — backtick-wrapped
``test_*.bats`` names on lines that start with ``|`` — so a passing mention in
prose (e.g. the ``test_my_script.bats`` placeholder in the "Adding New Tests"
section) neither satisfies the forward check nor trips the reverse one.
"""

from __future__ import annotations

import re
from pathlib import Path

_BATS_DIR = Path(__file__).resolve().parent / "BATS"
_README = _BATS_DIR / "README.md"
_ROW_NAME = re.compile(r"`(test_[A-Za-z0-9_]+\.bats)`")


def _bats_files() -> set[str]:
    return {p.name for p in _BATS_DIR.glob("*.bats")}


def _documented_in_table() -> set[str]:
    """Names from backtick-wrapped ``test_*.bats`` tokens on table-row lines."""
    names: set[str] = set()
    for line in _README.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("|"):
            names.update(_ROW_NAME.findall(line))
    return names


def test_every_bats_suite_has_a_readme_table_row() -> None:
    undocumented = sorted(_bats_files() - _documented_in_table())
    assert not undocumented, (
        "Undocumented BATS suite(s) — add a row to the 'What's Tested' table in "
        f"tests/BATS/README.md: {undocumented}"
    )


def test_readme_table_has_no_rows_for_missing_bats_suites() -> None:
    missing = sorted(_documented_in_table() - _bats_files())
    assert not missing, (
        "tests/BATS/README.md documents BATS suite(s) that no longer exist — "
        f"remove their row(s): {missing}"
    )
