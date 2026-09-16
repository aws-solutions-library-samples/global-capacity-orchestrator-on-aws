"""Guardrail that fails if internal planning-document references leak into shipped content.

Planning artifacts (requirements, design notes, task lists) live outside the
repository and are internal to the design process. If production source files,
scripts, tests, the wiki, or shipped documentation refer to them — by filename,
by the directory they live in, by an acceptance-criterion number such as
``Requirement 4.2``, by a ``design §2.3`` section pointer, by a ``task 16.9``
task number, or by the prose that typically introduces them — readers who do
not have those documents get dangling references.

This test walks every shipped top-level directory (``gco_mcp/``, ``cli/``,
``gco/``, ``lambda/``, ``tests/``, ``dockerfiles/``, ``examples/``, ``docs/``,
``scripts/``, ``wiki/``, ``diagrams/``, ``demo/``, ``images/``, ``.github/``) plus
every regular file at the repository root (``README.md``, ``mkdocs.yml``,
``pyproject.toml``, ``cdk.json``, ``app.py``, ``Dockerfile.dev``, …) and fails
loudly with file paths and line numbers if any prohibited pattern appears.

Two kinds of pattern are checked, both case-insensitively:

* plain substrings — ``.kiro/specs``, ``requirements.md``, ``design.md``,
  ``tasks.md``, ``bugfix.md``, ``per the requirements``, ``per the design``,
  ``per the spec``, ``as the spec says``, ``see the requirements doc``,
  ``see the design doc``, ``see the tasks doc``;
* regular expressions for numbered breadcrumbs — ``.kiro spec`` / ``Kiro spec``,
  ``Requirement 4.2`` / ``requirements 2.4``, ``Req 1.11``, ``design §2.3``,
  ``task 16.9`` / ``Task 7.11``, ``Validates: Requirements`` /
  ``Validates: Property``, and bare ``Property 1`` numbering.

Domain vocabulary is deliberately left alone: Kubernetes ``spec`` blocks, the
example-job "spec registry", "Add to Kiro" MCP-client instructions, and AWS
"requirements doc" links match none of the patterns. The test passes silently
when nothing is found.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Directories to walk. Everything under these is scanned.
SCANNED_DIRS = (
    "gco_mcp",
    "cli",
    "gco",
    "lambda",
    "tests",
    "dockerfiles",
    "examples",
    "docs",
    "scripts",
    "wiki",
    "diagrams",
    "demo",
    "images",
    ".github",
)

# Walked-into-but-skipped: cache/build/output trees, IDE-local content, and
# vendored artifacts whose contents we do not own.
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
    "site",
    # Vendored Lambda build artifacts. Pinned third-party copies whose contents
    # we do not own — also excluded from ruff and mypy in pyproject.toml.
    "kubectl-applier-simple-build",
    "helm-installer-build",
}

# Files that quote the prohibited patterns *by design*: this guard (the
# PROHIBITED_SUBSTRINGS / PROHIBITED_PATTERNS literals below), the per-feature
# companion guard that documents the same breadcrumb shapes in its docstring,
# and tests/README.md, which explains what both guards search for.
SELF = Path(__file__).resolve()
COMPANION_GUARD = (PROJECT_ROOT / "tests" / "test_doc_hygiene.py").resolve()
TESTS_README = (PROJECT_ROOT / "tests" / "README.md").resolve()
EXCLUDED_FILES = frozenset({SELF, COMPANION_GUARD, TESTS_README})

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

# Numbered breadcrumbs and workflow pointers that plain substrings cannot
# express. Each entry is ``(label, pattern)``; the label appears in the
# failure report so the offending shape is obvious at a glance.
PROHIBITED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "see .kiro spec", "the Kiro spec for …" — the spec tree by another name.
    ("kiro-spec", re.compile(r"\.kiro\s+specs?\b|\bkiro\s+specs?\b", re.IGNORECASE)),
    # "Requirement 4.2", "requirements 2.4" — acceptance-criterion numbers.
    ("requirement-number", re.compile(r"\brequirements?\s+\d+\.\d+\b", re.IGNORECASE)),
    # "Req 1.11", "Req. 1.15" — the abbreviated form.
    ("requirement-abbrev", re.compile(r"\breq\.?\s+\d+\.\d+\b", re.IGNORECASE)),
    # "design §2.3" — a section pointer into the design document.
    ("design-section", re.compile(r"\bdesign\s+§", re.IGNORECASE)),
    # "task 16.9", "Task 7.11" — implementation-plan task numbers.
    ("task-number", re.compile(r"\btask\s+\d+\.\d+\b", re.IGNORECASE)),
    # "Validates: Requirements 1.3", "Validates: Property 2" — authoring tags.
    (
        "validates-annotation",
        re.compile(r"\bvalidates:\s*(?:requirements?|property)\b", re.IGNORECASE),
    ),
    # "Property 1 — …" — bare property numbering from a design document.
    ("property-number", re.compile(r"\bproperty\s+\d+\b", re.IGNORECASE)),
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
        if resolved in EXCLUDED_FILES:
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

    # Every regular file at the repository root: the README family, mkdocs.yml,
    # pyproject.toml, cdk.json, app.py, Dockerfile.dev, and anything added later.
    for path in PROJECT_ROOT.iterdir():
        _add(path)

    return sorted(files)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of ``(line_no, matched_label, line_text)`` hits."""
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
        for label, pattern in PROHIBITED_PATTERNS:
            if pattern.search(line):
                hits.append((line_no, label, line.rstrip()))
    return hits


def test_scanned_directories_exist() -> None:
    """Every scanned directory must exist, so a rename cannot silently skip a tree.

    The guard once listed the pre-rename ``mcp/`` package and quietly scanned
    nothing under ``gco_mcp/`` for a long time; this pins the inventory to the
    real layout.
    """
    missing = sorted(top for top in SCANNED_DIRS if not (PROJECT_ROOT / top).is_dir())
    assert not missing, f"SCANNED_DIRS lists directories that do not exist: {missing}"


def test_patterns_catch_the_known_breadcrumb_shapes() -> None:
    """The regexes recognise every breadcrumb shape this guard exists to catch."""
    samples = {
        "kiro-spec": "# Constraints honored by this file (see .kiro spec / tests):",
        "requirement-number": "Requirement 4.2 of the wiki contract forbids external hosts",
        "requirement-abbrev": "# Explicit per-flag assertion (Req 1.11, 1.15).",
        "design-section": "# see the warning in design §2.3",
        "task-number": "# reviewed under task 16.9 and migrated",
        "validates-annotation": "Validates: Property 1 (Control-path determinism).",
        "property-number": "* **Property 2 — sampling cannot mutate the control path.**",
    }
    patterns = dict(PROHIBITED_PATTERNS)
    assert set(samples) == set(patterns)
    for label, sample in samples.items():
        assert patterns[label].search(sample), f"{label} does not match {sample!r}"

    legitimate = (
        'Add to Kiro (.kiro/settings/mcp.json): {"mcpServers": {"gco": {...}}}',
        "Keep in sync with the AWS EKS networking requirements doc.",
        "The Job's pod template spec must set a security context.",
        "Property `enabled` toggles the feature.",
        "Run task 3 of the tutorial before task 4.",
    )
    for line in legitimate:
        assert not any(pattern.search(line) for pattern in patterns.values()), line


def test_no_spec_references() -> None:
    """Fail with a structured report if any spec-doc references are present."""
    failures: list[str] = []
    for file_path in _iter_target_files():
        for line_no, label, line_text in _scan_file(file_path):
            rel = file_path.relative_to(PROJECT_ROOT)
            failures.append(f"{rel}:{line_no}: [{label}] {line_text}")
    assert not failures, (
        "Spec / requirements / design / tasks references detected in "
        "shipped code or docs:\n  " + "\n  ".join(failures)
    )
