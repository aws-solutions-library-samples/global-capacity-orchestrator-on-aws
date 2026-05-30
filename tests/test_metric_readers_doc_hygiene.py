"""Doc-hygiene guard for the metric-reader feature files.

The metric-reader package, its tool wrapper, and its test modules are
shipped, standalone code. They should read as such: their comments and
docstrings explain behaviour in plain terms, not by pointing at an
internal planning artifact the repository does not contain. References
like ``R6.5``, ``Validates: Requirements 1.3``, ``Property 7``, or "the
design's per-format table" are scaffolding from the authoring process —
useful while building, noise (and dangling pointers) once merged.

This test scans the feature's own files and fails if any such reference
survives, listing every offending line so the fix is mechanical. It
deliberately excludes itself from the scan so the pattern catalogue
below does not trip the very check it defines.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()

# The feature's files: the pure package, the tool wrapper, and the test
# modules. Documentation files (READMEs, docs/MISSION.md) and example
# artifacts are intentionally out of scope — those legitimately describe
# the feature and may reference requirements in operator-facing prose.
_FEATURE_PATHS: tuple[Path, ...] = (
    _REPO_ROOT / "mcp" / "metric_readers",
    _REPO_ROOT / "mcp" / "tools" / "metrics.py",
    _REPO_ROOT / "tests",
)

# Only the metric-reader test modules under ``tests/`` are in scope; the
# rest of the suite is unrelated.
_TEST_PREFIX = "test_metric_readers_"


def _iter_feature_files() -> list[Path]:
    """Collect the Python files this guard scans, excluding itself."""
    files: list[Path] = []
    for base in _FEATURE_PATHS:
        if base.is_file():
            files.append(base)
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.resolve() == _THIS_FILE:
                continue
            # Under ``tests/`` only the metric-reader modules are in scope.
            if path.parent.name == "tests" and not path.name.startswith(_TEST_PREFIX):
                continue
            files.append(path)
    return files


# Each pattern names a class of authoring-scaffolding reference that must
# not survive into shipped code. The label is shown in the failure report
# so a contributor knows what to rewrite.
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Requirement IDs like R6.5, R18.10.
    ("requirement-id", re.compile(r"\bR\d+\.\d+\b")),
    # The "Validates: Requirements ..." docstring annotation.
    ("validates-annotation", re.compile(r"Validates:\s*Requirement", re.IGNORECASE)),
    # The "# Feature: mission-metric-reader-tools, Property N" tag.
    ("feature-property-tag", re.compile(r"Feature:\s*mission-metric-reader-tools")),
    # Bare "Property N" property-numbering references.
    ("property-number", re.compile(r"\bProperty\s+\d+\b")),
    # "Requirement 18" / "Requirements 1.3" prose references.
    ("requirement-word", re.compile(r"\bRequirements?\s+\d", re.IGNORECASE)),
    # Numbered task references pointing at the implementation plan.
    ("task-number", re.compile(r"\btask\s+\d+(?:\.\d+)?\b", re.IGNORECASE)),
    # Pointers at planning documents the repository does not ship.
    ("planning-doc", re.compile(r"\b(?:requirements|design|tasks)\.md\b", re.IGNORECASE)),
    # "the design" / "design's" / "the spec" prose pointers.
    ("spec-prose", re.compile(r"\bthe design\b|\bdesign's\b|\bthe spec\b", re.IGNORECASE)),
)


def test_feature_files_have_no_spec_internal_references() -> None:
    """No metric-reader file may reference the authoring spec artifacts.

    Scans every feature source and test file line by line for the
    scaffolding patterns above and fails with a complete, grouped list of
    offences so they can be rewritten in one pass.
    """
    violations: list[str] = []

    for path in _iter_feature_files():
        rel = path.relative_to(_REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in _FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno} [{label}] {line.strip()}")

    assert not violations, (
        "Spec-internal references must not appear in shipped feature files. "
        f"Found {len(violations)}:\n" + "\n".join(violations)
    )
