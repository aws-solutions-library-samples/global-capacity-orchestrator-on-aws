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
import json
import re
import subprocess
import sys
import types
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


def test_workflow_uses_four_nonempty_shards(workflow: dict[str, Any]) -> None:
    """The matrix has exactly four dynamic cells.

    Three shards ran about eight minutes each once the suite passed 5,400
    tests; four keeps every cell under the runner's twenty-minute budget with
    headroom, at the cost of one more setup. Changing this number is the
    whole change — ``--of`` and the artifact glob follow the matrix.
    """
    job = _shard_job(workflow)
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]


def test_shard_checkout_contains_diagram_source_history(workflow: dict[str, Any]) -> None:
    """Provenance checks need the recorded source commit, not a depth-1 clone."""
    checkout = next(
        step
        for step in _shard_job(workflow)["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_shard_one_owns_whole_repository_policy_checks(
    split: Any, workflow: dict[str, Any]
) -> None:
    """The accelerator policy runs once, independently of pytest sharding.

    Its two suites are carved out of the shards (``DEDICATED_JOB_MODULES``), so
    this step is the only place they execute and the only thing that covers
    ``scripts/accelerator_catalog.py``. It must therefore record coverage, and
    the sharded run on the same runner must ``--cov-append`` rather than erase
    that data at start-up — otherwise the combined floor sees the module at a
    fraction of its real coverage and fails.
    """
    steps = _shard_job(workflow)["steps"]
    step = next(
        item for item in steps if item.get("name") == "Validate accelerator catalog and NodePools"
    )
    assert step["if"] == "matrix.shard == 1"
    policy_lines = [
        line.strip()
        for line in step["run"].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert policy_lines[0] == "python scripts/accelerator_catalog.py validate"
    policy_pytest = " ".join(policy_lines[1:])
    for module in split.DEDICATED_JOB_MODULES:
        if "offline policy step" in split.DEDICATED_JOB_MODULES[module]:
            assert module in policy_pytest, f"{module} is carved out of the shards but not run here"
    for flag in ("--cov", "--cov-report=", "--cov-fail-under=0"):
        assert flag in policy_pytest.split(), f"the policy step must record coverage ({flag})"

    sharded = next(body for body in (s.get("run", "") for s in steps) if "split_tests.py" in body)
    assert "--cov-append" in sharded.split(), (
        "the sharded run must append to the policy step's coverage data, not erase it"
    )


def test_shard_artifacts_are_dynamic_and_aggregate_uses_a_glob(
    workflow: dict[str, Any],
) -> None:
    """Adding a matrix cell must require no artifact-name edits."""
    shard_steps = _shard_job(workflow)["steps"]
    upload = next(item for item in shard_steps if item.get("name") == "Upload shard coverage data")
    assert upload["with"]["name"] == "pytest-coverage-shard-${{ matrix.shard }}"
    assert "coverage-data-shard-${{ matrix.shard }}" in upload["with"]["path"]
    assert "report-shard-${{ matrix.shard }}.xml" in upload["with"]["path"]

    aggregate_steps = workflow["jobs"]["unit-pytest-core"]["steps"]
    download = next(
        item for item in aggregate_steps if item.get("name") == "Download shard coverage data"
    )
    assert download["with"]["pattern"] == "pytest-coverage-shard-*"
    assert download["with"]["merge-multiple"] is True


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
    test coverage of 100.0% not reached" no matter how healthy the codebase is —
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
    assert job["if"] == (
        "${{ always() && (github.event_name != 'pull_request' "
        "|| github.event.pull_request.draft == false) }}"
    ), (
        "the stable required check must run even when a matrix shard fails; a "
        "skipped required job can otherwise be treated as non-blocking. The "
        "draft clause is the only permitted narrowing: a draft PR cannot merge, "
        "and marking it ready re-runs the workflow with the clause true, so "
        "every mergeable state still produces a real aggregate result"
    )

    guard = next(
        step for step in job["steps"] if step.get("name") == "Require every core test shard to pass"
    )
    assert guard["env"]["SHARD_RESULT"] == "${{ needs.unit-pytest-core-shard.result }}"
    assert 'if [ "$SHARD_RESULT" != "success" ]' in guard["run"]
    assert "exit 1" in guard["run"]

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
    """The 100 is written once, in pyproject, and never restated in the workflow.

    Shards may pass ``--cov-fail-under=0`` to switch the inherited check off, but
    no job may name a different threshold: that would be a second source of truth
    which could drift away from ``[tool.coverage.report]``.
    """
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "fail_under = 100" in pyproject

    # Covers both spellings: pytest-cov's --cov-fail-under and coverage's own
    # --fail-under, either of which could restate the threshold.
    thresholds = re.findall(r"--(?:cov-)?fail-under=(\d+)", _all_run_commands(workflow))
    assert set(thresholds) <= {"0"}, (
        f"workflow names coverage thresholds {sorted(set(thresholds))}; the floor "
        "belongs in [tool.coverage.report] alone. Only an explicit 0 is allowed, to "
        "switch the inherited check off where it does not apply."
    )


def test_cdk_output_contract_runs_in_the_synthesizing_job(
    workflow: dict[str, Any],
) -> None:
    """Synthesized-output assertions must run after the job creates cdk.out."""
    steps = workflow["jobs"]["unit-cdk-synth"]["steps"]
    synth_index = next(index for index, step in enumerate(steps) if step.get("name") == "cdk synth")
    validation_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Validate synthesized cloud assembly"
    )

    assert validation_index > synth_index
    validation = steps[validation_index]
    assert "tests/test_integration.py::TestCDKOutput" in validation["run"]
    assert "test -d cdk.out" in validation["run"]
    assert "test -f cdk.out/manifest.json" in validation["run"]
    assert validation["env"]["AWS_DEFAULT_REGION"] == "us-east-1"

    upload = next(step for step in steps if step.get("name") == "Upload cdk.out")
    assert "cdk.out/" in upload["with"]["path"]
    assert "report-cdk-output.xml" in upload["with"]["path"]


# ---------------------------------------------------------------------------
# collect_counts: pytest --collect-only is parsed without ever running tests
# ---------------------------------------------------------------------------

#: What ``pytest -q --collect-only`` prints: one node id per line, then a
#: summary line. Parametrized ids count individually; ``some/notes.txt::x`` is
#: not a Python module and the bare summary line has no ``::`` separator.
_COLLECT_OUTPUT = """\
tests/test_a.py::test_one
tests/test_a.py::test_two[param-1]
tests/test_a.py::test_two[param-2]
tests/test_b.py::TestGroup::test_nested
  tests/test_c.py::test_indented
notes.txt::not_python
tests/test_d.py

5 tests collected in 0.12s
"""


def _install_run(
    monkeypatch: pytest.MonkeyPatch,
    split: Any,
    *,
    returncode: int = 0,
    stdout: str = _COLLECT_OUTPUT,
    stderr: str = "",
) -> list[dict[str, Any]]:
    """Replace ``subprocess.run`` inside the script and record every invocation."""
    calls: list[dict[str, Any]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(split, "subprocess", types.SimpleNamespace(run=_run))
    return calls


def test_collect_counts_parses_node_ids_per_file(
    split: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counts follow node ids (so parametrization counts), ignoring non-test lines."""
    calls = _install_run(monkeypatch, split)

    counts = split.collect_counts()

    assert counts == {"tests/test_a.py": 3, "tests/test_b.py": 1, "tests/test_c.py": 1}
    assert len(calls) == 1
    call = calls[0]
    assert call["args"] == [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-o",
        "addopts=",
        "-q",
        "--collect-only",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *split.ignore_args(),
    ]
    assert call["cwd"] == split.REPO_ROOT == PROJECT_ROOT
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["check"] is False


def test_collect_counts_fails_loudly_when_collection_breaks(
    split: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A collection error must fail the job, forwarding pytest's own output."""
    _install_run(
        monkeypatch,
        split,
        returncode=2,
        stdout="ERROR collecting tests/test_broken.py\n",
        stderr="ImportError: no module named nothing\n",
    )

    with pytest.raises(SystemExit, match="pytest collection failed with exit code 2"):
        split.collect_counts()

    err = capsys.readouterr().err
    assert "ERROR collecting tests/test_broken.py" in err
    assert "ImportError: no module named nothing" in err


def test_collect_counts_refuses_to_shard_an_empty_collection(
    split: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero node ids with a clean exit is treated as an error, not an empty shard."""
    _install_run(monkeypatch, split, stdout="no tests ran in 0.01s\n")

    with pytest.raises(SystemExit, match="produced no test ids; refusing to shard"):
        split.collect_counts()


# ---------------------------------------------------------------------------
# main: argument handling and the three output modes
# ---------------------------------------------------------------------------


@pytest.fixture
def collected(split: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Stand in for collection so ``main`` never spawns pytest."""
    counts = dict(_SAMPLE_COUNTS)
    monkeypatch.setattr(split, "collect_counts", lambda: dict(counts))
    return counts


def test_main_prints_the_requested_shard_one_file_per_line(
    split: Any, collected: dict[str, int], capsys: pytest.CaptureFixture[str]
) -> None:
    expected = split.balance(collected, 3)

    assert split.main(["--of", "3", "--shard", "2"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines() == expected[1]
    assert out.endswith("\n")


@pytest.mark.parametrize("shard", [1, 2])
def test_main_shards_together_cover_every_file(
    split: Any, collected: dict[str, int], capsys: pytest.CaptureFixture[str], shard: int
) -> None:
    """The CLI surface agrees with ``balance``: shard N prints group N-1."""
    assert split.main(["--of", "2", "--shard", str(shard)]) == 0
    assert capsys.readouterr().out.splitlines() == split.balance(collected, 2)[shard - 1]


def test_main_summary_reports_per_shard_totals(
    split: Any, collected: dict[str, int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert split.main(["--of", "2", "--summary"]) == 0

    lines = capsys.readouterr().out.splitlines()
    total = sum(collected.values())
    assert lines[0] == f"{total} tests across {len(collected)} files -> 2 shard(s)"
    assert len(lines) == 3
    groups = split.balance(collected, 2)
    for index, group in enumerate(groups, 1):
        shard_total = sum(collected[path] for path in group)
        share = shard_total / total * 100
        assert lines[index] == (
            f"  shard {index}: {shard_total:5d} tests ({share:5.1f}%) in {len(group)} files"
        )


def test_main_json_emits_the_whole_partition(
    split: Any, collected: dict[str, int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert split.main(["--of", "3", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    groups = split.balance(collected, 3)
    assert payload == {
        "total_tests": sum(collected.values()),
        "total_files": len(collected),
        "shards": [
            {"shard": index + 1, "tests": sum(collected[p] for p in group), "files": group}
            for index, group in enumerate(groups)
        ],
    }


def test_main_json_wins_over_summary(
    split: Any, collected: dict[str, int], capsys: pytest.CaptureFixture[str]
) -> None:
    """Machine-readable output is never mixed with the human summary."""
    assert split.main(["--of", "2", "--json", "--summary"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["total_files"] == len(collected)
    assert "shard(s)" not in out


def test_main_requires_shard_unless_summary_or_json(split: Any, collected: dict[str, int]) -> None:
    with pytest.raises(SystemExit, match="--shard is required unless --summary or --json"):
        split.main(["--of", "2"])


@pytest.mark.parametrize("shard", ["0", "3"])
def test_main_rejects_out_of_range_shard_numbers(
    split: Any, collected: dict[str, int], shard: str
) -> None:
    with pytest.raises(SystemExit, match=r"--shard must be between 1 and 2"):
        split.main(["--of", "2", "--shard", shard])


def test_main_rejects_zero_shards(split: Any, collected: dict[str, int]) -> None:
    with pytest.raises(SystemExit, match="--of must be at least 1"):
        split.main(["--of", "0", "--shard", "1"])


def test_main_requires_of(split: Any, collected: dict[str, int]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        split.main(["--shard", "1"])
    assert excinfo.value.code == 2
