"""preflight: verify exact git, account, configuration, and ownership identity."""

from __future__ import annotations

import json
from typing import Any

from ..constants import (
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
from ..models import RunContext
from ..ownership.ecr import (
    _expected_ecr_images,
)
from ..ownership.stacks import (
    _reconcile_stack_ownership,
)


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

    identity = ctx.session.client("sts", region_name=ctx.config.global_region).get_caller_identity()
    account = str(identity.get("Account") or "")
    if account != settings.expected_account:
        raise RuntimeError(
            f"AWS caller account {account or 'unknown'} does not match expected "
            f"account {settings.expected_account}"
        )

    _validate_profile(ctx)
    selected = set(ctx.report.selected_actions)
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
        "kms_key_deletion_confirmed": settings.confirm_kms_key_deletion,
        "resume": settings.resume,
    }
