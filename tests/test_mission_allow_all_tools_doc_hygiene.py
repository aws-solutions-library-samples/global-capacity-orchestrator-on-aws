"""Doc-hygiene guard for the allow-all-tools feature files.

The allow-all-tools resolver, its MCP-tool and CLI call sites, and its
test modules are shipped, standalone code. They should read as such:
their comments and docstrings explain behaviour in plain terms, not by
pointing at an internal planning artifact the repository does not
contain. References like ``R4.1``, ``Validates: Requirements 1.3``,
``Property 7``, or "the design's resolver table" are scaffolding from the
authoring process — useful while building, noise (and dangling pointers)
once merged.

This test scans the feature's touched source files plus its own test
modules and fails if any such reference survives, listing every
offending line so the fix is mechanical. It deliberately excludes itself
from the scan so the pattern catalogue below does not trip the very
check it defines.

The three touched source files are pre-existing, shared modules: the
guard scans them so this feature's additions stay breadcrumb-free, and
they carry no such references today. Documentation files (docs/MISSION.md)
are intentionally out of scope — operator-facing prose may legitimately
describe the feature and reference requirements.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()

# The feature's files: the three touched source modules and the feature
# test modules. Documentation files (docs/MISSION.md) and example
# artifacts are intentionally out of scope — those legitimately describe
# the feature and may reference requirements in operator-facing prose.
_FEATURE_PATHS: tuple[Path, ...] = (
    _REPO_ROOT / "mcp" / "mission" / "validation.py",
    _REPO_ROOT / "mcp" / "tools" / "mission.py",
    _REPO_ROOT / "cli" / "commands" / "mission_cmd.py",
    _REPO_ROOT / "tests",
)

# Only this feature's test modules under ``tests/`` are in scope; the
# rest of the suite is unrelated.
_TEST_PREFIX = "test_mission_allow_all_tools_"


def _iter_feature_files() -> list[Path]:
    """Collect the Python files this guard scans, excluding itself."""
    tests_dir = _REPO_ROOT / "tests"
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
            # Under ``tests/`` only this feature's own modules are in
            # scope, at any depth — unrelated shared helpers such as
            # ``tests/strategies/`` belong to other features and are
            # left out so this guard never trips on their content.
            if base == tests_dir and not path.name.startswith(_TEST_PREFIX):
                continue
            files.append(path)
    return files


# Each pattern names a class of authoring-scaffolding reference that must
# not survive into shipped code. The label is shown in the failure report
# so a contributor knows what to rewrite.
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Requirement IDs like R4.1, R10.5.
    ("requirement-id", re.compile(r"\bR\d+\.\d+\b")),
    # The "Validates: Requirements ..." docstring annotation.
    ("validates-annotation", re.compile(r"Validates:\s*Requirement", re.IGNORECASE)),
    # The "# Feature: mission-allow-all-tools, Property N" tag.
    ("feature-property-tag", re.compile(r"Feature:\s*mission-allow-all-tools")),
    # Bare "Property N" property-numbering references.
    ("property-number", re.compile(r"\bProperty\s+\d+\b")),
    # "Requirement 4" / "Requirements 1.3" prose references.
    ("requirement-word", re.compile(r"\bRequirements?\s+\d", re.IGNORECASE)),
    # Numbered task references pointing at the implementation plan.
    ("task-number", re.compile(r"\btask\s+\d+(?:\.\d+)?\b", re.IGNORECASE)),
    # Pointers at planning documents the repository does not ship.
    ("planning-doc", re.compile(r"\b(?:requirements|design|tasks)\.md\b", re.IGNORECASE)),
    # "the design" / "design's" / "the spec" prose pointers.
    ("spec-prose", re.compile(r"\bthe design\b|\bdesign's\b|\bthe spec\b", re.IGNORECASE)),
)


def test_feature_files_have_no_spec_internal_references() -> None:
    """No allow-all-tools file may reference the authoring spec artifacts.

    Scans every touched source and feature test file line by line for the
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
