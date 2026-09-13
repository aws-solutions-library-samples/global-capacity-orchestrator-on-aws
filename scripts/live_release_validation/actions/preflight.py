"""preflight: verify exact git, account, configuration, and ownership identity."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..constants import (
    _CLUSTER_TUNNEL_ACTIONS,
    _HEALTHY_STACK_STATUSES,
)
from ..context import (
    _direct_regional_access_enabled,
    _resolve_branch,
    _run_git,
    _topology_regions,
    _validate_profile,
)
from ..inventory import (
    collect_project_stacks,
    describe_stack,
    discover_enabled_regions,
)
from ..models import RunContext, RunSettings
from ..ownership.ecr import (
    _expected_ecr_images,
)
from ..ownership.stacks import (
    _reconcile_stack_ownership,
)

_GIB = float(1024**3)


def _nearest_existing(path: Path) -> Path:
    """Walk up until a path that exists (the report dir may not yet)."""
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _check_free_disk(settings: RunSettings) -> dict[str, float]:
    """Refuse to deploy from a host that cannot absorb this run's disk usage.

    ``deploy`` builds every service image locally before publishing, the
    checkpoint grows to tens of megabytes, and the container runtime's image
    store lives under the home volume on macOS. A host that fills up mid-run
    fails the image build, then fails to persist the checkpoint, and that
    second failure aborts the guaranteed cleanup too — leaving stacks behind
    that only a resume can reclaim. Measure the floor before anything is
    created. Every probed location is reported so the operator sees where
    the space went; the check fails on the first location below the floor.
    """
    floor_gib = float(settings.min_free_disk_gib)
    probes = {
        "repo_root": Path(settings.repo_root),
        "report_dir": Path(settings.report_dir),
        "home": Path.home(),
    }
    observed: dict[str, float] = {}
    short: list[str] = []
    for label, path in probes.items():
        free_gib = shutil.disk_usage(_nearest_existing(path)).free / _GIB
        observed[label] = round(free_gib, 2)
        if free_gib < floor_gib:
            short.append(f"{label} ({path}) has {free_gib:.1f} GiB free")
    if short:
        raise RuntimeError(
            f"Free disk space is below the {floor_gib:g} GiB floor deploy needs for "
            "container image builds and checkpoint persistence: "
            + "; ".join(short)
            + ". Reclaim space (stale container images from earlier runs are the usual "
            "culprit) or lower --min-free-disk-gib, then rerun."
        )
    return observed


def action_preflight(ctx: RunContext) -> dict[str, Any]:
    """Validate exact git/AWS/config identity and prove project ownership."""
    settings = ctx.settings
    head = _run_git(settings.repo_root, "rev-parse", "HEAD")
    if head != settings.expected_sha:
        raise RuntimeError(f"HEAD {head} does not match expected SHA {settings.expected_sha}")

    branch = _resolve_branch(settings.repo_root)
    if branch != settings.expected_branch:
        raise RuntimeError(
            f"Current branch {branch!r} does not match expected branch {settings.expected_branch!r}"
        )

    dirty = _run_git(
        settings.repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if dirty:
        raise RuntimeError(
            "Live validation requires a clean worktree; commit or remove these paths:\n" + dirty
        )

    selected = set(ctx.report.selected_actions)
    session_manager_plugin = None
    tunnel_actions = sorted(selected & _CLUSTER_TUNNEL_ACTIONS)
    if tunnel_actions:
        session_manager_plugin = shutil.which("session-manager-plugin")
        if session_manager_plugin is None:
            raise RuntimeError(
                f"The {', '.join(tunnel_actions)} action(s) reach the private cluster "
                "endpoint through an SSM tunnel and require the AWS Session Manager plugin "
                "before deploy. Install session-manager-plugin and ensure it is on PATH, "
                "then resume."
            )

    free_disk_gib: dict[str, float] | None = None
    if "deploy" in selected and settings.min_free_disk_gib > 0:
        free_disk_gib = _check_free_disk(settings)

    identity = ctx.session.client("sts", region_name=ctx.config.global_region).get_caller_identity()
    account = str(identity.get("Account") or "")
    if account != settings.expected_account:
        raise RuntimeError(
            f"AWS caller account {account or 'unknown'} does not match expected "
            f"account {settings.expected_account}"
        )

    _validate_profile(ctx)
    if "deploy" in selected and not settings.confirm_kms_key_deletion:
        raise RuntimeError(
            "Deployment creates retained EKS encryption keys. Pass "
            "--confirm-kms-key-deletion to explicitly authorize scheduling only "
            "this run's exact keys for deletion during cleanup."
        )
    direct_regional_access = _direct_regional_access_enabled(ctx)
    if (
        len(ctx.deployment_regions) > 1
        and selected.intersection({"api", "sqs", "central-queue"})
        and not direct_regional_access
    ):
        raise RuntimeError(
            "Multi-Region Job actions require api_gateway.regional_api_enabled=true; "
            "the global API cannot prove which same-named regional Job it observed"
        )

    enabled_regions = discover_enabled_regions(ctx.session, ctx.config.global_region)
    target_stacks = ctx.stack_manager.list_stacks()
    if not target_stacks:
        raise RuntimeError("CDK returned no target stacks")
    expected_ecr_images = _expected_ecr_images(ctx, target_stacks)
    unexpected_names = [
        name
        for name in target_stacks
        if not (name == ctx.config.project_name or name.startswith(f"{ctx.config.project_name}-"))
    ]
    if unexpected_names:
        raise RuntimeError(
            "Refusing to own non-project CDK stacks: " + ", ".join(sorted(unexpected_names))
        )

    target_stack_regions = {
        stack_name: ctx.stack_manager._get_destroy_region(stack_name)
        for stack_name in target_stacks
    }
    if any(not region for region in target_stack_regions.values()):
        raise RuntimeError(
            "Could not resolve target stack Regions: "
            + json.dumps(target_stack_regions, sort_keys=True)
        )
    target_region_set = {str(region) for region in target_stack_regions.values()}
    unavailable_targets = sorted(target_region_set - set(enabled_regions))
    if unavailable_targets:
        raise RuntimeError(
            "Target Regions are not enabled for this account: " + ", ".join(unavailable_targets)
        )

    bootstrap_stacks: dict[str, Any] = {}
    for region in sorted(target_region_set):
        bootstrap = describe_stack(ctx.session, region, "CDKToolkit")
        if bootstrap is None or bootstrap.get("status") not in _HEALTHY_STACK_STATUSES:
            status = bootstrap.get("status") if bootstrap else "absent"
            raise RuntimeError(
                f"Region {region} must already contain a healthy CDKToolkit stack; found {status}. "
                "Live validation never auto-bootstraps or mutates the protected baseline."
            )
        bootstrap_stacks[region] = {
            "stack_id": bootstrap["stack_id"],
            "status": bootstrap["status"],
        }

    previous_bootstrap = ctx.checkpoint.state.get("bootstrap_stacks")
    if previous_bootstrap is not None and previous_bootstrap != bootstrap_stacks:
        raise RuntimeError(
            "Checkpointed CDKToolkit ARN/status changed; refusing bootstrap adoption"
        )
    previous_ecr_targets = ctx.checkpoint.state.get("expected_ecr_images")
    if previous_ecr_targets is not None and previous_ecr_targets != expected_ecr_images:
        raise RuntimeError("Cloud-assembly ECR image targets changed since checkpoint creation")

    existing = collect_project_stacks(
        ctx.session,
        enabled_regions,
        ctx.config.project_name,
    )
    if not ctx.checkpoint.deployment_attempted and existing:
        raise RuntimeError(
            "Fresh runs refuse pre-existing project stacks because ownership is unproven: "
            + json.dumps(existing, sort_keys=True)
        )

    previous_targets = ctx.checkpoint.state.get("target_stack_regions")
    if previous_targets is not None and previous_targets != target_stack_regions:
        raise RuntimeError(
            "CDK target stacks changed since the checkpoint was created; refusing resume"
        )

    ctx.checkpoint.state.update(
        {
            "account_arn": str(identity.get("Arn") or ""),
            "enabled_regions": enabled_regions,
            "target_stack_regions": target_stack_regions,
            "topology_regions": list(_topology_regions(ctx)),
            "bootstrap_stacks": bootstrap_stacks,
            "expected_ecr_images": expected_ecr_images,
            "direct_regional_access": direct_regional_access,
            "preexisting_project_stacks": existing
            if not ctx.checkpoint.deployment_attempted
            else ctx.checkpoint.state.get("preexisting_project_stacks", {}),
        }
    )
    ctx.persist()
    if ctx.checkpoint.deployment_attempted:
        _reconcile_stack_ownership(ctx)

    return {
        "account": account,
        "caller_arn": identity.get("Arn"),
        "sha": head,
        "branch": branch,
        "profile": settings.profile,
        "deployment_regions": list(ctx.deployment_regions),
        "topology_regions": list(_topology_regions(ctx)),
        "enabled_regions": enabled_regions,
        "target_stack_regions": target_stack_regions,
        "bootstrap_stacks": bootstrap_stacks,
        "expected_ecr_images": expected_ecr_images,
        "direct_regional_access": direct_regional_access,
        "session_manager_plugin": session_manager_plugin or "not-required",
        "min_free_disk_gib": settings.min_free_disk_gib,
        "free_disk_gib": free_disk_gib if free_disk_gib is not None else "not-required",
        "kms_key_deletion_confirmed": settings.confirm_kms_key_deletion,
        "resume": settings.resume,
    }
