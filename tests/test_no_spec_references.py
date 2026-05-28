"""Guardrail that fails if internal planning-document references leak into production code or human-facing docs.

The spec workflow keeps planning artifacts under ``.kiro/specs/`` — those are
internal to the design process and are not part of the user-facing project. If
production source files, examples, scripts, or shipped documentation refer to
them by name (or to the prose patterns that typically introduce them), readers
who do not have access to ``.kiro/`` get dangling references.

This test walks the production tree, the test tree, the examples and docs
trees, and the project-root README family, and fails loudly with file paths and
line numbers if any of the prohibited substrings appear.

Prohibited substrings (matched case-insensitively as plain substrings):

* ``.kiro/specs``
* ``requirements.md``
* ``design.md``
* ``tasks.md``
* ``bugfix.md``
* ``per the requirements``
* ``per the design``
* ``per the spec``
* ``as the spec says``
* ``see the requirements doc``
* ``see the design doc``
* ``see the tasks doc``

The test passes silently when none are present.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Directories to walk. Everything under these is scanned.
SCANNED_DIRS = (
    "mcp",
    "cli",
    "gco",
    "lambda",
    "tests",
    "dockerfiles",
    "examples",
    "docs",
    "scripts",
)

# Standalone files outside the SCANNED_DIRS that should also be checked. The
# ``mcp/README.md`` and ``examples/README.md`` entries overlap with the
# directory walks above; ``_iter_target_files`` deduplicates by resolved path.
EXTRA_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "QUICKSTART.md",
    "mcp/README.md",
    "examples/README.md",
)

# Walked-into-but-skipped: cache/build/output trees and the spec tree itself
# (the spec docs are the documents being kept private).
EXCLUDED_DIR_NAMES = {
    ".kiro",
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    "cdk.out",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "htmlcov",
    # Vendored Lambda build artifacts. Pinned third-party copies whose contents
    # we do not own — also excluded from ruff and mypy in pyproject.toml.
    "kubectl-applier-simple-build",
    "helm-installer-build",
}

# This file itself contains every prohibited substring as a string literal in
# the PROHIBITED_SUBSTRINGS tuple below; without this exclusion it would always
# fail.
SELF = Path(__file__).resolve()

# tests/README.md documents what these guardrails search for, so by design it
# contains every prohibited substring as part of the explanation. Skip it for
# the same reason we skip ``SELF``.
TESTS_README = (PROJECT_ROOT / "tests" / "README.md").resolve()

# Prohibited substrings, lowercase. Matched against the lowercased line.
PROHIBITED_SUBSTRINGS: tuple[str, ...] = (
    ".kiro/specs",
    "requirements.md",
    "design.md",
    "tasks.md",
    "bugfix.md",
    "per the requirements",
    "per the design",
    "per the spec",
    "as the spec says",
    "see the requirements doc",
    "see the design doc",
    "see the tasks doc",
)


def _iter_target_files() -> list[Path]:
    """Return the deduplicated, sorted list of files to scan."""
    seen: set[Path] = set()
    files: list[Path] = []

    def _add(candidate: Path) -> None:
        if not candidate.is_file():
            return
        if any(part in EXCLUDED_DIR_NAMES for part in candidate.parts):
            return
        resolved = candidate.resolve()
        if resolved == SELF:
            return
        if resolved == TESTS_README:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        files.append(candidate)

    for top in SCANNED_DIRS:
        root = PROJECT_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            _add(path)

    for name in EXTRA_FILES:
        _add(PROJECT_ROOT / name)

    return sorted(files)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of ``(line_no, matched_substring, line_text)`` hits."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return []
    hits: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        haystack = line.lower()
        for needle in PROHIBITED_SUBSTRINGS:
            if needle in haystack:
                hits.append((line_no, needle, line.rstrip()))
    return hits


def test_no_spec_references() -> None:
    """Fail with a structured report if any spec-doc references are present."""
    failures: list[str] = []
    for file_path in _iter_target_files():
        for line_no, needle, line_text in _scan_file(file_path):
            rel = file_path.relative_to(PROJECT_ROOT)
            failures.append(f"{rel}:{line_no}: [{needle}] {line_text}")
    assert not failures, (
        "Spec / requirements / design / tasks references detected in "
        "production code or docs:\n  " + "\n  ".join(failures)
    )
