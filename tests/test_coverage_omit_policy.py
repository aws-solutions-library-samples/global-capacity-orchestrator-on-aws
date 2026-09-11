"""Guard what ``[tool.coverage.run] omit`` is allowed to exclude.

The repository measures coverage from the root (``source = ["."]``) and
enforces a 100% floor over every authored Python file. The only things
``omit`` may exclude are structural: the test-suite itself, package markers,
generated or vendored trees that can appear inside a checkout, and the
byte-identical copies of the two shared Lambda sources (whose canonical file is
measured and whose copies ``tests/test_lambda_shared_sources.py`` keeps in
lockstep).

Until every file reached 100% the list also carried a delimited "coverage
ratchet" of not-yet-covered sources. That block is gone, and these tests are
what keeps it gone: an ``omit`` entry that excuses any other source file — by
exact path or by an over-matching glob, the way a bare ``app.py`` once silently
excused ``.github/oidc_provider/app.py`` too — fails here, so the floor cannot
be lowered without a visible, reviewable change to this file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from coverage.files import GlobMatcher, abs_file, prep_patterns

from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

RETIRED_MARKERS = ("# coverage ratchet: BEGIN", "# coverage ratchet: END")

# Directories coverage never sees as source: tooling output, environments and
# scratch trees. Anything under them is not an authored file of this project.
GENERATED_PARTS = frozenset(
    {".git", ".venv", ".worktrees", "cdk.out", "build", "dist", "htmlcov", "site", "__pycache__"}
)


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _omit_patterns() -> list[str]:
    omit = _pyproject()["tool"]["coverage"]["run"]["omit"]
    assert isinstance(omit, list)
    return [str(item) for item in omit]


def _authored_python_files() -> list[Path]:
    """Every ``.py`` file the floor applies to, repository-relative."""
    return sorted(
        path.relative_to(PROJECT_ROOT)
        for path in PROJECT_ROOT.rglob("*.py")
        if not any(
            part in GENERATED_PARTS
            or part.endswith((".egg-info", "-build"))
            or part == "node_modules"
            for part in path.relative_to(PROJECT_ROOT).parts
        )
    )


def _structurally_excluded(relative: Path) -> bool:
    """Whether ``relative`` is one of the files ``omit`` is allowed to drop."""
    if relative.name == "__init__.py":
        return True
    if "tests" in relative.parts:
        return True
    return any(relative.as_posix() in copies for copies in LAMBDA_SHARED_SOURCE_TARGETS.values())


def test_ratchet_block_has_not_been_reintroduced() -> None:
    """The not-yet-covered list emptied and was deleted; it must not come back."""
    text = PYPROJECT.read_text(encoding="utf-8")
    present = [marker for marker in RETIRED_MARKERS if marker in text]
    assert not present, (
        f"pyproject.toml carries a retired coverage-ratchet marker {present}: every "
        "authored Python file is at 100%, so cover the new file instead of listing it"
    )


def test_omit_excludes_only_structural_files() -> None:
    """No authored source file may be excused from the floor.

    Checked with coverage's own matcher rather than by spelling convention:
    coverage expands a pattern with no directory separator into both the
    absolutised path and a bare glob, so this catches an entry that
    over-matches as well as one that names a source file outright.
    """
    matcher = GlobMatcher(prep_patterns(_omit_patterns()))
    excused = [
        relative.as_posix()
        for relative in _authored_python_files()
        if matcher.match(abs_file(str(PROJECT_ROOT / relative)))
        and not _structurally_excluded(relative)
    ]
    assert not excused, (
        "[tool.coverage.run] omit excludes authored source files from the 100% "
        f"floor: {excused}. Write the tests instead of widening omit."
    )


def test_shared_lambda_copies_are_omitted_and_their_canonical_sources_are_not() -> None:
    """Each shared source counts exactly once: the canonical file, never a copy."""
    matcher = GlobMatcher(prep_patterns(_omit_patterns()))
    for canonical, copies in LAMBDA_SHARED_SOURCE_TARGETS.items():
        assert not matcher.match(abs_file(str(PROJECT_ROOT / canonical))), (
            f"the canonical shared source {canonical} must be measured"
        )
        for copy in copies:
            assert (PROJECT_ROOT / copy).is_file(), f"shared copy {copy} does not exist"
            assert matcher.match(abs_file(str(PROJECT_ROOT / copy))), (
                f"shared copy {copy} is a byte-identical duplicate of {canonical} and must "
                "be omitted so it is not counted twice"
            )


def test_every_omitted_source_file_is_a_shared_copy() -> None:
    """Exact-path entries in ``omit`` may name only the shared Lambda copies."""
    shared_copies = {copy for copies in LAMBDA_SHARED_SOURCE_TARGETS.values() for copy in copies}
    exact_entries = [pattern for pattern in _omit_patterns() if "*" not in pattern]
    strays = [entry for entry in exact_entries if entry not in shared_copies]
    assert not strays, (
        f"[tool.coverage.run] omit names files that are not shared Lambda copies: {strays}"
    )
    assert set(exact_entries) == shared_copies, (
        "every shared Lambda copy must appear in omit by exact path; "
        f"missing: {sorted(shared_copies - set(exact_entries))}"
    )


def test_coverage_floor_and_root_source_are_still_in_force() -> None:
    """The omit policy is only meaningful while the floor and root source remain."""
    data = _pyproject()
    assert data["tool"]["coverage"]["run"]["source"] == ["."], (
        "coverage must measure the repository root; a hand-kept package list "
        "lets new directories escape the floor"
    )
    assert data["tool"]["coverage"]["run"]["branch"] is True
    assert data["tool"]["coverage"]["report"]["fail_under"] == 100
    assert data["tool"]["coverage"]["report"]["include_namespace_packages"] is True, (
        "without include_namespace_packages, Lambda handlers and .github/scripts "
        "are invisible to coverage and 0% reads as 100%"
    )
