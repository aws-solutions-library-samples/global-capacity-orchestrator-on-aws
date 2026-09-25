"""Checks behind the ``eks-capabilities`` action.

The opt-in EKS Capabilities (AWS-managed ACK and kro; see
``docs/EKS_CAPABILITIES.md``) are off in the shipped ``cdk.json``, and the
harness's preflight requires a clean worktree, so a run enables them the way
it enables optional schedulers: through run-scoped CDK context. This module
turns ``--eks-capabilities`` into the ``eks_capabilities_overrides`` JSON the
deploy carries and later resolves the same merged block to know what to
prove.

What the action proves, per deployed Region: every enabled capability is
attached to the Region's cluster, ``ACTIVE`` and drift-free — read through
the same configured-vs-live merge ``gco stacks capabilities status`` uses
(``cli.eks_capabilities``), so the CLI's drift model and the harness agree.
What each capability then does in the cluster (a kro ResourceGraphDefinition
composing a Job, an ACK SQS queue) is the example harness's job
(``examples/kro-batch-job.yaml``, ``examples/ack-sqs-queue.yaml``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cli.eks_capabilities import build_status, describe_live_capabilities
from gco.eks_capabilities_config import (
    EKS_CAPABILITY_TYPES,
    EksCapabilitiesConfigError,
    enabled_capability_types,
    merge_eks_capabilities_overrides,
    parse_eks_capabilities_overrides,
    validate_eks_capabilities_config,
)

from ..models import RunContext


class EksCapabilitiesValidationError(RuntimeError):
    """The deployed capabilities do not match what the run configured."""


# ─── run inputs -> eks_capabilities_overrides ────────────────────────────────


def build_eks_capabilities_overrides(
    *,
    types: tuple[str, ...],
    extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Turn the run's capability selection into the ``eks_capabilities_overrides`` object.

    Every named type is enabled for every Region the run deploys. ``extra``
    carries per-type settings merged into that type's block (the example
    harness uses it to give ACK the managed policy its SQS example needs).
    Raises ``ValueError`` on an unknown type, or on ``extra`` for a type that
    is not selected.
    """
    unknown = sorted(set(types) - set(EKS_CAPABILITY_TYPES))
    if unknown:
        raise ValueError(
            "unknown EKS capability type(s): "
            + ", ".join(unknown)
            + "; valid: "
            + ", ".join(EKS_CAPABILITY_TYPES)
        )
    extra = extra or {}
    stray = sorted(set(extra) - set(types))
    if stray:
        raise ValueError(
            "capability settings for type(s) the run does not enable: " + ", ".join(stray)
        )
    overrides: dict[str, Any] = {}
    for name in EKS_CAPABILITY_TYPES:
        if name in types:
            overrides[name] = {**dict(extra.get(name) or {}), "enabled": True}
    return overrides


def overrides_json(overrides: Mapping[str, Any]) -> str:
    """Canonical JSON for the ``--context`` value and the resume identity."""
    return json.dumps(overrides, sort_keys=True, separators=(",", ":"))


# ─── effective configuration ─────────────────────────────────────────────────


def effective_eks_capabilities_config(ctx: RunContext) -> dict[str, Any]:
    """The block the run deployed with: cdk.json merged with the run's overrides."""
    try:
        overrides = parse_eks_capabilities_overrides(
            getattr(ctx.settings, "eks_capabilities_overrides_json", "") or None
        )
        raw = merge_eks_capabilities_overrides(ctx.cdk_context.get("eks_capabilities"), overrides)
        return validate_eks_capabilities_config(raw, ctx.deployment_regions)
    except EksCapabilitiesConfigError as exc:
        raise EksCapabilitiesValidationError(f"eks_capabilities is invalid: {exc}") from exc


def enabled_types_by_region(
    config: Mapping[str, Any], regions: tuple[str, ...]
) -> dict[str, list[str]]:
    """``{region: [types]}`` for the Regions where at least one type is on."""
    result: dict[str, list[str]] = {}
    for region in regions:
        enabled = enabled_capability_types(config, region)
        if enabled:
            result[region] = enabled
    return result


# ─── AWS-side verification ───────────────────────────────────────────────────


def verify_capabilities_attached(
    ctx: RunContext,
    region: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Require every enabled type ACTIVE on the Region's cluster; return the CLI status document.

    Uses ``cli.eks_capabilities.build_status`` so a failure here reads exactly
    like ``gco stacks capabilities status`` would report it.
    """
    eks = ctx.session.client("eks", region_name=region)
    project_name = ctx.config.project_name
    cluster_name = f"{project_name}-{region}"
    cluster = eks.describe_cluster(name=cluster_name).get("cluster") or {}
    cluster_arn = str(cluster.get("arn") or "")
    if not cluster_arn:
        raise EksCapabilitiesValidationError(f"{region}: EKS returned no ARN for {cluster_name}")
    live = describe_live_capabilities(eks, cluster_name)
    status = build_status(region=region, project_name=project_name, config=config, live=live)
    problems = [f"{row['type']}: {row['drift']}" for row in status["capabilities"] if row["drift"]]
    if problems:
        raise EksCapabilitiesValidationError(
            f"{region}: capabilities drift from the run configuration: " + "; ".join(problems)
        )
    status["cluster_arn"] = cluster_arn
    return status
