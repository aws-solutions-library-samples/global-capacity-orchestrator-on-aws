"""Draft PRs must not spend shared runner capacity.

Every push to a draft pull request used to start all nine PR workflows,
matrices included — roughly 66 jobs for a branch its author has explicitly
marked as not ready to review. The Amazon-wide GitHub Actions pool is shared,
so that waste saturates a queue other teams sit in.

The fix has three parts, and all three have to hold together or the gate is
either useless or actively harmful:

1. Every job carries ``pull_request.draft == false``. GitHub has no
   workflow-level ``if:``, so a single ungated job keeps burning a runner and
   silently reopens the hole.
2. ``ready_for_review`` is in the trigger ``types``. Without it, a PR that
   leaves draft never re-fires and the suite simply never runs — the failure
   mode here is a *missing* gate, not a slow one.
3. Declaring ``types`` at all replaces the default
   ``[opened, synchronize, reopened]``, so those three must be restated
   explicitly or pushes to an open PR stop triggering CI.

These tests pin all three so a new workflow, or a new job in an existing one,
cannot land half of the arrangement.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"

#: The clause that makes a job skip on a draft pull request. Asserted as a
#: substring, not an exact ``if:``, because several jobs legitimately compose it
#: with other conditions (``always()``, the dependency path filter).
DRAFT_CLAUSE = "github.event.pull_request.draft == false"

#: ``types`` replaces the default set, so a draft-gated workflow must restate
#: the defaults alongside ``ready_for_review``.
REQUIRED_PR_TYPES = {"opened", "synchronize", "reopened", "ready_for_review"}

#: pr-type-label.yml is the one workflow deliberately left ungated. It costs a
#: single short job, and the type label drives release-note grouping and review
#: routing — both worth having correct while a PR is still a draft. It also
#: swaps ``synchronize`` for ``edited``, because the label is derived from the PR
#: body rather than from the code.
UNGATED_PULL_REQUEST_WORKFLOWS = {"pr-type-label.yml"}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    # ``on`` is a YAML 1.1 boolean, so safe_load yields the key True.
    triggers = document.get(True) or document.get("on") or {}
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    return triggers


def _jobs(document: dict[str, Any]) -> dict[str, Any]:
    jobs = document.get("jobs") or {}
    return {name: spec for name, spec in jobs.items() if isinstance(spec, dict)}


def _pull_request_workflows() -> list[Path]:
    files = [
        path
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        if "pull_request" in _triggers(_load(path))
    ]
    assert files, "no pull_request workflows found; did the path move?"
    return files


def _gated_workflows() -> list[Path]:
    return [p for p in _pull_request_workflows() if p.name not in UNGATED_PULL_REQUEST_WORKFLOWS]


def test_the_pull_request_workflow_set_is_known() -> None:
    """Pin the inventory so a new PR workflow has to opt in or out on purpose.

    A workflow added without a decision here would default to ungated and go
    unnoticed, which is exactly how the original 246-jobs-a-day figure built up.
    """
    assert {path.name for path in _pull_request_workflows()} == {
        "floci-tests.yml",
        "grafana-dashboards.yml",
        "inference-streaming-proxy.yml",
        "integration-tests.yml",
        "lint.yml",
        "mooncake-image.yml",
        "pr-type-label.yml",
        "security.yml",
        "unit-tests.yml",
    }


@pytest.mark.parametrize("path", _pull_request_workflows(), ids=lambda p: p.name)
def test_pull_request_workflows_rerun_when_a_pr_leaves_draft(path: Path) -> None:
    """``ready_for_review`` is what makes draft gating safe rather than lossy.

    Skipping jobs on drafts is only correct if something re-runs them when the
    PR is marked ready. That event is ``ready_for_review``; without it a PR
    could reach a mergeable state having never run the suite at all.
    """
    types = (_triggers(_load(path))["pull_request"] or {}).get("types")
    assert types is not None, (
        f"{path.name}: declare pull_request types explicitly and include "
        "ready_for_review, or a PR leaving draft will not re-trigger this workflow"
    )
    assert "ready_for_review" in types, f"{path.name}: missing the ready_for_review trigger"


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_gated_workflows_restate_the_default_pull_request_types(path: Path) -> None:
    """Declaring ``types`` discards the defaults, so they must be written back.

    The dangerous omission is ``synchronize``: lose it and pushes to an open PR
    stop running CI, while opened/reopened keep working well enough that the
    hole is easy to miss in review.
    """
    types = set((_triggers(_load(path))["pull_request"] or {}).get("types") or [])
    assert types == REQUIRED_PR_TYPES, (
        f"{path.name}: pull_request types must be exactly {sorted(REQUIRED_PR_TYPES)}, got {sorted(types)}"
    )


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_every_job_skips_draft_pull_requests(path: Path) -> None:
    """One ungated job is enough to keep holding a runner slot.

    GitHub offers no workflow-level ``if:``, so the gate is per job and the
    invariant has to be checked per job. Matrix jobs matter most: a single
    ungated matrix expands to several concurrent runners.
    """
    ungated = [
        name
        for name, job in _jobs(_load(path)).items()
        if DRAFT_CLAUSE not in " ".join(str(job.get("if", "")).split())
    ]
    assert not ungated, (
        f"{path.name}: these jobs run on draft PRs; add `{DRAFT_CLAUSE}` to each `if:`: {ungated}"
    )


@pytest.mark.parametrize(
    "path",
    [WORKFLOW_DIR / name for name in sorted(UNGATED_PULL_REQUEST_WORKFLOWS)],
    ids=lambda p: p.name,
)
def test_the_documented_exemption_stays_deliberate(path: Path) -> None:
    """The exemption is a decision, so drifting into it must fail too.

    If someone later gates pr-type-label.yml, that is a behaviour change worth
    making explicitly here rather than by silent edit — and if someone adds a
    workflow to the exemption set, this test makes them prove it is ungated on
    purpose.
    """
    conditions = " ".join(str(job.get("if", "")) for job in _jobs(_load(path)).values())
    assert DRAFT_CLAUSE not in conditions, (
        f"{path.name} is listed as an intentional draft-gating exemption but now "
        "gates on draft state; remove it from UNGATED_PULL_REQUEST_WORKFLOWS instead"
    )


@pytest.mark.parametrize("path", _pull_request_workflows(), ids=lambda p: p.name)
def test_pull_request_workflows_cancel_superseded_runs(path: Path) -> None:
    """A superseded run holds a runner slot to produce a result nobody reads.

    Every PR-triggered workflow groups per ref and cancels in progress, so a
    force-push or a rapid series of edits leaves one live run rather than a
    queue of doomed ones.
    """
    concurrency = _load(path).get("concurrency")
    assert isinstance(concurrency, dict), f"{path.name}: needs a top-level concurrency block"
    assert concurrency.get("group") == "${{ github.workflow }}-${{ github.ref }}", (
        f"{path.name}: concurrency group must be per workflow and ref, got {concurrency.get('group')!r}"
    )
    assert concurrency.get("cancel-in-progress") is True, (
        f"{path.name}: PR workflows must cancel superseded runs"
    )
