"""Guardrail test that fails if Python 3.15 deprecation surface appears in the codebase.

The Python 3.15 release schedule soft-deprecates a number of stdlib symbols and
syntactic forms. This test walks the production tree, plus the project README, and
fails loudly with file paths and line numbers if any of those forms are reintroduced.

The companion version-drift test treats this filename as part of the contract: it
must target the Python minor immediately after the floor in ``requires-python``.
Raising the floor therefore fails CI until this module, its patterns, and its
documentation advance to the following Python release.

Patterns checked:

* ``collections.abc.ByteString`` — removed alias.
* ``typing.ByteString`` — removed alias.
* ``typing.no_type_check_decorator`` — removed helper.
* ``import cProfile`` / ``from cProfile`` — direct cProfile usage.
* ``glob.glob0`` / ``glob.glob1`` — internal glob helpers exposed by accident.
* ``platform.java_ver`` — Jython-only helper.
* ``load_module`` / ``find_module`` / ``zipimporter`` — legacy importlib API.
* ``NamedTuple("name", field=type)`` keyword-argument constructor.
* ``TypedDict("name")`` zero-field constructor.
* Bare ``re.match(`` calls outside the two intentional carve-outs.

The test passes silently when none are present.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Directories to walk. Everything under these is scanned. Files outside them are
# left alone (so the spec/design tree, hidden caches, and the .git folder do not
# trip the test).
SCANNED_DIRS = (
    "gco_mcp",
    "cli",
    "gco",
    "lambda",
    "tests",
    "dockerfiles",
)

# Standalone files outside the SCANNED_DIRS that should also be checked.
EXTRA_FILES = ("README.md",)

# Directories that are walked-into-but-skipped: cache/build/output trees that
# can contain generated artifacts mirroring source files we have already covered.
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
    # Vendored Lambda build artifacts (boto3 + six). Pinned third-party copies
    # whose contents we do not own — also excluded from ruff and mypy in
    # pyproject.toml.
    "kubectl-applier-simple-build",
    "helm-installer-build",
}

# This file itself contains the patterns as regex literals; it must not flag itself.
SELF = Path(__file__).resolve()

# tests/README.md documents the patterns these guardrails search for, which
# means it necessarily contains every literal we look for. Skipping it here
# is the same posture we take for ``SELF``: don't flag a doc whose entire
# purpose is to list the prohibited surface.
TESTS_README = (PROJECT_ROOT / "tests" / "README.md").resolve()

# tests/test_integration.py historically used ``re.match`` and was reviewed under
# task 16.9 — its single call site was migrated to ``re.search``. The carve-out
# remains so future patches that re-introduce ``re.match`` there get one warning
# from this guardrail before being committed.
RE_MATCH_CARVE_OUTS = {
    PROJECT_ROOT / "tests" / "test_integration.py",
    PROJECT_ROOT / "cli" / "kubectl_helpers.py",
}

# (regex, human-readable label, optional callable that returns True when a match
# should be ignored — takes the absolute Path of the offending file).
_NEVER_IGNORE = lambda _p: False  # noqa: E731

PATTERNS: tuple[tuple[re.Pattern[str], str, object], ...] = (
    (re.compile(r"\bcollections\.abc\.ByteString\b"), "collections.abc.ByteString", _NEVER_IGNORE),
    (re.compile(r"\btyping\.ByteString\b"), "typing.ByteString", _NEVER_IGNORE),
    (
        re.compile(r"\btyping\.no_type_check_decorator\b"),
        "typing.no_type_check_decorator",
        _NEVER_IGNORE,
    ),
    (
        re.compile(r"^\s*(?:import\s+cProfile|from\s+cProfile\b)"),
        "cProfile import",
        _NEVER_IGNORE,
    ),
    (re.compile(r"\bglob\.glob0\b"), "glob.glob0", _NEVER_IGNORE),
    (re.compile(r"\bglob\.glob1\b"), "glob.glob1", _NEVER_IGNORE),
    (re.compile(r"\bplatform\.java_ver\b"), "platform.java_ver", _NEVER_IGNORE),
    (re.compile(r"\bload_module\b"), "load_module", _NEVER_IGNORE),
    (re.compile(r"\bfind_module\b"), "find_module", _NEVER_IGNORE),
    (re.compile(r"\bzipimporter\b"), "zipimporter", _NEVER_IGNORE),
    # NamedTuple keyword-argument constructor: NamedTuple("Name", field=type)
    (
        re.compile(r"""\bNamedTuple\s*\(\s*['"][^'"]+['"]\s*,\s*\*{0,2}\w+\s*="""),
        'NamedTuple("Name", field=type) keyword-arg syntax',
        _NEVER_IGNORE,
    ),
    # TypedDict zero-field constructor: TypedDict("Name") with no extra args
    (
        re.compile(r"""\bTypedDict\s*\(\s*['"][^'"]+['"]\s*\)"""),
        'TypedDict("Name") zero-field syntax',
        _NEVER_IGNORE,
    ),
    # Bare re.match( — soft-deprecated in 3.15 in favour of re.search / re.fullmatch.
    # Compiled-pattern .match() (e.g. _CLUSTER_NAME_RE.match) is unaffected.
    (
        re.compile(r"\bre\.match\s*\("),
        "bare re.match( call",
        lambda p: p in RE_MATCH_CARVE_OUTS,
    ),
)


def _iter_target_files() -> list[Path]:
    """Return the list of files to scan, sorted for stable failure messages."""
    files: list[Path] = []
    for top in SCANNED_DIRS:
        root = PROJECT_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
                continue
            if path.resolve() == SELF:
                continue
            if path.resolve() == TESTS_README:
                continue
            files.append(path)
    for name in EXTRA_FILES:
        extra = PROJECT_ROOT / name
        if extra.is_file():
            files.append(extra)
    return sorted(files)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of (line_no, label, line) tuples for hits in ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return []
    hits: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, label, ignore_fn in PATTERNS:
            if pattern.search(line) and not ignore_fn(path):  # type: ignore[operator]
                hits.append((line_no, label, line.rstrip()))
    return hits


def test_deprecation_surface_guard_tracks_next_python_minor() -> None:
    """Require this guard to advance whenever the supported Python floor advances."""
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires_python = metadata.get("project", {}).get("requires-python")
    assert isinstance(requires_python, str), "[project].requires-python must be a string"

    minimum = re.match(r"^\s*>=\s*(\d+)\.(\d+)(?:\.\d+)?", requires_python)
    assert minimum is not None, (
        "The deprecation guard could not identify the Python floor from "
        f"requires-python={requires_python!r}; keep an explicit leading >=<major>.<minor> floor"
    )

    major, minor = (int(part) for part in minimum.groups())
    target_major, target_minor = major, minor + 1
    expected_filename = f"test_no_python_{target_major}{target_minor:02d}_deprecation_surface.py"
    assert SELF.name == expected_filename, (
        f"{SELF.name} is stale for requires-python={requires_python!r}. The deprecation "
        f"guard must target Python {target_major}.{target_minor}: rename this module to "
        f"{expected_filename} and replace its scan patterns and documentation with "
        f"Python {target_major}.{target_minor} deprecations."
    )


def test_no_python_315_deprecation_surface() -> None:
    """Fail with a structured report if any 3.15 deprecation surface is present."""
    failures: list[str] = []
    for file_path in _iter_target_files():
        for line_no, label, line_text in _scan_file(file_path):
            rel = file_path.relative_to(PROJECT_ROOT)
            failures.append(f"{rel}:{line_no}: [{label}] {line_text}")
    assert not failures, "Python 3.15 deprecation surface detected:\n  " + "\n  ".join(failures)
