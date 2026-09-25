"""eks-capabilities: the opt-in AWS-managed ACK / kro are attached and ACTIVE."""

from __future__ import annotations

from typing import Any

from ..checks.eks_capabilities import (
    effective_eks_capabilities_config,
    enabled_types_by_region,
    verify_capabilities_attached,
)
from ..models import RunContext


def action_eks_capabilities(ctx: RunContext) -> dict[str, Any]:
    """Require every configured EKS Capability to be attached, ACTIVE and drift-free.

    Resolves the block the run deployed with (cdk.json ``eks_capabilities``
    merged with the run's ``--eks-capabilities`` selection). When no type is
    enabled for any deployed Region the action passes with a note: the shipped
    default is every capability off, and that default is what most runs
    validate. Otherwise, for every Region with an enabled type, the EKS API
    must report each enabled capability attached to the Region's cluster and
    ``ACTIVE`` with no drift from the configuration, judged by the same merge
    ``gco stacks capabilities status`` prints. AWS API only — no cluster
    session is needed.
    """
    config = effective_eks_capabilities_config(ctx)
    by_region = enabled_types_by_region(config, ctx.deployment_regions)
    if not by_region:
        evidence: dict[str, Any] = {
            "enabled": False,
            "detail": (
                "No EKS capability is enabled for any deployed Region (the shipped default); "
                "pass --eks-capabilities ack,kro (or all) to prove them"
            ),
        }
        ctx.checkpoint.state["eks_capabilities_validation"] = evidence
        ctx.persist()
        return evidence

    regions: dict[str, Any] = {}
    for region, enabled_types in by_region.items():
        record: dict[str, Any] = {"enabled_types": enabled_types}
        regions[region] = record
        ctx.checkpoint.state["eks_capabilities_validation"] = {"enabled": True, "regions": regions}
        ctx.persist()

        status = verify_capabilities_attached(ctx, region, config)
        record["cluster_arn"] = status["cluster_arn"]
        record["capabilities"] = [
            {
                key: row.get(key)
                for key in ("type", "capability_name", "status", "version", "arn", "role_arn")
            }
            for row in status["capabilities"]
            if row["configured"]
        ]
        ctx.persist()

    evidence = {"enabled": True, "regions": regions}
    ctx.checkpoint.state["eks_capabilities_validation"] = evidence
    ctx.persist()
    return evidence
