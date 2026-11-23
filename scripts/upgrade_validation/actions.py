"""The upgrade-validation actions.

Six are new: ``preflight`` (the release preflight plus the base release),
``prepare`` (the private workspace), ``deploy`` (the base release, deployed by
its own ``gco``), ``sentinels`` and ``upgrade`` (the base release's ``gco
upgrade`` to this commit), and ``verify-upgrade``. ``topology`` runs the
release harness's check against the cdk.json the upgraded deployment was
synthesized from. The rest of the registry reuses the release harness's
handlers unchanged.

``deploy`` and ``upgrade`` each run one long ``gco`` subprocess inside a
*phase*. The phase checkpoints the AWS server time before the command starts,
and whatever the command did, even if it failed or timed out, the stacks it
left are adopted by run tag against that time before the action returns. A
phase interrupted by the harness itself is adopted the same way on resume and
is never run again: the run fails, and teardown removes what it left.
"""

from __future__ import annotations

import copy
import email.utils
import json
import os
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.live_release_validation.actions import action_preflight, action_topology
from scripts.live_release_validation.cleanup.local_images import (
    prune_local_cdk_asset_images_safely,
)
from scripts.live_release_validation.constants import _HEALTHY_STACK_STATUSES, _RUN_STACK_TAG
from scripts.live_release_validation.context import _run_git
from scripts.live_release_validation.inventory import describe_stack
from scripts.live_release_validation.models import ActionFailure, RunContext, utc_now
from scripts.live_release_validation.ownership.ecr import (
    PRIOR_RELEASE_ECR_IMAGES_KEY,
    _checkpointed_ecr_image_targets,
    _expected_ecr_images,
)
from scripts.live_release_validation.ownership.kms import _checkpoint_retained_kms_keys
from scripts.live_release_validation.ownership.stacks import (
    _adopt_run_tagged_stacks,
    _owned_stack_record,
    _reconcile_stack_ownership,
)

from .models import UpgradeRunSettings
from .sentinel import create_sentinel, delete_sentinel, verify_sentinel
from .workspace import (
    CommandResult,
    Workspace,
    build_mirror,
    checkout_head,
    clone_base,
    command_environment,
    console_echo,
    gco_result_document,
    read_cloud_assembly,
    reset_workspace,
    run_cdk_context,
    run_logged,
    running_command,
    sha256_file,
    synthetic_release_tag,
    tag_candidate,
    tracked_changes,
    write_run_context,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-19T06:05:34Z
# Generated from Git commit: 1e6de1636d5418ba50126d6619a9bafc70839dad
# Flowchart(s) generated from this file:
#   * ``action_upgrade`` -> ``diagrams/code_diagrams/scripts/upgrade_validation/actions.action_upgrade.html``
#     (PNG: ``diagrams/code_diagrams/scripts/upgrade_validation/actions.action_upgrade.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


#: ``checkpoint.state`` key for everything this harness records.
STATE_KEY = "upgrade_validation"
#: Wall-clock cap for each preparation step and for ``gco upgrade --check``.
STEP_TIMEOUT_SECONDS = 30 * 60

#: The CDK app reads the context the CLI would pass it from this file, and
#: writes its cloud assembly to ``CDK_OUTDIR`` (``aws_cdk.cx_api``).
CDK_CONTEXT_FILE_ENV = "CONTEXT_OVERFLOW_LOCATION_ENV"
CDK_OUTDIR_ENV = "CDK_OUTDIR"
_GIB = float(1024**3)


def _settings(ctx: RunContext) -> UpgradeRunSettings:
    settings = ctx.settings
    if not isinstance(settings, UpgradeRunSettings):
        raise TypeError("The upgrade-validation actions need UpgradeRunSettings")
    return settings


def _state(ctx: RunContext) -> dict[str, Any]:
    state = ctx.checkpoint.state.setdefault(STATE_KEY, {})
    if not isinstance(state, dict):
        raise RuntimeError(f"Checkpoint {STATE_KEY} must be an object")
    return state


def _phases(ctx: RunContext) -> dict[str, dict[str, Any]]:
    phases = _state(ctx).setdefault("phases", {})
    if not isinstance(phases, dict) or any(not isinstance(item, dict) for item in phases.values()):
        raise RuntimeError(f"Checkpoint {STATE_KEY}.phases is malformed")
    return phases


def _workspace(ctx: RunContext) -> Workspace:
    return Workspace(_settings(ctx).workspace_dir)


def _gco_identity(ctx: RunContext) -> dict[str, str]:
    """``GCO_*`` settings that point the base ``gco`` at the harness's deployment."""
    config = ctx.config
    return {
        "GCO_PROJECT_NAME": str(config.project_name),
        "GCO_GLOBAL_REGION": str(config.global_region),
        "GCO_API_GATEWAY_REGION": str(config.api_gateway_region),
        "GCO_MONITORING_REGION": str(config.monitoring_region),
        "GCO_DEFAULT_REGION": str(config.default_region),
    }


def _environment(ctx: RunContext) -> dict[str, str]:
    return command_environment(_workspace(ctx), identity=_gco_identity(ctx))


def _server_time(ctx: RunContext) -> datetime:
    """AWS's clock, from an STS response, after proving the caller's account."""
    response = ctx.session.client("sts", region_name=ctx.config.global_region).get_caller_identity()
    account = str(response.get("Account") or "")
    if account != ctx.settings.expected_account:
        raise RuntimeError(
            f"AWS caller account {account or 'unknown'} does not match expected account "
            f"{ctx.settings.expected_account}"
        )
    headers = (response.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    if not headers.get("date"):
        raise RuntimeError("STS returned no Date header to start the adoption window from")
    moment = email.utils.parsedate_to_datetime(str(headers["date"]))
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _run_step(
    ctx: RunContext,
    label: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    phase: str | None = None,
) -> CommandResult:
    """Run one workspace command; a phase's command has its process ID checkpointed."""
    workspace = _workspace(ctx)

    def record_pid(pid: int) -> None:
        if phase is not None:
            _phases(ctx)[phase]["pid"] = pid
            ctx.persist()

    return run_logged(
        argv,
        cwd=cwd,
        env=_environment(ctx),
        log_path=workspace.logs / f"{label.replace(' ', '-')}.log",
        timeout_seconds=timeout_seconds,
        echo=console_echo(label),
        on_start=record_pid,
    )


# ─── Phases ─────────────────────────────────────────────────────────


def _begin_phase(ctx: RunContext, name: str, **details: Any) -> dict[str, Any]:
    """Checkpoint a phase's start, and its adoption window, before its command runs."""
    window = _server_time(ctx)
    phase = {"started_at": utc_now(), "window_started_at": window.isoformat(), **details}
    with ctx.state_lock:
        _phases(ctx)[name] = phase
        if name == "deploy":
            ctx.checkpoint.deployment_attempted = True
            ctx.checkpoint.destroyed = False
    ctx.persist()
    return phase


def _replaceable(name: str, phase: dict[str, Any]) -> tuple[str, ...]:
    """The stacks a phase may recreate: the upgrade's workload tier, nothing else."""
    if name != "upgrade":
        return ()
    return tuple((phase.get("plan") or {}).get("workload_stacks") or ())


def _adopt_phase(ctx: RunContext, name: str) -> dict[str, Any]:
    phase = _phases(ctx)[name]
    return _adopt_run_tagged_stacks(
        ctx,
        phase=name,
        window_started_at=datetime.fromisoformat(str(phase["window_started_at"])),
        replaceable=_replaceable(name, phase),
    )


def _settle_phase(ctx: RunContext, name: str) -> None:
    """Take ownership of what a phase's command left, whatever its outcome."""
    phase = _phases(ctx)[name]
    phase["adoption"] = _adopt_phase(ctx, name)
    _reconcile_stack_ownership(ctx)
    # Retained resources (the EKS keys, their log groups) are only checkpointed
    # while their stack stands; the upgrade destroys the base generation.
    phase["owned_kms_keys"] = len(_checkpoint_retained_kms_keys(ctx))
    phase["local_image_prune"] = prune_local_cdk_asset_images_safely()
    phase["finished_at"] = utc_now()
    ctx.persist()


def _recover_interrupted_phases(ctx: RunContext) -> list[str]:
    """Adopt what a phase the harness itself was interrupted in left behind.

    A harness killed outright (``kill -9``) cannot stop its command, which then
    keeps deploying on its own. Nothing is adopted while it still runs.
    """
    gco = str(_workspace(ctx).gco)
    recovered: list[str] = []
    for name, phase in _phases(ctx).items():
        if phase.get("finished_at"):
            continue
        pid = phase.get("pid")
        command = running_command(pid) if isinstance(pid, int) else None
        if command is not None and gco in command:
            raise RuntimeError(
                f"The {name} phase's command is still running (process {pid}: {command}); "
                f"let it finish or stop it with 'kill -TERM -{pid}', then resume"
            )
        phase["adoption"] = _adopt_phase(ctx, name)
        phase["interrupted"] = True
        phase["finished_at"] = utc_now()
        ctx.persist()
        recovered.append(name)
    return recovered


def _phase_outcome(phase: dict[str, Any], *, action: str, command: str) -> None:
    """Fail the action unless its phase's command finished and succeeded."""
    details = {"phase": copy.deepcopy(phase)}
    if phase.get("interrupted"):
        raise ActionFailure(
            f"{command} was interrupted before it finished; the {action} action never "
            "reruns it, so this run can only be torn down",
            details,
        )
    result = phase.get("command") or {}
    if result.get("timed_out"):
        raise ActionFailure(f"{command} timed out; see {result.get('log')}", details)
    if result.get("exit_code") != 0:
        raise ActionFailure(
            f"{command} exited with {result.get('exit_code')}; see {result.get('log')}", details
        )


# ─── Checks shared by the actions ───────────────────────────────────


def _target_regions(ctx: RunContext) -> dict[str, str]:
    targets = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(targets, dict) or not targets:
        raise RuntimeError("Checkpoint lacks target stack Regions")
    return {str(name): str(region) for name, region in targets.items()}


def _require_prepared_workspace(ctx: RunContext) -> dict[str, Any]:
    """The prepared base checkout, still at the base release and unmodified."""
    settings = _settings(ctx)
    workspace = _workspace(ctx)
    prepared = _state(ctx).get("workspace")
    if not isinstance(prepared, dict) or not prepared.get("prepared_at"):
        raise RuntimeError("The prepare action has not completed")
    if not workspace.owned_by(settings.run_id) or not workspace.gco.is_file():
        raise RuntimeError(f"The prepared workspace {workspace.root} is missing or incomplete")
    head = checkout_head(workspace)
    if head != settings.base_commit:
        raise RuntimeError(f"The base checkout is at {head}, not {settings.base_ref}")
    changes = tracked_changes(workspace)
    if changes != ["cdk.json"]:
        raise RuntimeError(
            "The base checkout's tracked files changed beyond the run's cdk.json: "
            + ", ".join(changes or ["none (cdk.json was reverted)"])
        )
    if sha256_file(workspace.clone / "cdk.json") != prepared["cdk_json"]["sha256"]:
        raise RuntimeError("The base checkout's cdk.json changed after the run wrote it")
    return prepared


def _require_mirror_repositories_in_baseline(
    ctx: RunContext, baseline: dict[str, Any]
) -> dict[str, Any]:
    """Every repository either release's image mirror copies into must predate the run.

    The mirror runs inside the base ``gco`` and, after the upgrade, inside the
    candidate's, with no creation callback into this harness: a repository it
    created would have no ownership record, so final-inventory could neither
    accept nor remove it. Copying into a repository the baseline already holds
    is covered the way a release run covers it: a tag the baseline lacked is
    a retained image delta.
    """
    present = {
        (str(region), str(repository.get("name") or ""))
        for region, repositories in (baseline.get("ecr_repositories") or {}).items()
        for repository in repositories
    }
    wanted = sorted(
        {
            (str(item["region"]), str(item["repository"]))
            for item in _checkpointed_ecr_image_targets(ctx)
            if any(source.get("kind") == "configured-mirror" for source in item["sources"])
        }
    )
    missing = [f"{region}:{name}" for region, name in wanted if (region, name) not in present]
    if missing:
        raise RuntimeError(
            "The image mirror would create ECR repositories outside this run's ownership "
            f"records: {', '.join(missing)}. Seed them with 'gco images mirror --region "
            "<region>' first (a release validation run leaves them in place), then rerun"
        )
    return {"repositories": [f"{region}:{name}" for region, name in wanted]}


def _stack_generations(ctx: RunContext) -> dict[str, str]:
    """The stack ID this run owns under each target name now."""
    return {
        name: str((_owned_stack_record(ctx, region, name) or {}).get("stack_id") or "")
        for name, region in sorted(_target_regions(ctx).items())
    }


def _require_healthy_targets(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """Every target stands, under the ID this run owns, in a completed state."""
    stacks: dict[str, dict[str, Any]] = {}
    unhealthy: list[str] = []
    for name, region in sorted(_target_regions(ctx).items()):
        record = _owned_stack_record(ctx, region, name)
        live = describe_stack(ctx.session, region, name)
        status = str((live or {}).get("status") or "absent")
        stacks[name] = {
            "region": region,
            "stack_id": (record or {}).get("stack_id"),
            "status": status,
        }
        if (
            record is None
            or live is None
            or live.get("stack_id") != record.get("stack_id")
            or status not in _HEALTHY_STACK_STATUSES
        ):
            unhealthy.append(f"{name} ({status})")
    if unhealthy:
        raise ActionFailure(
            "Target stacks are not deployed and owned by this run: " + ", ".join(unhealthy),
            {"stacks": stacks},
        )
    return stacks


# ─── preflight ──────────────────────────────────────────────────────


def _verify_base_release(ctx: RunContext) -> dict[str, Any]:
    """The base tag still names the pinned commit, and deploys this checkout's topology."""
    settings = _settings(ctx)
    root = settings.repo_root
    commit = _run_git(
        root,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/tags/{settings.base_ref}^{{commit}}",
        check=False,
    )
    if commit != settings.base_commit:
        raise RuntimeError(
            f"{settings.base_ref} names {commit or 'no commit'} in this repository, not "
            f"{settings.base_commit}"
        )
    if _run_git(root, "merge-base", settings.base_commit, settings.expected_sha) != commit:
        raise RuntimeError(
            f"{settings.expected_sha} does not descend from {settings.base_ref}; the upgrade "
            "path this harness validates starts from a release in the checkout's history"
        )
    try:
        base_config = json.loads(_run_git(root, "show", f"{commit}:cdk.json"))
    except ValueError as exc:
        raise RuntimeError(f"{settings.base_ref}'s cdk.json is not valid JSON: {exc}") from exc
    base_context = base_config.get("context") if isinstance(base_config, dict) else None
    if not isinstance(base_context, dict):
        raise RuntimeError(f"{settings.base_ref}'s cdk.json has no context object")
    differing = [
        key
        for key in ("project_name", "deployment_regions")
        if base_context.get(key) != ctx.cdk_context.get(key)
    ]
    if differing:
        raise RuntimeError(
            f"{settings.base_ref} deploys a different {' and '.join(differing)} than this "
            "checkout; the teardown and inventory would not cover what it deploys"
        )
    return {
        "ref": settings.base_ref,
        "commit": commit,
        "project_name": base_context["project_name"],
        "deployment_regions": base_context["deployment_regions"],
        # Checked against the baseline before the deploy phase begins.
        "image_mirror": bool((base_context.get("volcano_image_mirror") or {}).get("enabled")),
    }


def _base_run_context(ctx: RunContext) -> dict[str, Any]:
    """The context the base checkout's cdk.json carries once ``prepare`` has written it.

    That is the base release's cdk.json context with the run tag and the
    run-scoped context added (:func:`run_cdk_context`, the merge
    ``write_run_context`` applies), read from the repository rather than the
    clone so the preflight has it before ``prepare`` builds the clone.
    """
    settings = _settings(ctx)
    document = json.loads(_run_git(settings.repo_root, "show", f"{settings.base_commit}:cdk.json"))
    context = document.get("context") if isinstance(document, dict) else None
    if not isinstance(context, dict):
        raise RuntimeError(f"{settings.base_ref}'s cdk.json has no context object")
    return run_cdk_context(
        context,
        run_tag_key=_RUN_STACK_TAG,
        run_id=settings.run_id,
        context=settings.base_cdk_context(),
        source=f"{settings.base_ref}'s cdk.json",
    )


def _candidate_stacks_under_base_config(ctx: RunContext) -> list[str]:
    """This checkout's stacks as ``gco upgrade`` deploys them: under the base's cdk.json.

    The upgrade keeps the base checkout's cdk.json byte for byte, so it deploys
    this checkout's app with that file's context, not with this checkout's own
    cdk.json. A block a release adds and turns on only in its own cdk.json
    (``regional_persistent_bucket``) is absent from the base's, and the stacks
    it adds are not part of the upgrade. The app runs as the CDK CLI runs it,
    with that context from a file and its cloud assembly written to this
    checkout's ``cdk.out``, which the release preflight reads the expected
    images from and :func:`_verify_candidate_stack_tags` the stack tags.
    """
    settings = _settings(ctx)
    root = settings.repo_root
    context_file = settings.report_dir / "base-cdk-context.json"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(json.dumps(_base_run_context(ctx), indent=2), encoding="utf-8")
    ctx.stack_manager._ensure_lambda_build()
    outdir = root / "cdk.out"
    shutil.rmtree(outdir, ignore_errors=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CDK_CONTEXT_JSON", "CDK_DEFAULT_ACCOUNT", "CDK_DEFAULT_REGION"}
    }
    environment.update({CDK_CONTEXT_FILE_ENV: str(context_file), CDK_OUTDIR_ENV: str(outdir)})
    result = run_logged(
        [sys.executable, "app.py"],
        cwd=root,
        env=environment,
        log_path=settings.report_dir / "candidate-synth-under-base-cdk-json.log",
        timeout_seconds=STEP_TIMEOUT_SECONDS,
        echo=console_echo("candidate synth"),
    )
    if not result.ok:
        raise RuntimeError(
            f"Synthesizing this checkout under {settings.base_ref}'s cdk.json failed "
            f"(exit {result.exit_code}); see {result.log_path}"
        )
    return sorted(read_cloud_assembly(root))


def _verify_candidate_stack_tags(ctx: RunContext) -> dict[str, Any]:
    """This checkout's app applies cdk.json ``context.tags`` to every target stack.

    The upgrade's redeploy passes no ``--tags``: the run tag reaches the stacks
    it recreates only through cdk.json, and a stack without it could never be
    adopted or destroyed by the run. The preflight has just synthesized this
    checkout under the base checkout's cdk.json
    (:func:`_candidate_stacks_under_base_config`), so its assembly shows
    whether every stack the upgrade deploys carries that file's tags,
    the run tag among them.
    """
    expected: dict[str, Any] = _base_run_context(ctx)["tags"]
    assembly = read_cloud_assembly(_settings(ctx).repo_root)
    untagged = sorted(
        name
        for name in _target_regions(ctx)
        if any(
            (assembly.get(name) or {}).get("tags", {}).get(str(key)) != str(value)
            for key, value in expected.items()
        )
    )
    if untagged:
        raise RuntimeError(
            "This checkout's app does not apply cdk.json context.tags to "
            + ", ".join(untagged)
            + ", so the stacks the upgrade recreates would lack the run tag"
        )
    return {"cdk_json_tags": sorted(str(key) for key in expected), "stacks": len(assembly)}


def _required_tools() -> dict[str, str]:
    tools = {name: shutil.which(name) for name in ("git", "node", "npm")}
    missing = sorted(name for name, path in tools.items() if not path)
    if missing:
        raise RuntimeError(
            "Preparing the base release needs " + ", ".join(missing) + " on PATH "
            "(git for the private clone, node and npm for its CDK CLI)"
        )
    return {name: str(path) for name, path in tools.items()}


def _check_workspace_disk(settings: UpgradeRunSettings) -> float | str:
    """The workspace volume must clear the same free-space floor as the checkout."""
    if settings.min_free_disk_gib <= 0:
        return "not-required"
    probe = settings.workspace_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free_gib = shutil.disk_usage(probe).free / _GIB
    if free_gib < settings.min_free_disk_gib:
        raise RuntimeError(
            f"The workspace volume ({probe}) has {free_gib:.1f} GiB free, below the "
            f"{settings.min_free_disk_gib} GiB floor; the base clone, its venv, and its "
            "image builds need the room"
        )
    return round(free_gib, 2)


def action_upgrade_preflight(ctx: RunContext) -> dict[str, Any]:
    """Run the release preflight, then pin the base release, the tools, and the workspace."""
    settings = _settings(ctx)
    recovered: list[str] = []
    if ctx.checkpoint.deployment_attempted:
        # A phase the harness was interrupted in left stacks it has not adopted
        # yet; the release preflight's ownership reconcile would refuse them.
        _server_time(ctx)
        recovered = _recover_interrupted_phases(ctx)
    # The base release is pinned first: the targets are synthesized from its cdk.json.
    base = _verify_base_release(ctx)
    details = action_preflight(
        ctx, list_target_stacks=lambda: _candidate_stacks_under_base_config(ctx)
    )
    candidate_tags = _verify_candidate_stack_tags(ctx)
    state = _state(ctx)
    if state.get("base") not in (None, base):
        raise RuntimeError("The base release changed since the checkpoint was created")
    state["base"] = base
    ctx.persist()
    preparing = "prepare" in set(ctx.report.selected_actions)
    return {
        **details,
        "base_release": base,
        "candidate_stack_tags": candidate_tags,
        "synthetic_tag": synthetic_release_tag(settings.base_ref),
        "tools": _required_tools() if preparing else "not-required",
        "workspace": str(settings.workspace_dir),
        "workspace_free_disk_gib": _check_workspace_disk(settings) if preparing else None,
        "recovered_phases": recovered,
    }


# ─── prepare ────────────────────────────────────────────────────────


def _prepare_commands(workspace: Workspace) -> tuple[tuple[str, list[str], Path], ...]:
    clone = workspace.clone
    return (
        ("prepare venv", [sys.executable, "-m", "venv", str(workspace.venv)], workspace.root),
        (
            "prepare pip",
            [
                str(workspace.python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-input",
                "--constraint",
                str(clone / "requirements-lock.txt"),
                "--editable",
                f"{clone}[cdk]",
            ],
            clone,
        ),
        ("prepare npm", ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], clone),
        ("prepare synth", [str(workspace.gco), "stacks", "synth"], clone),
    )


def action_prepare(ctx: RunContext) -> dict[str, Any]:
    """Clone the base release privately, install its gco, and synthesize its app."""
    settings = _settings(ctx)
    state = _state(ctx)
    if isinstance(state.get("workspace"), dict) and state["workspace"].get("prepared_at"):
        return {"reused": True, **_require_prepared_workspace(ctx)}
    workspace = _workspace(ctx)
    reset_workspace(workspace, settings.run_id)
    mirror = build_mirror(
        workspace,
        source=settings.repo_root,
        branch=settings.expected_branch,
        candidate_sha=settings.expected_sha,
        base_ref=settings.base_ref,
        base_commit=settings.base_commit,
    )
    clone = clone_base(workspace, base_ref=settings.base_ref, base_commit=settings.base_commit)
    cdk_json = write_run_context(
        workspace.clone,
        run_tag_key=_RUN_STACK_TAG,
        run_id=settings.run_id,
        context=settings.base_cdk_context(),
    )
    steps: list[dict[str, Any]] = []
    for label, argv, cwd in _prepare_commands(workspace):
        result = _run_step(ctx, label, argv, cwd=cwd, timeout_seconds=STEP_TIMEOUT_SECONDS)
        steps.append({"step": label, **result.to_dict()})
        if not result.ok:
            raise ActionFailure(
                f"Preparing {settings.base_ref} failed at '{label}' "
                f"(exit {result.exit_code}); see {result.log_path}",
                {"steps": steps},
            )

    assembly = read_cloud_assembly(workspace.clone)
    evidence: dict[str, Any] = {"steps": steps, "assembly": assembly}
    targets = sorted(_target_regions(ctx))
    if sorted(assembly) != targets:
        raise ActionFailure(
            f"{settings.base_ref} synthesizes {sorted(assembly)}, but this checkout "
            f"synthesizes {targets} under the same cdk.json; a stack renamed or added "
            "between the releases would outlive teardown",
            evidence,
        )
    untagged = sorted(
        name
        for name, stack in assembly.items()
        if stack["tags"].get(_RUN_STACK_TAG) != settings.run_id
    )
    if untagged:
        raise ActionFailure(
            f"{settings.base_ref} would deploy {', '.join(untagged)} without the run tag, so "
            "the run could not prove it owns them",
            evidence,
        )
    changes = tracked_changes(workspace)
    if changes != ["cdk.json"]:
        raise ActionFailure(
            "Preparing the base checkout changed tracked files beyond cdk.json, which "
            "gco upgrade refuses: " + ", ".join(changes),
            evidence,
        )
    prior_images = _expected_ecr_images(ctx, targets, root=workspace.clone)
    record = {
        "root": str(workspace.root),
        "mirror": mirror,
        "clone": clone,
        "cdk_json": cdk_json,
        "synthetic_tag": synthetic_release_tag(settings.base_ref),
        "stacks": targets,
        "steps": steps,
        "prepared_at": utc_now(),
    }
    with ctx.state_lock:
        ctx.checkpoint.state[PRIOR_RELEASE_ECR_IMAGES_KEY] = prior_images
        state["workspace"] = record
    ctx.persist()
    return {**record, "prior_release_ecr_images": prior_images}


# ─── deploy ─────────────────────────────────────────────────────────


def action_base_deploy(ctx: RunContext) -> dict[str, Any]:
    """Deploy the base release with its own gco, then adopt its stacks by run tag."""
    if ctx.checkpoint.baseline is None:
        raise RuntimeError("A protected-resource baseline is required before deployment")
    settings = _settings(ctx)
    workspace = _workspace(ctx)
    phase = _phases(ctx).get("deploy")
    if phase is None:
        _require_prepared_workspace(ctx)
        mirror = _require_mirror_repositories_in_baseline(ctx, ctx.checkpoint.baseline)
        phase = _begin_phase(ctx, "deploy", image_mirror=mirror)
        try:
            # No --tag: the run tag comes from cdk.json, exactly as it must for
            # the upgrade's redeploy, so adopting these stacks also proves it.
            result = _run_step(
                ctx,
                "base deploy",
                [str(workspace.gco), "stacks", "deploy-all", "--yes"],
                cwd=workspace.clone,
                timeout_seconds=settings.deploy_timeout_seconds,
                phase="deploy",
            )
            phase["command"] = result.to_dict()
        finally:
            _settle_phase(ctx, "deploy")
    else:
        _reconcile_stack_ownership(ctx)
    _phase_outcome(phase, action="deploy", command=f"{settings.base_ref}'s gco stacks deploy-all")
    return {
        "base_release": _state(ctx).get("base"),
        "phase": copy.deepcopy(phase),
        "stacks": _require_healthy_targets(ctx),
    }


# ─── sentinels ──────────────────────────────────────────────────────


def action_sentinels(ctx: RunContext) -> dict[str, Any]:
    """Write a job template through the base release's API for the upgrade to preserve."""
    template = create_sentinel(ctx, timeout_seconds=_settings(ctx).api_ready_timeout_seconds)
    state = _state(ctx)
    state["sentinel"] = template
    ctx.persist()
    return {"template": copy.deepcopy(template)}


# ─── upgrade ────────────────────────────────────────────────────────


def _validated_plan(ctx: RunContext, check: CommandResult, tag: str) -> dict[str, Any]:
    """``gco upgrade --check`` must describe exactly the upgrade this run validates."""
    document = gco_result_document(check.stdout) or {}
    plan = document.get("plan")
    details = {"check": check.to_dict(), "document": document}
    if not check.ok or not isinstance(plan, dict):
        raise ActionFailure("gco upgrade --check did not return a plan", details)
    control = [str(name) for name in plan.get("control_plane_stacks") or []]
    workload = [str(name) for name in plan.get("workload_stacks") or []]
    problems: list[str] = []
    if document.get("up_to_date") or plan.get("already_at_target"):
        problems.append("the base checkout is already at the target")
    if plan.get("target") != tag:
        problems.append(f"the target is {plan.get('target')!r}, not {tag}")
    if (plan.get("install") or {}).get("cli_editable_from_checkout") is not True:
        problems.append("the base gco is not the editable install of its checkout")
    if (
        not workload
        or set(control) & set(workload)
        or {*control, *workload} != set(_target_regions(ctx))
    ):
        problems.append("its control-plane and workload stacks do not partition the targets")
    if problems:
        raise ActionFailure(
            "gco upgrade --check does not describe this run's upgrade: " + "; ".join(problems),
            details,
        )
    return {"target": tag, "control_plane_stacks": control, "workload_stacks": workload}


def action_upgrade(ctx: RunContext) -> dict[str, Any]:
    """Run the base release's gco upgrade to this commit and adopt the recreated stacks."""
    settings = _settings(ctx)
    workspace = _workspace(ctx)
    phase = _phases(ctx).get("upgrade")
    if phase is None:
        prepared = _require_prepared_workspace(ctx)
        tag = tag_candidate(
            workspace, tag=str(prepared["synthetic_tag"]), candidate_sha=settings.expected_sha
        )
        check = _run_step(
            ctx,
            "upgrade check",
            [str(workspace.gco), "-o", "json", "upgrade", "--check", "--ref", tag["tag"]],
            cwd=workspace.clone,
            timeout_seconds=STEP_TIMEOUT_SECONDS,
        )
        plan = _validated_plan(ctx, check, tag["tag"])
        # Checkpoint the base generation's retained resources while it stands.
        _reconcile_stack_ownership(ctx)
        _checkpoint_retained_kms_keys(ctx)
        phase = _begin_phase(
            ctx,
            "upgrade",
            tag=tag,
            check=check.to_dict(),
            plan=plan,
            before=_stack_generations(ctx),
        )
        try:
            result = _run_step(
                ctx,
                "upgrade",
                [
                    str(workspace.gco),
                    "-o",
                    "json",
                    "upgrade",
                    "--yes",
                    "--ref",
                    tag["tag"],
                    "--skip-container",
                ],
                cwd=workspace.clone,
                timeout_seconds=settings.upgrade_timeout_seconds,
                phase="upgrade",
            )
            phase["command"] = result.to_dict()
            phase["document"] = gco_result_document(result.stdout)
        finally:
            _settle_phase(ctx, "upgrade")
    else:
        _reconcile_stack_ownership(ctx)
    _phase_outcome(phase, action="upgrade", command=f"{settings.base_ref}'s gco upgrade")
    document = phase.get("document") or {}
    if document.get("status") != "ok":
        raise ActionFailure(
            f"gco upgrade reported {document.get('status')!r}, not 'ok'",
            {"phase": copy.deepcopy(phase)},
        )
    return {"phase": copy.deepcopy(phase)}


# ─── topology ───────────────────────────────────────────────────────


def _upgraded_cdk_context(ctx: RunContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """The cdk.json context the upgraded deployment was synthesized from, with its evidence.

    ``gco upgrade`` keeps the base checkout's cdk.json byte for byte across its
    checkout, so the redeploy reads the base release's settings plus the run's,
    not this checkout's cdk.json. The topology check compares the deployment
    with some of them (``kubernetes_version``): read from this checkout, a
    release that changes one would be checked against a value the upgraded
    deployment never used. The file is read as it is now, which
    is what the redeploy read; ``verify-upgrade`` fails the run if it is not
    the one the run wrote.
    """
    path = _workspace(ctx).clone / "cdk.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not read the upgraded checkout's cdk.json {path}: {exc}"
        ) from exc
    context = document.get("context") if isinstance(document, dict) else None
    if not isinstance(context, dict):
        raise RuntimeError(f"The upgraded checkout's cdk.json {path} has no context object")
    differing = [
        key
        for key in ("project_name", "deployment_regions")
        if context.get(key) != ctx.cdk_context.get(key)
    ]
    if differing:
        raise RuntimeError(
            f"The upgraded checkout's cdk.json deploys a different {' and '.join(differing)} "
            "than this checkout; the topology check and the teardown would not cover it"
        )
    digest = sha256_file(path)
    written = ((_state(ctx).get("workspace") or {}).get("cdk_json") or {}).get("sha256")
    return context, {"path": str(path), "sha256": digest, "preserved": digest == written}


def action_upgraded_topology(ctx: RunContext) -> dict[str, Any]:
    """Run the release topology check against the cdk.json the upgrade redeployed from."""
    context, source = _upgraded_cdk_context(ctx)
    details = action_topology(copy.replace(ctx, cdk_context=context))
    return {**details, "cdk_json": source}


# ─── verify-upgrade ─────────────────────────────────────────────────


def _verify_generations(ctx: RunContext, phase: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Control-plane stacks kept their IDs; each workload stack is a new generation."""
    before = phase.get("before") or {}
    plan = phase.get("plan") or {}
    now = _stack_generations(ctx)
    problems: list[str] = []
    for name in plan.get("control_plane_stacks") or []:
        if now.get(name) != before.get(name):
            problems.append(f"{name} was replaced; the upgrade updates the control plane in place")
    for name in plan.get("workload_stacks") or []:
        region = _target_regions(ctx)[name]
        replaced = (_owned_stack_record(ctx, region, name) or {}).get("replaced_generations") or []
        if (
            now.get(name) == before.get(name)
            or not replaced
            or replaced[-1].get("stack_id") != before.get(name)
        ):
            problems.append(f"{name} was not recreated from its base generation")
    return {"before": before, "after": now}, problems


def _verify_document(phase: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """``gco upgrade``'s own account of its checkout and stack cycle."""
    document = phase.get("document") or {}
    plan = phase.get("plan") or {}
    steps = document.get("steps") or {}
    cycle = steps.get("stacks") or {}
    problems: list[str] = []
    if (document.get("plan") or {}).get("target") != plan.get("target"):
        problems.append("the upgrade document names another target")
    if (steps.get("checkout") or {}).get("checked_out") != plan.get("target"):
        problems.append("the upgrade did not check out the target")
    if (steps.get("python") or {}).get("status") != "ok":
        problems.append("the upgrade did not refresh the editable install")
    if cycle.get("ok") is not True or set(cycle.get("destroyed") or []) != set(
        plan.get("workload_stacks") or []
    ):
        problems.append("the stack cycle did not destroy exactly the workload tier")
    return {"steps": copy.deepcopy(steps)}, problems


def action_verify_upgrade(ctx: RunContext) -> dict[str, Any]:
    """Verify the upgraded checkout, the stack generations, and the sentinel."""
    settings = _settings(ctx)
    workspace = _workspace(ctx)
    state = _state(ctx)
    phase = _phases(ctx).get("upgrade")
    if phase is None or not phase.get("finished_at"):
        raise RuntimeError("The upgrade action has not run")
    prepared = state.get("workspace") or {}
    problems: list[str] = []
    head = checkout_head(workspace)
    changes = tracked_changes(workspace)
    cdk_json_sha256 = sha256_file(workspace.clone / "cdk.json")
    evidence: dict[str, Any] = {
        "checkout": {
            "head": head,
            "tracked_changes": changes,
            "cdk_json_sha256": cdk_json_sha256,
        }
    }
    if head != settings.expected_sha:
        problems.append(f"the base checkout is at {head}, not {settings.expected_sha}")
    if changes not in ([], ["cdk.json"]):
        problems.append("the upgrade changed tracked files: " + ", ".join(changes))
    if cdk_json_sha256 != (prepared.get("cdk_json") or {}).get("sha256"):
        problems.append("the upgrade did not preserve cdk.json byte for byte")
    evidence["stacks"], stack_problems = _verify_generations(ctx, phase)
    evidence["upgrade"], document_problems = _verify_document(phase)
    problems.extend([*stack_problems, *document_problems])
    try:
        evidence["sentinel"] = verify_sentinel(
            ctx,
            state.get("sentinel") or {},
            timeout_seconds=settings.api_ready_timeout_seconds,
        )
    except RuntimeError as exc:
        problems.append(f"sentinel: {exc}")
    finally:
        try:
            evidence["sentinel_cleanup"] = delete_sentinel(
                ctx, timeout_seconds=settings.api_ready_timeout_seconds
            )
        except RuntimeError as exc:
            problems.append(f"sentinel cleanup: {exc}")
    if problems:
        raise ActionFailure(
            "The upgrade did not leave the deployment it promises: " + "; ".join(problems),
            evidence,
        )
    return evidence
