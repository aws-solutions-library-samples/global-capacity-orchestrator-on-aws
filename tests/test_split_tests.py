"""Tests for ``scripts/split_tests.py``, which shards the core pytest suite.

The property that matters is that sharding neither loses nor duplicates a test.
A bug there would not fail CI loudly — it would quietly stop running part of the
suite while every job still reported green, which is the worst possible failure
mode for a test splitter. ``test_partition_covers_every_file_exactly_once``
pins it for shard counts from 1 to 6.

The rest guard the parts that a future change to the shard count would touch:
that the workflow matrix and the script agree, that ``--of`` is derived from the
matrix rather than hardcoded a second time, and that the excluded modules are
exactly the ones other CI jobs own.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "split_tests.py"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "unit-tests.yml"

#: A stand-in collection: uneven counts, so balancing has something to do.
_SAMPLE_COUNTS = {
    "tests/test_a.py": 500,
    "tests/test_b.py": 250,
    "tests/test_c.py": 120,
    "tests/test_d.py": 80,
    "tests/test_e.py": 40,
    "tests/test_f.py": 7,
    "tests/test_g.py": 3,
    "tests/test_h.py": 1,
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("gco_split_tests", SCRIPT)
    assert spec and spec.loader, f"could not load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def split() -> Any:
    return _load()


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 6])
def test_partition_covers_every_file_exactly_once(split: Any, shards: int) -> None:
    """Sharding must not lose or duplicate a single test file.

    A splitter that drops files still reports green on every shard, so this is
    the one property that has to hold for any shard count.
    """
    groups = split.balance(_SAMPLE_COUNTS, shards)

    assert len(groups) == shards
    flattened = [path for group in groups for path in group]
    assert sorted(flattened) == sorted(_SAMPLE_COUNTS), "a file was lost or duplicated"
    assert len(flattened) == len(set(flattened)), "a file landed in more than one shard"


@pytest.mark.parametrize("shards", [2, 3, 4])
def test_shards_are_reasonably_balanced(split: Any, shards: int) -> None:
    """No shard carries wildly more tests than the lightest one.

    Greedy bin-packing is not optimal, so this asserts a loose bound rather than
    equality: the heaviest shard stays within the lightest plus the single
    largest file, which is the worst case for this algorithm.
    """
    groups = split.balance(_SAMPLE_COUNTS, shards)
    totals = [sum(_SAMPLE_COUNTS[path] for path in group) for group in groups]
    largest_file = max(_SAMPLE_COUNTS.values())

    assert min(totals) > 0, "a shard ended up empty"
    assert max(totals) - min(totals) <= largest_file


def test_partition_is_deterministic(split: Any) -> None:
    """The same input must always produce the same partition.

    Reruns of a commit have to reproduce the split, or a rerun would execute a
    different subset than the run it replaced.
    """
    assert split.balance(_SAMPLE_COUNTS, 3) == split.balance(_SAMPLE_COUNTS, 3)


def test_more_shards_than_files_does_not_crash(split: Any) -> None:
    """Asking for more shards than files yields empty shards, not an error."""
    groups = split.balance({"tests/test_only.py": 5}, 3)
    assert sorted(len(group) for group in groups) == [0, 0, 1]


def test_zero_shards_is_rejected(split: Any) -> None:
    with pytest.raises(SystemExit):
        split.balance(_SAMPLE_COUNTS, 0)


def test_excluded_modules_exist_and_are_not_sharded(split: Any) -> None:
    """Every excluded module is a real file, and none reach the shards."""
    groups = split.balance(_SAMPLE_COUNTS, 2)
    sharded = {path for group in groups for path in group}

    for path, owner in split.DEDICATED_JOB_MODULES.items():
        assert (PROJECT_ROOT / path).is_file(), f"excluded module {path} does not exist"
        assert owner, f"{path} must record which job runs it"
        assert path not in sharded

    assert split.ignore_args() == [
        f"--ignore={path}" for path in sorted(split.DEDICATED_JOB_MODULES)
    ]


def _shard_job(workflow: dict[str, Any]) -> dict[str, Any]:
    job = workflow["jobs"]["unit-pytest-core-shard"]
    assert isinstance(job, dict)
    return job


def test_workflow_shard_matrix_is_a_contiguous_range(workflow: dict[str, Any]) -> None:
    """The matrix must be 1..N so shard numbers line up with ``--shard``.

    ``--shard`` is 1-based, so a matrix of ``[1, 3]`` would ask for shard 3 of 2
    and fail, while ``[0, 1]`` would ask for shard 0. Changing the shard count
    means extending this list, and this keeps that edit honest.
    """
    shards = _shard_job(workflow)["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1)), (
        f"matrix.shard must be a contiguous 1-based range, got {shards}"
    )


def test_workflow_derives_shard_total_from_the_matrix(workflow: dict[str, Any]) -> None:
    """``--of`` must come from ``strategy.job-total``, never a second literal.

    If the total were written out again, bumping the matrix from two shards to
    three would silently keep splitting the suite in two and skip a third of the
    tests while every job still passed.
    """
    steps = _shard_job(workflow)["steps"]
    run_steps = [step.get("run", "") for step in steps if isinstance(step, dict)]
    pytest_step = next((body for body in run_steps if "split_tests.py" in body), "")
    assert pytest_step, "expected a step that invokes scripts/split_tests.py"

    assert '--of "$SHARDS"' in pytest_step, "--of should be passed the SHARDS env var"

    env = next(
        step.get("env", {})
        for step in steps
        if isinstance(step, dict) and "split_tests.py" in step.get("run", "")
    )
    assert env.get("SHARDS") == "${{ strategy.job-total }}", (
        "SHARDS must be derived from strategy.job-total so the shard count lives "
        "only in matrix.shard"
    )
    assert env.get("SHARD") == "${{ matrix.shard }}"

    for literal in re.findall(r"--of\s+(\d+)", pytest_step):
        pytest.fail(f"--of is hardcoded to {literal}; derive it from strategy.job-total")


def test_shards_explicitly_disable_the_coverage_floor(workflow: dict[str, Any]) -> None:
    """Each shard must pass ``--cov-fail-under=0``.

    This is load-bearing rather than redundant. pytest-cov reads ``fail_under``
    from ``[tool.coverage.report]`` and enforces it even when ``--cov-report=``
    suppresses every report, so a shard without this flag fails with "Required
    test coverage of 90.0% not reached" no matter how healthy the codebase is —
    a shard only exercises its own slice. The real floor is applied to the
    combined data by ``unit:pytest:core``.
    """
    steps = _shard_job(workflow)["steps"]
    bodies = "\n".join(
        line
        for step in steps
        if isinstance(step, dict)
        for line in step.get("run", "").splitlines()
        if not line.strip().startswith("#")
    )
    assert "--cov-fail-under=0" in bodies, (
        "shards must disable the inherited fail_under; see the docstring for why"
    )
    other_floors = [
        value for value in re.findall(r"--cov-fail-under=(\d+)", bodies) if value != "0"
    ]
    assert not other_floors, f"a shard enforces a coverage floor of {other_floors}"


def test_combining_job_enforces_the_floor_and_needs_every_shard(
    workflow: dict[str, Any],
) -> None:
    """`unit:pytest:core` combines the shards and is gated on all of them."""
    job = workflow["jobs"]["unit-pytest-core"]
    assert job["name"] == "unit:pytest:core", (
        "the combining job keeps this display name so existing required-status-check "
        "rules continue to match"
    )
    assert job["needs"] == "unit-pytest-core-shard"

    bodies = "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))
    assert "coverage combine" in bodies
    assert "coverage report" in bodies


def _all_run_commands(workflow: dict[str, Any]) -> str:
    """Every ``run:`` body in the workflow, with comment lines removed.

    Comments are stripped so prose explaining why a flag is absent does not read
    as the flag being present.
    """
    bodies: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            body = step.get("run", "")
            bodies.extend(line for line in body.splitlines() if not line.strip().startswith("#"))
    return "\n".join(bodies)


def test_the_floor_value_lives_only_in_pyproject(workflow: dict[str, Any]) -> None:
    """The 90 is written once, in pyproject, and never restated in the workflow.

    Shards may pass ``--cov-fail-under=0`` to switch the inherited check off, but
    no job may name a different threshold: that would be a second source of truth
    which could drift away from ``[tool.coverage.report]``.
    """
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "fail_under = 90" in pyproject

    # Covers both spellings: pytest-cov's --cov-fail-under and coverage's own
    # --fail-under, either of which could restate the threshold.
    thresholds = re.findall(r"--(?:cov-)?fail-under=(\d+)", _all_run_commands(workflow))
    assert set(thresholds) <= {"0"}, (
        f"workflow names coverage thresholds {sorted(set(thresholds))}; the floor "
        "belongs in [tool.coverage.report] alone. Only an explicit 0 is allowed, to "
        "switch the inherited check off where it does not apply."
    )
