"""Doc-hygiene guard: shipped feature files must carry no spec breadcrumbs.

Each spec-driven feature ships standalone code — a package, its tool/CLI
call sites, and its test modules. Those files should read as production
code: comments and docstrings explain behaviour in plain terms, not by
pointing at an internal planning artifact the repository does not ship.
References like ``R12.6``, ``Validates: Requirements 1.3``, ``Property 7``,
``task 6.9``, or "the design's determinism boundary" are scaffolding from
the authoring process — useful while building, noise (and dangling
pointers) once merged.

This guard scans each registered feature's own files and fails if any such
reference survives, listing every offending line so the fix is mechanical.
Each feature is one row in :data:`_FEATURES`; adding a new feature means
adding one entry, not a new copy of this file.

Why this is one narrow-scoped guard rather than a repo-wide scan: the
patterns below are deliberately aggressive (bare ``Property N``, "the
design", ``task N``) and would false-positive across the wider tree, where
those phrases appear legitimately — property-test infrastructure, the
task-execution code, architecture docs, and so on. Restricting each pattern
set to one feature's explicit file list is what keeps the aggressive
matching safe. The companion ``test_no_spec_references.py`` handles the
*safe* substrings (planning-doc filenames, the spec directory) repo-wide.

Documentation files (READMEs, ``docs/MISSION.md``) and example artifacts are
intentionally out of scope for every feature — operator-facing prose may
legitimately describe a feature and reference its requirements. A feature's
own functional flag names and ``[gated by ...]`` docstring prefixes are
behaviour, not breadcrumbs: none of the patterns match them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()
_TESTS_DIR = _REPO_ROOT / "tests"


@dataclass(frozen=True)
class _Feature:
    """One spec-driven feature's doc-hygiene scope.

    ``feature_id`` is the spec/feature slug (used both as the parametrize id
    and as the value the per-feature ``Feature: <slug>`` tag must not carry).
    ``paths`` lists the shipped source files and directories to scan; a
    directory is walked recursively for ``*.py``. ``test_prefix`` restricts
    the scan under ``tests/`` to this feature's own modules so the guard never
    trips on an unrelated feature's test content.

    ``allow_labels`` names shared pattern labels that this feature's own
    domain vocabulary legitimately uses, so the guard does not flag them. The
    ``spec-prose`` heuristic ("the spec", "the design"), for instance, is
    behaviour rather than a breadcrumb for a feature whose core data model is
    an endpoint ``spec`` dict — code and comments necessarily say "the spec"
    to mean that structure. The hard breadcrumb patterns (requirement IDs,
    ``Validates: Requirements``, property/task numbering, planning-doc
    filenames) are never allowable and cannot be listed here.
    """

    feature_id: str
    paths: tuple[Path, ...]
    test_prefix: str
    allow_labels: frozenset[str] = frozenset()


# Every spec-driven feature whose shipped files must stay breadcrumb-free.
# Add a feature by appending one entry — no new test file required.
_FEATURES: tuple[_Feature, ...] = (
    _Feature(
        feature_id="mission-metric-reader-tools",
        paths=(
            _REPO_ROOT / "gco_mcp" / "metric_readers",
            _REPO_ROOT / "gco_mcp" / "tools" / "metrics.py",
            _TESTS_DIR,
        ),
        test_prefix="test_metric_readers_",
    ),
    _Feature(
        feature_id="mission-allow-all-tools",
        paths=(
            _REPO_ROOT / "gco_mcp" / "mission" / "validation.py",
            _REPO_ROOT / "gco_mcp" / "tools" / "mission.py",
            _REPO_ROOT / "cli" / "commands" / "mission_cmd.py",
            _TESTS_DIR,
        ),
        test_prefix="test_mission_allow_all_tools_",
    ),
    _Feature(
        feature_id="mission-semantic-progress-judge",
        paths=(
            _REPO_ROOT / "gco_mcp" / "mission_judge",
            _REPO_ROOT / "gco_mcp" / "tools" / "semantic_progress.py",
            _TESTS_DIR,
        ),
        test_prefix="test_semantic_progress_",
    ),
    _Feature(
        feature_id="mooncake-distributed-inference",
        paths=(
            _REPO_ROOT / "cli" / "inference.py",
            _REPO_ROOT / "cli" / "models.py",
            _REPO_ROOT / "cli" / "images.py",
            _REPO_ROOT / "gco" / "services" / "inference_monitor.py",
            _REPO_ROOT / "gco" / "services" / "mooncake_pd_proxy.py",
            _REPO_ROOT / "gco" / "services" / "inference_store.py",
            _REPO_ROOT / "gco" / "stacks" / "regional_stack.py",
            _REPO_ROOT / "gco_mcp" / "tools" / "inference.py",
            _REPO_ROOT / "gco_mcp" / "tools" / "storage.py",
            _TESTS_DIR,
        ),
        test_prefix="test_mooncake_",
        # This feature's data model is an endpoint ``spec`` dict, so code and
        # comments say "the spec" to name that structure — behaviour, not a
        # breadcrumb. The hard breadcrumb patterns stay enforced.
        allow_labels=frozenset({"spec-prose"}),
    ),
)


# Patterns shared by every feature: each names a class of authoring
# scaffolding that must not survive into shipped code. The label is shown in
# the failure report so a contributor knows what to rewrite.
_SHARED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Requirement IDs like R12.6, R3.1.
    ("requirement-id", re.compile(r"\bR\d+\.\d+\b")),
    # The "Validates: Requirements ..." docstring annotation.
    ("validates-annotation", re.compile(r"Validates:\s*Requirement", re.IGNORECASE)),
    # Bare "Property N" property-numbering references.
    ("property-number", re.compile(r"\bProperty\s+\d+\b")),
    # "Requirement 12" / "Requirements 1.3" prose references.
    ("requirement-word", re.compile(r"\bRequirements?\s+\d", re.IGNORECASE)),
    # Numbered task references pointing at the implementation plan.
    ("task-number", re.compile(r"\btask\s+\d+(?:\.\d+)?\b", re.IGNORECASE)),
    # Pointers at planning documents the repository does not ship.
    ("planning-doc", re.compile(r"\b(?:requirements|design|tasks)\.md\b", re.IGNORECASE)),
    # "the design" / "design's" / "the spec" prose pointers.
    ("spec-prose", re.compile(r"\bthe design\b|\bdesign's\b|\bthe spec\b", re.IGNORECASE)),
)


# Labels a feature may legitimately allow via ``_Feature.allow_labels`` because
# the matched phrase can be domain vocabulary rather than a planning breadcrumb.
# Hard breadcrumb labels (requirement IDs, ``Validates: Requirements``,
# property/task numbering, planning-doc filenames, the per-feature tag) are
# absent here and can never be suppressed.
_ALLOWABLE_LABELS: frozenset[str] = frozenset({"spec-prose"})


def _patterns_for(feature: _Feature) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Return the shared patterns plus this feature's own ``Feature:`` tag.

    The feature tag is per-feature so the ``# Feature: <slug>, Property N``
    authoring tag is caught for the feature that owns it, without one feature's
    guard flagging another's slug. Labels listed in ``feature.allow_labels``
    (restricted to :data:`_ALLOWABLE_LABELS`) are dropped, so a feature whose
    domain vocabulary overlaps a soft heuristic is not flagged for using it.
    """
    bad_allows = feature.allow_labels - _ALLOWABLE_LABELS
    assert not bad_allows, (
        f"Feature '{feature.feature_id}' may not allow hard breadcrumb labels: {sorted(bad_allows)}"
    )
    feature_tag = (
        "feature-property-tag",
        re.compile(r"Feature:\s*" + re.escape(feature.feature_id)),
    )
    shared = tuple(
        (label, pattern) for label, pattern in _SHARED_PATTERNS if label not in feature.allow_labels
    )
    return (*shared, feature_tag)


def _iter_feature_files(feature: _Feature) -> list[Path]:
    """Collect the Python files this guard scans for ``feature``, excluding itself."""
    files: list[Path] = []
    for base in feature.paths:
        if base.is_file():
            files.append(base)
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.resolve() == _THIS_FILE:
                continue
            # Under ``tests/`` only this feature's own modules are in scope, at
            # any depth — unrelated shared helpers belong to other features and
            # are left out so this guard never trips on their content.
            if base == _TESTS_DIR and not path.name.startswith(feature.test_prefix):
                continue
            files.append(path)
    return files


@pytest.mark.parametrize("feature", _FEATURES, ids=[f.feature_id for f in _FEATURES])
def test_feature_files_have_no_spec_internal_references(feature: _Feature) -> None:
    """No shipped file of ``feature`` may reference the authoring spec artifacts.

    Scans every feature source and test file line by line for the scaffolding
    patterns and fails with a complete, grouped list of offences so they can be
    rewritten in one pass.
    """
    patterns = _patterns_for(feature)
    violations: list[str] = []

    for path in _iter_feature_files(feature):
        rel = path.relative_to(_REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in patterns:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno} [{label}] {line.strip()}")

    assert not violations, (
        f"Spec-internal references must not appear in shipped files for "
        f"'{feature.feature_id}'. Found {len(violations)}:\n" + "\n".join(violations)
    )
