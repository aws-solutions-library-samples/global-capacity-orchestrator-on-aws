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


def effective_kubernetes_version(ctx: RunContext) -> str:
    """The EKS minor this run deploys and verifies the regional clusters at.

    The run's ``--kubernetes-version`` override when set (it is the
    ``kubernetes_version`` CDK context every deploy of the run carried),
    otherwise cdk.json's. The runner loads ``cdk.json`` once
    (``RunContext.cdk_context``); a context without the key, or with a
    non-string value, is a broken checkout rather than something to default,
    so it raises instead of letting a version comparison pass vacuously.
    """
    override = getattr(ctx.settings, "kubernetes_version_override", "")
    if isinstance(override, str) and override.strip():
        return override.strip()
    version = ctx.cdk_context.get("kubernetes_version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("cdk.json context.kubernetes_version is missing or not a string")
    return version.strip()


def _global_api_carries_workloads(ctx: RunContext) -> bool:
    """Whether the global API is a workload ingress: in the commercial ``aws`` partition only.

    Elsewhere Global Accelerator is omitted and the global API only aggregates,
    so the regional API bridges are the workload ingress.
    """
    partition = ctx.session.get_partition_for_region(ctx.config.global_region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {ctx.config.global_region}")
    return bool(partition == "aws")


def _job_transport_region(ctx: RunContext, execution_region: str) -> str | None:
    """The Region whose API bridge a workload call goes to, or ``None`` for the global API.

    Every deployment has a regional API bridge in each regional Region, and an
    identity allowed ``execute-api:Invoke`` (the operator running the harness)
    may call it, so a call is pinned to the exact execution Region through that
    bridge. A single-Region run in the ``aws`` partition drives the global API
    instead: the path an unpinned ``gco`` call takes. The cdk.json key
    ``api_gateway.regional_api_enabled`` plays no part: it no longer changes
    the bridges, and a cdk.json may carry it or not.
    """
    if (
        len(ctx.deployment_regions) == 1
        and execution_region == ctx.deployment_regions[0]
        and _global_api_carries_workloads(ctx)
    ):
        return None
    return execution_region


def _project_ecr_name(name: str, project_name: str) -> bool:
    return name == project_name or name.startswith((f"{project_name}/", f"{project_name}-"))
