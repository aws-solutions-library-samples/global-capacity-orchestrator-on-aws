"""Run-context helpers: git identity, profile, and Region topology."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import RunContext


def _run_git(repo_root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
    return result.stdout.strip()


def _resolve_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "symbolic-ref", "--short", "HEAD", check=False)
    if branch:
        return branch
    raise RuntimeError("HEAD is detached; local live validation requires a checked-out branch")


def _validate_profile(ctx: RunContext) -> None:
    count = len(ctx.deployment_regions)
    profile = ctx.settings.profile
    if profile == "single-region" and count != 1:
        raise RuntimeError(
            f"single-region profile requires exactly one regional Region; cdk.json has {count}"
        )
    if profile == "multi-region" and count < 2:
        raise RuntimeError(
            f"multi-region profile requires at least two regional Regions; cdk.json has {count}"
        )
    if profile not in {"configured", "single-region", "multi-region"}:
        raise RuntimeError(f"Unknown validation profile: {profile}")


def _topology_regions(ctx: RunContext) -> tuple[str, ...]:
    regions = ctx.cdk_context["deployment_regions"]
    return tuple(
        dict.fromkeys(
            (
                str(regions["global"]),
                str(regions["api_gateway"]),
                str(regions["monitoring"]),
                *ctx.deployment_regions,
            )
        )
    )


def _direct_regional_access_enabled(ctx: RunContext) -> bool:
    partition = ctx.session.get_partition_for_region(ctx.config.global_region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {ctx.config.global_region}")
    configured = bool((ctx.cdk_context.get("api_gateway") or {}).get("regional_api_enabled", False))
    return partition != "aws" or configured


def _job_transport_region(ctx: RunContext, execution_region: str) -> str | None:
    """Choose authorized transport without probing a denied regional bridge."""
    if _direct_regional_access_enabled(ctx):
        return execution_region
    if len(ctx.deployment_regions) == 1 and execution_region == ctx.deployment_regions[0]:
        return None
    raise RuntimeError(
        "Multi-Region workload validation requires api_gateway.regional_api_enabled=true "
        "so each Job can be observed and deleted in its exact execution Region"
    )


def _project_ecr_name(name: str, project_name: str) -> bool:
    return name == project_name or name.startswith((f"{project_name}/", f"{project_name}-"))
