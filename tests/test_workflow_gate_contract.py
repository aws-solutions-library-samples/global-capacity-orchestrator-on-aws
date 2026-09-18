"""One required status check per PR workflow: the ``gate:*`` jobs.

Branch protection on ``main`` used to require every CI job by name — 85
entries, matrix legs included, each of which had to be edited by hand when a
job was added, renamed or re-sharded. Names that drifted from the jobs were
silently unsatisfiable: a required check that never reports blocks every
merge with "Expected — Waiting for status to be reported", and nothing in the
repository could catch it.

The replacement is one ``gate:<workflow>`` job at the end of every
PR-triggered workflow. It ``needs`` every other job in the file, runs under
``always()``, and hands ``toJSON(needs)`` to
``.github/scripts/verify_gate_needs.py``, which refuses to pass unless each
needed job succeeded (or was skipped under a documented allowance). The
ruleset requires exactly the eight gates, so the merge rule is "every job in
every PR workflow passed" and a job change never touches the ruleset again.

That only holds while four things stay true, and this module pins them:

1. Every gated workflow has exactly one gate, and its ``needs`` lists every
   other job — a job left out of ``needs`` is a job whose failure can no
   longer block a merge.
2. The gate is scheduled with ``always()`` composed with the draft clause, so
   a failed, cancelled or skipped dependency produces a *failing* gate rather
   than a skipped one (GitHub treats a skipped required check as passing).
3. The skip allowances the gate grants match exactly the jobs whose ``if:``
   is conditional beyond the draft clause — every unconditional job must be
   ``success``, and every conditional job needs a written reason to skip.
4. Everything that depends on the gate inventory agrees with it: the
   ``release.yml`` dispatch list (the release PR's checks arrive via
   ``workflow_dispatch``), the ``.github/CI.md`` required-checks table, and
   the absence of path filters (a paths-filtered workflow never reports on a
   PR outside its paths, which blocks the merge instead of skipping the job).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CI_DOC = ROOT / ".github" / "CI.md"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
VERIFIER = ".github/scripts/verify_gate_needs.py"

#: PR-triggered workflows that carry no gate. pr-type-label.yml is a labeler
#: that runs on ``edited`` rather than ``synchronize`` and skips fork PRs; it
#: was never a merge gate and cannot be one.
UNGATED_PULL_REQUEST_WORKFLOWS = {"pr-type-label.yml"}

#: The exact ``if:`` every gate carries. ``always()`` is what turns a failed
#: or skipped dependency into a *failing* gate instead of a skipped one, and
#: the draft clause is the single permitted narrowing (a draft PR cannot
#: merge, and ``ready_for_review`` re-runs the workflow with it true).
GATE_IF = (
    "${{ always() && (github.event_name != 'pull_request' "
    "|| github.event.pull_request.draft == false) }}"
)

#: The two ``if:`` shapes an *unconditional* job may carry: it runs on every
#: non-draft event, so its result must be ``success`` and it needs no skip
#: allowance. Any other ``if:`` makes the job conditional.
UNCONDITIONAL_IFS = {
    "github.event_name != 'pull_request' || github.event.pull_request.draft == false",
    GATE_IF,
}

#: The skip allowances each gate grants, pinned so a new conditional job has
#: to be given a reason here and in the workflow, in the same review.
#: ``None`` is an unconditional allowance; a tuple is ``(dep, output, value)``.
EXPECTED_ALLOWANCES: dict[str, dict[str, tuple[str, str, str] | None]] = {
    "unit-tests.yml": {
        # Runs only on pull requests; push and workflow_dispatch skip it.
        "changes": None,
        # Skip only when the dependency-path filter reported no change.
        "unit-lockfile-freshness": ("changes", "deps", "false"),
        "unit-fresh-install": ("changes", "deps", "false"),
    },
}

_ALLOW_SKIPPED = re.compile(r"--allow-skipped\s+(\S+)")


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


def _jobs(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: spec for name, spec in (document.get("jobs") or {}).items() if isinstance(spec, dict)
    }


def _gated_workflows() -> list[Path]:
    files = [
        path
        for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        if "pull_request" in _triggers(_load(path))
        and path.name not in UNGATED_PULL_REQUEST_WORKFLOWS
    ]
    assert files, "no gated pull_request workflows found; did the path move?"
    return files


def _gate_id(path: Path) -> str:
    return f"gate-{path.stem}"


def _gate_name(path: Path) -> str:
    return f"gate:{path.stem}"


def _gate(path: Path) -> dict[str, Any]:
    jobs = _jobs(_load(path))
    assert _gate_id(path) in jobs, f"{path.name}: no `{_gate_id(path)}` job"
    return jobs[_gate_id(path)]


def _normalised_if(job: dict[str, Any]) -> str:
    return " ".join(str(job.get("if", "")).split())


def _gate_run_script(path: Path) -> str:
    steps = [step for step in _gate(path).get("steps", []) if VERIFIER in str(step.get("run", ""))]
    assert len(steps) == 1, (
        f"{path.name}: expected exactly one step invoking {VERIFIER}, found {len(steps)}"
    )
    return str(steps[0]["run"])


def _allowances(path: Path) -> dict[str, tuple[str, str, str] | None]:
    """The ``--allow-skipped`` grants in the gate's run script, parsed like the verifier does."""
    granted: dict[str, tuple[str, str, str] | None] = {}
    for spec in _ALLOW_SKIPPED.findall(_gate_run_script(path)):
        job, _, condition = spec.partition("=")
        assert job not in granted, f"{path.name}: `{job}` is allowed to skip twice"
        if not condition:
            granted[job] = None
            continue
        dependency, _, rest = condition.partition(".")
        output, _, value = rest.partition("=")
        granted[job] = (dependency, output, value)
    return granted


# ─── the inventory ────────────────────────────────────────────────────────────


def test_the_gated_workflow_set_is_known() -> None:
    """Pin the inventory: a new PR workflow must be gated (and required) on purpose.

    The ruleset requires exactly these eight ``gate:*`` checks. A workflow
    added here without a gate would run on every PR yet never be able to block
    one; a workflow gated but missing from the ruleset is the same silent gap.
    """
    assert {path.name for path in _gated_workflows()} == {
        "floci-tests.yml",
        "grafana-dashboards.yml",
        "inference-streaming-proxy.yml",
        "integration-tests.yml",
        "lint.yml",
        "mooncake-image.yml",
        "security.yml",
        "unit-tests.yml",
    }


def test_the_ungated_workflow_is_the_draft_gating_exemption_too() -> None:
    """Both contracts exempt the same file, so neither can quietly widen its own list."""
    from tests import test_workflow_draft_pr_gating_contract as draft_contract

    assert UNGATED_PULL_REQUEST_WORKFLOWS == draft_contract.UNGATED_PULL_REQUEST_WORKFLOWS


# ─── 1. one gate that needs every job ─────────────────────────────────────────


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_the_gate_is_the_last_job_and_named_after_the_workflow(path: Path) -> None:
    """``gate-<file>`` / ``gate:<file>``: the check name is derivable from the filename.

    Check names must be unique across workflows (the ruleset matches on the
    name alone), and deriving them from the filename is what keeps the ruleset
    entry, the dispatch list and the docs table trivially in agreement.
    """
    jobs = _jobs(_load(path))
    assert list(jobs)[-1] == _gate_id(path), (
        f"{path.name}: the gate must be the last job in the file"
    )
    assert jobs[_gate_id(path)].get("name") == _gate_name(path)
    others = [
        job_id for job_id, job in jobs.items() if str(job.get("name", "")).startswith("gate:")
    ]
    assert others == [_gate_id(path)], (
        f"{path.name}: exactly one gate:* job is allowed, found {others}"
    )


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_the_gate_needs_every_other_job(path: Path) -> None:
    """A job absent from ``needs`` is a job whose failure can no longer block a merge.

    Matrix jobs are covered by their single job id: ``needs.<job>.result`` is
    the aggregate over every leg, so one entry gates all of them.
    """
    jobs = _jobs(_load(path))
    needs = _gate(path).get("needs")
    assert isinstance(needs, list), f"{path.name}: the gate's `needs` must be a list"
    expected = sorted(job_id for job_id in jobs if job_id != _gate_id(path))
    assert needs == expected, (
        f"{path.name}: the gate must need every other job, alphabetically — "
        f"missing={sorted(set(expected) - set(needs))!r} "
        f"extra={sorted(set(needs) - set(expected))!r} "
        f"(or the list is out of order)"
    )


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_no_job_hides_its_failure_from_the_gate(path: Path) -> None:
    """A job-level ``continue-on-error`` reports ``success`` to ``needs`` after failing."""
    offenders = [job_id for job_id, job in _jobs(_load(path)).items() if "continue-on-error" in job]
    assert not offenders, (
        f"{path.name}: job-level continue-on-error would make these jobs read as "
        f"success to the gate even when they fail: {offenders}"
    )


# ─── 2. always(), bounded, on a hosted runner ─────────────────────────────────


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_the_gate_runs_under_always_with_the_draft_clause(path: Path) -> None:
    """Without ``always()`` a failed dependency skips the gate — and a skip passes."""
    assert _normalised_if(_gate(path)) == GATE_IF, (
        f"{path.name}: the gate's `if:` must be exactly {GATE_IF}"
    )


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_the_gate_is_small_and_bounded(path: Path) -> None:
    """The gate is a checkout and one stdlib script; it must never become a place to run work."""
    gate = _gate(path)
    assert gate.get("runs-on") == "ubuntu-latest"
    assert gate.get("timeout-minutes") == 5
    assert "strategy" not in gate and "continue-on-error" not in gate and "outputs" not in gate
    steps = gate.get("steps", [])
    assert len(steps) == 2, (
        f"{path.name}: the gate is a checkout plus the verifier step, found {len(steps)} steps"
    )
    checkout, verify = steps
    assert str(checkout.get("uses", "")).startswith("actions/checkout@")
    assert checkout.get("with", {}).get("sparse-checkout") == ".github/scripts"
    assert verify.get("env", {}).get("NEEDS_JSON") == "${{ toJSON(needs) }}"
    assert 'python3 .github/scripts/verify_gate_needs.py --needs "$NEEDS_JSON"' in str(
        verify.get("run", "")
    )


# ─── 3. skip allowances mirror the conditional jobs ───────────────────────────


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_skip_allowances_match_the_conditional_jobs_exactly(path: Path) -> None:
    """Every conditional job needs a written reason to skip; no unconditional job gets one.

    A job whose ``if:`` is one of the two unconditional shapes runs on every
    non-draft event, so a ``skipped`` result from it is a broken condition and
    the gate must fail. A job with any other ``if:`` can legitimately skip,
    and the gate must say when — otherwise it would fail every run in which
    the filter did its job.
    """
    jobs = _jobs(_load(path))
    conditional = {
        job_id
        for job_id, job in jobs.items()
        if job_id != _gate_id(path) and _normalised_if(job) not in UNCONDITIONAL_IFS
    }
    granted = _allowances(path)
    assert set(granted) == conditional, (
        f"{path.name}: --allow-skipped must name exactly the conditional jobs — "
        f"unexplained={sorted(conditional - set(granted))!r} "
        f"needless={sorted(set(granted) - conditional)!r}"
    )
    assert granted == EXPECTED_ALLOWANCES.get(path.name, {}), (
        f"{path.name}: the allowances changed; update EXPECTED_ALLOWANCES deliberately"
    )


def test_conditional_allowances_read_an_output_the_dependency_declares() -> None:
    """``DEP.OUTPUT=VALUE`` must name a real job output, or the allowance can never be satisfied."""
    for name, allowances in EXPECTED_ALLOWANCES.items():
        jobs = _jobs(_load(WORKFLOW_DIR / name))
        for job_id, condition in allowances.items():
            if condition is None:
                continue
            dependency, output, _value = condition
            assert output in (jobs[dependency].get("outputs") or {}), (
                f"{name}: `{job_id}` may skip on `{dependency}.outputs.{output}`, "
                f"but `{dependency}` declares no such output"
            )


def test_the_verifier_parses_the_workflow_allowances_the_same_way() -> None:
    """The contract's parser and the real verifier must agree on every committed spec."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("verify_gate_needs", ROOT / VERIFIER)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their postponed annotations through sys.modules.
    sys.modules[spec.name] = verifier
    spec.loader.exec_module(verifier)
    for path in _gated_workflows():
        for raw in _ALLOW_SKIPPED.findall(_gate_run_script(path)):
            allowance = verifier.parse_allowance(raw)
            expected = _allowances(path)[allowance.job]
            actual = (
                None
                if allowance.condition is None
                else (
                    allowance.condition.dependency,
                    allowance.condition.output,
                    allowance.condition.value,
                )
            )
            assert actual == expected, f"{path.name}: {raw!r} parses differently in the verifier"


# ─── 4. everything that depends on the inventory agrees with it ───────────────


@pytest.mark.parametrize("path", _gated_workflows(), ids=lambda p: p.name)
def test_gated_workflows_carry_no_path_filter(path: Path) -> None:
    """A paths-filtered workflow does not run on a PR outside its paths.

    Its required gate then never reports, and the PR is blocked waiting for a
    check that will not come — the opposite of the skip a path filter is meant
    to be. grafana-dashboards.yml lost its filter for exactly this reason.
    """
    triggers = _triggers(_load(path))
    for event in ("pull_request", "push"):
        spec = triggers.get(event) or {}
        assert "paths" not in spec and "paths-ignore" not in spec, (
            f"{path.name}: `{event}` must not be paths-filtered while its gate is a required check"
        )


def test_release_dispatches_every_gated_workflow() -> None:
    """The release PR is opened with ``GITHUB_TOKEN``, so its checks arrive only by dispatch.

    A gated workflow missing from this list leaves the release PR waiting on
    a required check that never runs; an extra entry dispatches a workflow
    whose result nothing requires.
    """
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"for workflow in \\\n(.*?); do", text, re.DOTALL)
    assert match, "release.yml: the `for workflow in` dispatch loop moved"
    dispatched = shlex.split(match.group(1).replace("\\\n", " "))
    assert dispatched == sorted(dispatched), "release.yml: keep the dispatch list alphabetical"
    assert set(dispatched) == {path.name for path in _gated_workflows()}


def test_ci_doc_lists_exactly_the_gates_as_required_checks() -> None:
    """``.github/CI.md`` "Required checks" is the human-readable ruleset; it must match the gates."""
    text = CI_DOC.read_text(encoding="utf-8")
    section = text.split("### Required checks", 1)
    assert len(section) == 2, ".github/CI.md: the 'Required checks' section moved"
    body = section[1].split("\n### ", 1)[0]
    documented = re.findall(
        r"^\| `(gate:[a-z-]+)` \| `workflows/([a-z-]+\.yml)` \|", body, re.MULTILINE
    )
    assert documented, ".github/CI.md: the required-checks table has no gate rows"
    assert [name for name, _ in documented] == sorted(name for name, _ in documented), (
        ".github/CI.md: keep the required-checks table alphabetical"
    )
    assert {name for name, _ in documented} == {_gate_name(path) for path in _gated_workflows()}
    for name, workflow in documented:
        assert name == f"gate:{workflow.removesuffix('.yml')}", (
            f"{name} is documented against {workflow}"
        )
