"""Checked-in Lambda shared-source copies stay identical to their canonical files.

``StackManager._sync_lambda_sources`` copies the canonical shared sources
(``lambda/proxy-shared/proxy_utils.py``, ``lambda/tls-shared/backend_tls.py``)
over their per-function copies before every synth. When the copies drift from
canonical — as happened when ``diagrams/code_diagrams/generate.py`` refreshed
the canonical pyflowchart headers without touching the copies — every deploy
rewrites tracked files, dirtying the worktree mid-run and failing the next
run's clean-worktree preflight (observed live: the release-validation harness
left five modified files behind on an otherwise green run).

This module enforces the sync at commit time instead of deploy time. The map
of canonical sources to copies is ``cli.stacks.LAMBDA_SHARED_SOURCE_TARGETS``
— the same one the deploy path consumes — so the test cannot drift from the
code. On failure, re-run the sync (any of):

    gco stacks deploy ... (runs it implicitly), or
    python -c "from pathlib import Path; from shutil import copy2;
               from cli.stacks import LAMBDA_SHARED_SOURCE_TARGETS as M;
               [copy2(s, t) for s, ts in M.items() for t in ts]"

and commit the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.stacks import LAMBDA_SHARED_SOURCE_TARGETS

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_PAIRS: list[tuple[str, str]] = [
    (source, target)
    for source, targets in LAMBDA_SHARED_SOURCE_TARGETS.items()
    for target in targets
]


class TestSharedSourceMap:
    """The map itself stays sane before any byte comparison runs."""

    def test_map_is_not_empty(self):
        assert LAMBDA_SHARED_SOURCE_TARGETS, "shared-source map unexpectedly empty"

    def test_every_canonical_source_exists(self):
        missing = [s for s in LAMBDA_SHARED_SOURCE_TARGETS if not (PROJECT_ROOT / s).is_file()]
        assert not missing, f"canonical shared sources missing from the checkout: {missing}"

    def test_every_target_exists(self):
        missing = [t for _, t in _PAIRS if not (PROJECT_ROOT / t).is_file()]
        assert not missing, f"checked-in copies missing from the checkout: {missing}"

    def test_no_target_is_also_a_source_of_itself(self):
        overlap = [t for s, t in _PAIRS if s == t]
        assert not overlap, f"map copies a file onto itself: {overlap}"


class TestSharedSourceIdentity:
    """Each checked-in copy is byte-identical to its canonical source."""

    @pytest.mark.parametrize(("source", "target"), _PAIRS, ids=[t for _, t in _PAIRS])
    def test_copy_matches_canonical(self, source: str, target: str):
        source_bytes = (PROJECT_ROOT / source).read_bytes()
        target_bytes = (PROJECT_ROOT / target).read_bytes()
        assert source_bytes == target_bytes, (
            f"{target} has drifted from its canonical source {source}. "
            f"Deploy would silently rewrite it and dirty the worktree; "
            f"copy {source} over {target} and commit (see module docstring)."
        )
