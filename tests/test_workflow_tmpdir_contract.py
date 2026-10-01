"""Every Ubuntu job keeps its temporary files on the runner's work disk.

The Ubuntu 26.04 runner image mounts ``/tmp`` as a tmpfs sized to half the
machine's memory with a per-user quota of a few gigabytes
(actions/runner-images#14777). ``unit:cdk:config-matrix`` and three
``unit:pytest:core`` shards hit that quota (``Unknown system error -122``,
``EDQUOT``) copying CDK assets and pytest temporary directories, while the
work disk under ``RUNNER_TEMP`` had more than a hundred gigabytes free. The
kind image loader writes whole image archives to ``TMPDIR`` as well.

The ``runner`` context is not available to ``jobs.<id>.env``, so the fix is
the first step of every job that runs on an Ubuntu label: it exports
``TMPDIR=$RUNNER_TEMP`` through ``GITHUB_ENV`` for the rest of the job.
Python's ``tempfile``, ``mktemp``, Go's ``os.TempDir`` and the CDK all honor
it. The step is identical everywhere and sits first, before checkout, so it
cannot be reordered behind the step that needs it. ``gate:*`` jobs are the
exception: ``tests/test_workflow_gate_contract.py`` pins them to exactly two
steps (checkout and the needs verifier) and they write nothing of size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"

TMPDIR_STEP: dict[str, str] = {
    "name": "Keep temporary files on the work disk",
    "run": 'echo "TMPDIR=${RUNNER_TEMP}" >> "${GITHUB_ENV}"',
}


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml"))
    assert files, "no workflow files found; did the path move?"
    return files


def _runner_labels(job: dict[str, Any]) -> list[str]:
    """The labels a job can run on; a matrix expression resolves against its include list."""
    runner = job.get("runs-on")
    if runner is None:
        return []
    if isinstance(runner, list):
        return [str(label) for label in runner]
    text = str(runner).strip()
    if not text.startswith("${{"):
        return [text]
    key = text.removeprefix("${{").removesuffix("}}").strip().split(".")[-1]
    include = ((job.get("strategy") or {}).get("matrix") or {}).get("include") or []
    labels = [str(entry[key]) for entry in include if isinstance(entry, dict) and key in entry]
    assert labels, f"cannot resolve runs-on {runner!r} from the job's matrix"
    return labels


def _ubuntu_jobs(path: Path) -> list[tuple[str, dict[str, Any]]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = [
        (job_id, job)
        for job_id, job in (document.get("jobs") or {}).items()
        if isinstance(job, dict) and _runner_labels(job)
    ]
    ubuntu: list[tuple[str, dict[str, Any]]] = []
    for job_id, job in jobs:
        labels = _runner_labels(job)
        on_ubuntu = [label.startswith("ubuntu-") for label in labels]
        assert all(on_ubuntu) or not any(on_ubuntu), (
            f"{path.name} / {job_id}: mixes Ubuntu and other runners {labels}"
        )
        if all(on_ubuntu):
            ubuntu.append((job_id, job))
    return ubuntu


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda path: path.name)
def test_every_ubuntu_job_exports_tmpdir_first(path: Path) -> None:
    for job_id, job in _ubuntu_jobs(path):
        steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
        assert steps, f"{path.name} / {job_id} has no steps"
        if str(job.get("name", "")).startswith("gate:"):
            assert TMPDIR_STEP["name"] not in {step.get("name") for step in steps}, (
                f"{path.name} / {job_id}: gate jobs stay at their two pinned steps"
            )
            continue
        first = {key: steps[0].get(key) for key in TMPDIR_STEP}
        assert first == TMPDIR_STEP, (
            f"{path.name} / {job_id}: the first step must be {TMPDIR_STEP}, got {steps[0]}"
        )
        assert [step for step in steps if step.get("name") == TMPDIR_STEP["name"]] == [steps[0]], (
            f"{path.name} / {job_id}: the TMPDIR step appears more than once"
        )


def test_the_contract_covers_the_jobs_it_is_written_for() -> None:
    """The jobs that hit the quota are Ubuntu jobs this contract reaches."""
    covered = {
        f"{path.name}:{job_id}" for path in _workflow_files() for job_id, _ in _ubuntu_jobs(path)
    }
    for required in (
        "unit-tests.yml:unit-cdk-config-matrix",
        "unit-tests.yml:unit-pytest-core",
        "integration-tests.yml:integration-kind-cluster-e2e",
        "integration-tests.yml:integration-kind-examples-smoke",
        "integration-tests.yml:integration-docker-dev-container",
    ):
        assert required in covered, required
    assert len(covered) >= 80, len(covered)


def test_non_ubuntu_jobs_are_left_alone() -> None:
    """macOS and Windows runners do not mount a quota-limited /tmp; the step is Linux-only."""
    others = [
        (path.name, job_id)
        for path in _workflow_files()
        for job_id, job in (
            yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}
        ).items()
        if isinstance(job, dict)
        and _runner_labels(job)
        and not any(label.startswith("ubuntu-") for label in _runner_labels(job))
    ]
    assert others, "expected at least one non-Ubuntu job (the dev-alias matrix)"
    for name, job_id in others:
        document = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
        steps = document["jobs"][job_id].get("steps") or []
        assert TMPDIR_STEP["name"] not in {
            step.get("name") for step in steps if isinstance(step, dict)
        }, f"{name} / {job_id}"
