"""Guard the coverage ratchet in ``[tool.coverage.run] omit``.

The repository measures coverage from the root (``source = ["."]``) and enforces
a 100% floor. Files that are inside that surface but not yet fully tested are
listed in a delimited "coverage ratchet" block inside ``omit`` so the floor can
apply to everything else today. The list is meant to shrink to nothing.

Nothing stops a future change from *adding* to it, so these tests make the
weakening visible and mechanical instead of silent:

* entries must name real files, spelled exactly as coverage reports them, so a
  renamed or deleted module cannot leave a stale hole in the gate;
* entries may never live under ``gco/``, ``cli/`` or ``gco_mcp/`` — those
  packages are already at 100% and the ratchet must not be used to lower them;
* the block stays sorted and duplicate-free so review diffs are readable.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

BEGIN_MARKER = "# coverage ratchet: BEGIN"
END_MARKER = "# coverage ratchet: END"

# Packages the pre-existing gate already holds at 100%.
PROTECTED_PREFIXES = ("gco/", "cli/", "gco_mcp/")


def _pyproject_text() -> str:
    return PYPROJECT.read_text(encoding="utf-8")


def _ratchet_entries() -> list[str]:
    """Return the quoted paths between the ratchet markers, in file order."""
    text = _pyproject_text()
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER)
    block = text[start:end]
    entries: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith('"') and line.endswith('",'):
            entries.append(line[1:-2])
    return entries


def _omit_list() -> list[str]:
    data = tomllib.loads(_pyproject_text())
    omit = data["tool"]["coverage"]["run"]["omit"]
    assert isinstance(omit, list)
    return [str(item) for item in omit]


def test_ratchet_block_is_delimited_exactly_once() -> None:
    """Both markers appear once, in order, so the block is unambiguous."""
    text = _pyproject_text()
    assert text.count(BEGIN_MARKER) == 1, f"expected one {BEGIN_MARKER!r}"
    assert text.count(END_MARKER) == 1, f"expected one {END_MARKER!r}"
    assert text.index(BEGIN_MARKER) < text.index(END_MARKER), (
        "the ratchet END marker precedes BEGIN; the block is inverted"
    )


def test_ratchet_entries_name_files_that_exist() -> None:
    """A renamed or deleted module must not leave a stale hole in the gate."""
    missing = [entry for entry in _ratchet_entries() if not (PROJECT_ROOT / entry).is_file()]
    assert not missing, (
        "coverage ratchet names paths that no longer exist: "
        f"{missing}. Delete them — the floor already applies."
    )


def test_ratchet_entries_are_python_files() -> None:
    """The ratchet excuses Python modules, not directories or glob patterns."""
    offenders = [entry for entry in _ratchet_entries() if not entry.endswith(".py") or "*" in entry]
    assert not offenders, (
        f"coverage ratchet entries must be exact .py paths, not patterns: {offenders}"
    )


def test_ratchet_never_covers_the_already_enforced_packages() -> None:
    """gco/, cli/ and gco_mcp/ are at 100%; the ratchet cannot lower them."""
    offenders = [entry for entry in _ratchet_entries() if entry.startswith(PROTECTED_PREFIXES)]
    assert not offenders, (
        "coverage ratchet must not exempt already-covered packages "
        f"{PROTECTED_PREFIXES}: {offenders}"
    )


def test_ratchet_is_sorted_and_unique() -> None:
    """Keeps review diffs one-line-per-change and blocks accidental repeats."""
    entries = _ratchet_entries()
    duplicates = sorted({entry for entry in entries if entries.count(entry) > 1})
    assert not duplicates, f"coverage ratchet lists duplicates: {duplicates}"
    assert entries == sorted(entries), (
        "coverage ratchet must stay sorted; run sorted() over the block"
    )


def test_ratchet_entries_are_part_of_the_omit_list() -> None:
    """The delimited block must be inside ``omit``, not stranded in a comment."""
    omit = set(_omit_list())
    stranded = [entry for entry in _ratchet_entries() if entry not in omit]
    assert not stranded, f"ratchet entries are not present in [tool.coverage.run] omit: {stranded}"


def test_coverage_floor_and_root_source_are_still_in_force() -> None:
    """The ratchet is only defensible while the floor and root source remain."""
    data = tomllib.loads(_pyproject_text())
    assert data["tool"]["coverage"]["run"]["source"] == ["."], (
        "coverage must measure the repository root; a hand-kept package list "
        "lets new directories escape the floor"
    )
    assert data["tool"]["coverage"]["report"]["fail_under"] == 100
    assert data["tool"]["coverage"]["report"]["include_namespace_packages"] is True, (
        "without include_namespace_packages, Lambda handlers and .github/scripts "
        "are invisible to coverage and 0% reads as 100%"
    )
