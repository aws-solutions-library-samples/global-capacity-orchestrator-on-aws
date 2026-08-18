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
from collections.abc import Iterator
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

#: A Git commit is the only immutable way to name a third-party action.
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: ``uses: owner/action@<40-hex>`` as it appears on a raw line, with whatever
#: trails the ref captured so the version comment can be checked.
PINNED_USES_LINE_RE = re.compile(
    r"^\s*(?:-\s+)?uses:\s+(?P<ref>[^\s@]+@[0-9a-f]{40})(?P<trailer>.*)$"
)

#: The trailing comment that keeps a SHA-pinned ref readable and gives
#: Dependabot the version it rewrites alongside the SHA.
VERSION_COMMENT_RE = re.compile(r"^\s+#\s*v\d+(?:\.\d+)*\s*$")


def _workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml"))
    assert files, "no workflow files found; did the path move?"
    return files


def _composite_action_files() -> list[Path]:
    files = sorted(ACTION_DIR.glob("*/action.yml"))
    assert files, "no composite actions found; did the path move?"
    return files


def _action_reference_files() -> list[Path]:
    """Every file in which this repo may name an action to run."""
    return _workflow_files() + _composite_action_files()


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _jobs(document: dict[str, Any]) -> dict[str, Any]:
    jobs = document.get("jobs") or {}
    return {name: spec for name, spec in jobs.items() if isinstance(spec, dict)}


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in (job.get("steps") or []) if isinstance(step, dict)]


def _ref_id(path: Path) -> str:
    """Stable pytest id: every composite action file is named ``action.yml``."""
    return path.name if path.parent == WORKFLOW_DIR else f"{path.parent.name}/{path.name}"


def _labelled_steps(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(label, step)`` for a workflow *or* a composite action.

    Workflow steps hang off ``jobs.<id>.steps``; composite action steps off
    ``runs.steps``. Walking both from one helper means a new action gets the
    same scrutiny as a new workflow job without a second traversal to keep in
    sync.
    """
    for job_name, job in _jobs(document).items():
        for index, step in enumerate(_steps(job)):
            yield f"{job_name} / {step.get('name') or f'step[{index}]'}", step
    for index, step in enumerate((document.get("runs") or {}).get("steps") or []):
        if isinstance(step, dict):
            yield f"runs / {step.get('name') or f'step[{index}]'}", step


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


@pytest.mark.parametrize("path", _action_reference_files(), ids=_ref_id)
def test_every_third_party_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    """A tag is a mutable pointer; whoever owns the action can move it.

    ``uses: owner/action@v7`` — or even ``@v7.0.1`` — resolves at run time to
    whatever the tag points at *then*. The action's publisher (or anyone who
    steals their account) can retarget it onto code that reads this repo's
    secrets and the ``GITHUB_TOKEN`` of whichever job runs it, retroactively,
    with no diff here. A 40-character commit SHA is the only ref that cannot
    be repointed, which is why supply-chain guidance for Actions is to pin to
    one. Local ``./.github/actions/*`` refs are exempt: they resolve inside the
    checked-out commit, so they are already as pinned as the workflow itself.
    """
    offenders: list[str] = []
    for label, step in _labelled_steps(_load(path)):
        ref = step.get("uses")
        if not isinstance(ref, str) or ref.startswith("./"):
            continue
        _, separator, version = ref.partition("@")
        if not separator or not COMMIT_SHA_RE.match(version):
            offenders.append(f"{label}: {ref}")

    assert offenders == [], (
        f"{_ref_id(path)}: action ref is not pinned to a 40-character commit SHA "
        f"(resolve the tag with `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha` "
        f"and keep the tag as a trailing `# vX.Y.Z` comment): {offenders}"
    )


@pytest.mark.parametrize("path", _action_reference_files(), ids=_ref_id)
def test_every_pinned_action_records_its_human_readable_version(path: Path) -> None:
    """A bare SHA is unreviewable, so each pin carries its tag in a comment.

    The comment is what makes the pin maintainable rather than merely safe:
    Dependabot recognizes the ``@<sha>  # <tag>`` shape and rewrites *both*
    halves when it bumps an action, and a reviewer can tell v7.0.1 from v6
    without resolving hashes by hand. Losing the comment turns future
    dependency diffs into forty opaque characters.

    Line-based on purpose — comments do not survive YAML parsing — so it also
    asserts it saw every ref the structural walk found, meaning a pin cannot
    hide from this check behind unusual formatting.
    """
    text = path.read_text(encoding="utf-8")
    missing: list[str] = []
    seen = 0
    for number, line in enumerate(text.splitlines(), start=1):
        match = PINNED_USES_LINE_RE.match(line)
        if not match:
            continue
        seen += 1
        if not VERSION_COMMENT_RE.match(match.group("trailer")):
            missing.append(f"line {number}: {match.group('ref')}")

    assert missing == [], (
        f"{_ref_id(path)}: SHA-pinned action is missing its trailing "
        f"`  # vX.Y.Z` version comment: {missing}"
    )

    structural = sum(
        1
        for _, step in _labelled_steps(_load(path))
        if isinstance(step.get("uses"), str) and COMMIT_SHA_RE.match(step["uses"].partition("@")[2])
    )
    assert seen == structural, (
        f"{_ref_id(path)}: found {seen} pinned ref(s) by line but {structural} by "
        "structure; a ref is formatted so this check cannot see it"
    )


def test_dependabot_covers_composite_actions_that_pin_third_party_actions() -> None:
    """SHA pins only stay safe if something keeps bumping them.

    For the ``github-actions`` ecosystem a ``directory`` of ``/`` scans
    ``.github/workflows`` plus an ``action.yml`` at the *repository root* — it
    does not walk ``.github/actions/*/action.yml``. Without a second entry those
    composite pins freeze at whatever SHA they were introduced with, quietly
    accumulating the very CVEs pinning was supposed to let us adopt on our own
    schedule. A glob only works under the plural ``directories`` key.
    """
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in (config.get("updates") or [])
        if isinstance(entry, dict) and entry.get("package-ecosystem") == "github-actions"
    ]
    assert entries, "dependabot.yml no longer tracks the github-actions ecosystem"

    locations = {
        location
        for entry in entries
        for location in (
            [entry["directory"]] if entry.get("directory") else entry.get("directories") or []
        )
    }
    assert "/" in locations, "dependabot lost the '/' entry that scans .github/workflows"

    needs_coverage = {
        path.parent.name
        for path in _composite_action_files()
        for _, step in _labelled_steps(_load(path))
        if isinstance(step.get("uses"), str) and not step["uses"].startswith("./")
    }
    uncovered = sorted(
        name
        for name in needs_coverage
        if not any(
            location == f"/.github/actions/{name}"
            or (location.endswith("*") and f"/.github/actions/{name}".startswith(location[:-1]))
            for location in locations
        )
    )
    assert uncovered == [], (
        "these composite actions pin a third-party action that Dependabot never "
        f"sees; add their directory to .github/dependabot.yml: {uncovered}"
    )
