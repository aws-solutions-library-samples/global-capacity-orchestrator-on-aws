"""Security invariants for the GitHub Actions surface.

A red-team pass over ``.github/workflows`` and ``.github/actions`` found no
exploitable weakness. These tests encode *why* it was clean, so the next
workflow edit cannot quietly reintroduce one of the classic Actions
vulnerability classes. Each test states the attack it forecloses.

The checks are deliberately structural (parse the YAML, walk jobs and steps)
rather than grep-based, so a reformatted workflow cannot slip past them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
ACTION_DIR = ROOT / ".github" / "actions"

#: Expression contexts an attacker can influence without write access: PR
#: titles/bodies/branch names, issue and comment text, and fork metadata.
#: Interpolating any of these straight into a ``run:`` script is the GitHub
#: Actions script-injection sink — the shell sees attacker text as code.
UNTRUSTED_CONTEXT_RE = re.compile(
    r"\$\{\{[^}]*\b("
    r"github\.event\."
    r"|github\.head_ref"
    r"|github\.triggering_actor"
    r"|inputs\."
    r"|env\.GITHUB_HEAD_REF"
    r")",
)

#: Only these workflows may keep the checkout token in .git/config: both push
#: refs as part of the two-stage release. Every other workflow must run with
#: persist-credentials disabled or unset (checkout's default keeps it, so the
#: point of this list is that the set of credential-bearing jobs stays known
#: and small).
CREDENTIAL_PERSISTING_WORKFLOWS = {"release.yml", "release-publish.yml"}

#: GitHub-hosted runner labels this project uses. A self-hosted label would
#: mean untrusted PR code executing on a persistent machine with whatever
#: credentials and state that host carries.
ALLOWED_RUNNER_LABELS = {
    "ubuntu-latest",
    "ubuntu-24.04-arm",
    "macos-15",
    "windows-latest",
}


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml"))
    assert files, "no workflow files found; did the path move?"
    return files


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _jobs(document: dict[str, Any]) -> dict[str, Any]:
    jobs = document.get("jobs") or {}
    return {name: spec for name, spec in jobs.items() if isinstance(spec, dict)}


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in (job.get("steps") or []) if isinstance(step, dict)]


def _strip_comments(script: str) -> str:
    """Drop whole-line shell comments.

    Workflows legitimately *document* the unsafe pattern in a comment (for
    example release.yml explaining why the bump input rides in ``env:``
    instead of being interpolated). Comments cannot execute, so scanning them
    would force prose to avoid naming the very footgun it warns about.
    """
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_untrusted_expression_reaches_a_run_script(path: Path) -> None:
    """Attacker-controlled text must never be interpolated into a shell script.

    The safe pattern — used throughout this repo — is to bind the value to an
    ``env:`` entry and reference it as a quoted shell variable, so the runner
    passes it as data instead of splicing it into the script the shell parses.
    """
    offenders: list[str] = []
    for job_name, job in _jobs(_load(path)).items():
        for index, step in enumerate(_steps(job)):
            script = step.get("run")
            if not isinstance(script, str):
                continue
            match = UNTRUSTED_CONTEXT_RE.search(_strip_comments(script))
            if match:
                label = step.get("name") or f"step[{index}]"
                offenders.append(f"{job_name} / {label}: {match.group(0)}")

    assert offenders == [], (
        f"{path.name}: untrusted expression interpolated into a run: script "
        f"(bind it to env: and quote the variable instead): {offenders}"
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_privileged_pull_request_target_trigger(path: Path) -> None:
    """``pull_request_target`` runs fork code with a writable token and secrets.

    This project has no need for it: PR validation runs on ``pull_request``
    (read-only, no secrets), and the one privileged reaction to a completed
    run is ``pages.yml``'s ``workflow_run``, which is separately guarded to
    same-repository default-branch pushes.
    """
    triggers = _load(path).get(True) or _load(path).get("on") or {}
    if isinstance(triggers, str):
        triggers = {triggers: None}
    assert "pull_request_target" not in triggers, (
        f"{path.name}: pull_request_target exposes a writable token to fork code"
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_workflow_declares_explicit_permissions(path: Path) -> None:
    """An absent ``permissions:`` inherits the repository default.

    That default can be changed org-wide (or was historically read/write), so
    a workflow without an explicit block silently gains whatever the
    organization hands out. Declaring it pins least privilege in the file.
    """
    document = _load(path)
    top_level = document.get("permissions")
    if top_level is not None:
        assert top_level != "write-all", f"{path.name}: write-all defeats least privilege"
        return
    # A workflow may instead scope permissions per job, but then EVERY job
    # must declare its own.
    jobs = _jobs(document)
    missing = [name for name, job in jobs.items() if job.get("permissions") is None]
    assert not missing, (
        f"{path.name}: no top-level permissions: and these jobs declare none: {missing}"
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_checkout_credentials_persist_only_in_release_workflows(path: Path) -> None:
    """A persisted token is reusable by every later step in the job.

    Only the release stages need it (they push a branch and a tag). Anywhere
    else it widens the blast radius of a compromised build step for no
    benefit, so the credential-bearing set stays explicit and small.
    """
    for job_name, job in _jobs(_load(path)).items():
        for step in _steps(job):
            uses = step.get("uses") or ""
            if not uses.startswith("actions/checkout"):
                continue
            persists = (step.get("with") or {}).get("persist-credentials")
            if persists in (True, "true"):
                assert path.name in CREDENTIAL_PERSISTING_WORKFLOWS, (
                    f"{path.name} / {job_name}: unexpected persist-credentials: true "
                    "(set it to false unless this job pushes a ref)"
                )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_all_jobs_run_on_github_hosted_runners(path: Path) -> None:
    """A self-hosted runner executes untrusted PR code on a persistent host.

    Matrix-driven labels are resolved against the job's own matrix so an
    expression cannot hide a self-hosted label behind indirection.
    """
    for job_name, job in _jobs(_load(path)).items():
        runner = job.get("runs-on")
        if runner is None:
            continue
        labels: list[Any] = []
        if isinstance(runner, str) and runner.strip().startswith("${{"):
            # Resolve every value the matrix can supply for this key.
            include = ((job.get("strategy") or {}).get("matrix") or {}).get("include") or []
            key = runner.strip().removeprefix("${{").removesuffix("}}").strip()
            key = key.split(".")[-1]
            labels = [entry.get(key) for entry in include if isinstance(entry, dict)]
            assert labels, f"{path.name} / {job_name}: cannot resolve runs-on {runner}"
        elif isinstance(runner, list):
            labels = runner
        else:
            labels = [runner]

        for label in labels:
            assert label in ALLOWED_RUNNER_LABELS, (
                f"{path.name} / {job_name}: runner {label!r} is not a known "
                f"GitHub-hosted label {sorted(ALLOWED_RUNNER_LABELS)}"
            )


def test_composite_actions_do_not_interpolate_untrusted_input_into_scripts() -> None:
    """Composite actions are the same injection sink as workflow steps.

    An action that splices ``${{ inputs.x }}`` into ``run:`` inherits its
    caller's trust level; every action here binds inputs through ``env:``
    instead, so a caller passing attacker text cannot execute it.
    """
    offenders: list[str] = []
    for path in sorted(ACTION_DIR.glob("*/action.yml")):
        document = _load(path)
        for index, step in enumerate((document.get("runs") or {}).get("steps") or []):
            if not isinstance(step, dict):
                continue
            script = step.get("run")
            if not isinstance(script, str):
                continue
            match = UNTRUSTED_CONTEXT_RE.search(_strip_comments(script))
            if match:
                label = step.get("name") or f"step[{index}]"
                offenders.append(f"{path.parent.name} / {label}: {match.group(0)}")

    assert offenders == [], (
        f"composite action interpolates an input into a run: script "
        f"(bind it to env: instead): {offenders}"
    )


def test_pages_workflow_run_trigger_stays_fenced_to_trusted_code() -> None:
    """``workflow_run`` is privileged: it gets a writable token and secrets.

    pages.yml reacts to Unit Tests completing and then checks out the measured
    commit, so without these guards a fork PR's head could be built and
    published with the default branch's privileges. The guard set must keep
    asserting success, a push event, the same repository, and the default
    branch.
    """
    document = _load(WORKFLOW_DIR / "pages.yml")
    triggers = document.get(True) or document.get("on") or {}
    assert "workflow_run" in triggers

    condition = " ".join(str(job.get("if", "")) for job in _jobs(document).values())
    for required in (
        "workflow_run.conclusion == 'success'",
        "workflow_run.event == 'push'",
        "head_repository.full_name == github.repository",
        "head_branch == github.event.repository.default_branch",
    ):
        assert required in condition, f"pages.yml lost its workflow_run guard: {required}"
