"""Tests for ``scripts/split_bats.py``, which shards the BATS suite across CI jobs.

The same property that matters for the pytest splitter matters here: sharding
must neither lose nor duplicate a suite, because a splitter that drops a
``.bats`` file keeps every shard green while part of the suite silently stops
running. ``test_partition_covers_every_suite_exactly_once`` pins it for shard
counts from 1 to 6.

The rest guard what a change to the shard count or to a suite's cost would
touch: that the workflow matrix and the script agree, that ``--of`` is derived
from the matrix rather than hardcoded a second time, that the per-suite weights
name real suites and are the reason the heaviest suite gets a shard to itself,
and that the plain and traced runs read the same shard list.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "split_bats.py"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "unit-tests.yml"
SUITE_DIR = PROJECT_ROOT / "tests" / "BATS"

#: A stand-in inventory: uneven counts, so balancing has something to do. The
#: weighted driver suite is present because ``weigh`` refuses a weight whose
#: suite is not in the inventory.
_SAMPLE_COUNTS = {
    "tests/BATS/test_dependency_scan_driver.bats": 22,
    "tests/BATS/test_a.bats": 380,
    "tests/BATS/test_b.bats": 100,
    "tests/BATS/test_c.bats": 70,
    "tests/BATS/test_d.bats": 57,
    "tests/BATS/test_e.bats": 22,
    "tests/BATS/test_f.bats": 7,
    "tests/BATS/test_g.bats": 3,
    "tests/BATS/test_h.bats": 0,
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("gco_split_bats", SCRIPT)
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


def _shard_job(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]["unit-bats-shell-shard"]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}, found {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# The partition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 6])
def test_partition_covers_every_suite_exactly_once(split: Any, shards: int) -> None:
    """Sharding must not lose or duplicate a single suite, a zero-test one included."""
    groups = split.balance(split.weigh(_SAMPLE_COUNTS), shards)
    assert len(groups) == shards
    flattened = [path for group in groups for path in group]
    assert sorted(flattened) == sorted(_SAMPLE_COUNTS), "a suite was lost or duplicated"
    assert len(flattened) == len(set(flattened)), "a suite landed in more than one shard"


def test_partition_is_deterministic(split: Any) -> None:
    weights = split.weigh(_SAMPLE_COUNTS)
    assert split.balance(weights, 4) == split.balance(dict(reversed(list(weights.items()))), 4)


def test_collect_counts_reads_every_suite_on_disk(split: Any) -> None:
    """One entry per ``tests/BATS/*.bats`` file, counting ``@test`` declarations."""
    counts = split.collect_counts()
    on_disk = sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in SUITE_DIR.glob("*.bats"))
    assert sorted(counts) == on_disk
    assert on_disk, "the BATS suite directory is empty; did it move?"
    for path, count in counts.items():
        declared = len(re.findall(r"^@test\s", (PROJECT_ROOT / path).read_text(), re.MULTILINE))
        assert count == declared, path
        assert count > 0, f"{path} declares no tests"


def test_collect_counts_refuses_an_empty_suite_directory(split: Any, tmp_path: Path) -> None:
    (tmp_path / "tests" / "BATS").mkdir(parents=True)
    with pytest.raises(SystemExit, match=r"no \.bats files"):
        split.collect_counts(tmp_path)


def test_suite_weights_name_real_suites_with_a_measured_reason(split: Any) -> None:
    """Every weighted suite exists, costs more than a typical test, and says why."""
    assert split.SUITE_WEIGHTS, "the weights table is what makes the balance match the timings"
    for path, (weight, reason) in split.SUITE_WEIGHTS.items():
        assert (PROJECT_ROOT / path).is_file(), f"SUITE_WEIGHTS names a missing suite: {path}"
        assert weight > 1, f"{path}: a weight of {weight} is the default; drop the entry"
        assert re.search(r"\d+(\.\d+)? s", reason), f"{path}: the reason must quote a measurement"
        assert re.search(r"20\d\d-\d\d-\d\d", reason), (
            f"{path}: the reason must say when it was measured"
        )


def test_weigh_rejects_a_weight_for_a_suite_that_does_not_exist(split: Any) -> None:
    """A renamed suite must not keep a stale weight that silently stops applying."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(split, "SUITE_WEIGHTS", {"tests/BATS/test_gone.bats": (25, "moved")})
        with pytest.raises(SystemExit, match="do not exist"):
            split.weigh(_SAMPLE_COUNTS)


def test_the_driver_suite_gets_a_shard_to_itself(split: Any) -> None:
    """The reason the weights exist: by count alone it would share a shard and be the long pole.

    On the 2026-10-30 timings, each dependency-scan driver test costs about 25
    typical tests, so weighted it is over a third of the suite and the greedy
    pack must leave it alone in its shard. Balanced by count it would be
    packed with ten other suites and that shard would run about 30% longer
    than the heaviest file by itself.
    """
    driver = "tests/BATS/test_dependency_scan_driver.bats"
    counts = split.collect_counts()
    groups = split.balance(split.weigh(counts), 4)
    assert [driver] in groups, "the driver suite should be alone in its shard"
    by_count = split.balance(counts, 4)
    assert [driver] not in by_count, (
        "if counts alone isolate the driver, the weight is no longer needed"
    )


def test_summary_and_json_describe_the_same_partition(
    split: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert split.main(["--of", "4", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert len(document["shards"]) == 4
    assert sum(shard["tests"] for shard in document["shards"]) == document["total_tests"]
    assert sum(shard["weight"] for shard in document["shards"]) == document["total_weight"]
    assert sorted(path for shard in document["shards"] for path in shard["files"]) == sorted(
        split.collect_counts()
    )

    assert split.main(["--of", "4", "--summary"]) == 0
    summary = capsys.readouterr().out
    assert f"{document['total_tests']} tests (weight {document['total_weight']})" in summary
    assert summary.count("shard ") == 4


def test_shard_lists_a_shard_and_rejects_bad_numbers(
    split: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert split.main(["--shard", "1", "--of", "4"]) == 0
    listed = capsys.readouterr().out.split()
    assert listed and all(path.startswith("tests/BATS/") for path in listed)
    with pytest.raises(SystemExit, match="between 1 and 4"):
        split.main(["--shard", "5", "--of", "4"])
    with pytest.raises(SystemExit, match="--shard is required"):
        split.main(["--of", "4"])


def test_script_runs_standalone_with_the_system_interpreter() -> None:
    """The bats shard has no project virtualenv: standard library only, run as a file."""
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--of", "4", "--summary"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "-> 4 shard(s)" in result.stdout


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


def test_workflow_shard_matrix_is_a_contiguous_range(workflow: dict[str, Any]) -> None:
    """The matrix must be 1..N so shard numbers line up with ``--shard``."""
    shards = _shard_job(workflow)["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1)), (
        f"matrix.shard must be a contiguous 1-based range, got {shards}"
    )


def test_workflow_uses_four_shards_that_do_not_fail_fast(workflow: dict[str, Any]) -> None:
    """Four cells: the heaviest suite alone, the second heaviest alone, the rest in two.

    On the 2026-10-30 timings that is 440 s, 364 s, 175 s and 202 s of
    plain-plus-traced running against 1,260 s for the single job. A fifth
    shard would not shorten the longest cell, which is one file.
    """
    job = _shard_job(workflow)
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]


def test_workflow_derives_shard_total_from_the_matrix(workflow: dict[str, Any]) -> None:
    """``--of`` must come from ``strategy.job-total``, never a second literal."""
    select = _step(_shard_job(workflow), "Select this shard's suites")
    assert '--of "$SHARDS"' in select["run"]
    assert '--shard "$SHARD"' in select["run"]
    assert select["env"] == {"SHARD": "${{ matrix.shard }}", "SHARDS": "${{ strategy.job-total }}"}
    for literal in re.findall(r"--of\s+(\d+)", select["run"]):
        pytest.fail(f"--of is hardcoded to {literal}; derive it from strategy.job-total")


def test_plain_and_traced_runs_read_the_same_shard_list(workflow: dict[str, Any]) -> None:
    """The traced run must execute exactly what the plain run executed."""
    job = _shard_job(workflow)
    select = _step(job, "Select this shard's suites")["run"]
    assert "> shard-suites.txt" in select
    for name in ("Run BATS suite", "Measure shell coverage"):
        run = _step(job, name)["run"]
        assert "mapfile -t SHARD_FILES < shard-suites.txt" in run, name
        assert '"${SHARD_FILES[@]}"' in run, name
        assert "tests/BATS/\n" not in run and "tests/BATS/ " not in run, (
            f"{name} must run the shard's suites, not the whole directory"
        )


def test_shard_artifacts_are_dynamic_and_aggregate_uses_a_glob(workflow: dict[str, Any]) -> None:
    """Adding a matrix cell must require no artifact-name edits."""
    job = _shard_job(workflow)
    measure = _step(job, "Measure shell coverage")["run"]
    assert 'mv coverage/.resultset.json "bash-coverage-shard-${SHARD}.resultset.json"' in measure
    upload = _step(job, "Upload shard coverage data")
    assert upload["with"]["name"] == "bash-coverage-shard-${{ matrix.shard }}"
    assert "bash-coverage-shard-${{ matrix.shard }}.resultset.json" in upload["with"]["path"]
    assert upload["if"] == "always()"
    # merge-multiple flattens every shard's files into one directory, so
    # nothing uploaded may share a name across shards.
    for line in upload["with"]["path"].splitlines():
        assert "${{ matrix.shard }}" in line, f"{line!r} would collide across shards"
    assert (
        'cp shard-suites.txt "bash-coverage-shard-${SHARD}.suites.txt"'
        in (_step(job, "Select this shard's suites")["run"])
    )

    aggregate = workflow["jobs"]["unit-bats-shell"]
    assert aggregate["needs"] == "unit-bats-shell-shard"
    download = _step(aggregate, "Download shard coverage data")
    assert download["with"]["pattern"] == "bash-coverage-shard-*"
    assert download["with"]["merge-multiple"] is True
    assert download["with"]["path"] == "shard-artifacts"


def test_aggregate_keeps_the_required_check_name_and_fails_on_any_shard(
    workflow: dict[str, Any],
) -> None:
    """``unit:bats:shell`` is the name branch protection knows; it must mean the whole suite."""
    aggregate = workflow["jobs"]["unit-bats-shell"]
    assert aggregate["name"] == "unit:bats:shell"
    assert _shard_job(workflow)["name"].startswith("unit:bats:shell (shard ")
    assert aggregate["if"] == (
        "${{ always() && (github.event_name != 'pull_request' "
        "|| github.event.pull_request.draft == false) }}"
    )
    require = _step(aggregate, "Require every shard to pass")
    assert require["env"] == {"SHARD_RESULT": "${{ needs.unit-bats-shell-shard.result }}"}
    assert 'if [ "$SHARD_RESULT" != "success" ]' in require["run"]
    gate = _step(aggregate, "Enforce the shell coverage floor")["run"]
    assert "check_bash_coverage.py shard-artifacts/ --report coverage/report" in gate


def test_report_artifact_keeps_the_layout_pages_publishes(workflow: dict[str, Any]) -> None:
    """pages.yml moves ``report/`` from the artifact root; the shard inputs ride inside coverage/."""
    aggregate = workflow["jobs"]["unit-bats-shell"]
    gate = _step(aggregate, "Enforce the shell coverage floor")["run"]
    assert "cp -R shard-artifacts/. coverage/shards/" in gate
    upload = _step(aggregate, "Upload shell coverage report")
    assert upload["with"]["name"] == "bash-coverage-report"
    assert upload["with"]["path"] == "coverage/"
    assert upload["if"] == "always()"
