"""platform-workloads: the gco-system services are hosted as the manifests promise."""

from __future__ import annotations

from typing import Any

from ..checks.cluster import cluster_kubectl
from ..checks.platform_workloads import (
    expected_deployments,
    manifest_processor_autoscaling_enabled,
    network_policy_enforcement_enabled,
    verify_platform_workloads,
)
from ..models import RunContext


def action_platform_workloads(ctx: RunContext) -> dict[str, Any]:
    """Require every Region's platform services to be converged, restart-free, drain-safe, and autoscaled as configured.

    For every deployed Region, open the tunnelled kubectl session and poll a
    bounded snapshot of the ``gco-system`` Deployments (health-monitor,
    manifest-processor, inference-monitor, inference-proxy, plus cost-monitor
    when cost monitoring is configured). Each must be converged at its current
    generation with every live container at zero restarts; each multi-replica
    service's PodDisruptionBudget must be ``maxUnavailable: 1`` and currently
    allow a disruption; the inference-proxy HPA must exist, target its
    Deployment, and be able to scale, and the manifest-processor HPA must exist
    exactly when ``manifest_processor.autoscaling.enabled`` is set; and the
    ``kube-system/amazon-vpc-cni`` switch must carry the value rendered from
    ``eks_cluster.network_policy_enforcement``.

    Running late in the registry is deliberate: by then the Job, queue,
    scheduler, inference, and cost actions have exercised every service, so a
    zero restart count is evidence the services held up under real traffic,
    not just that they started.
    """
    regions: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        with cluster_kubectl(ctx, region) as kubectl:
            regions[region] = verify_platform_workloads(ctx, region, kubectl)
    return {
        "deployments": list(expected_deployments(ctx)),
        "manifest_processor_autoscaling": manifest_processor_autoscaling_enabled(ctx),
        "network_policy_enforcement": network_policy_enforcement_enabled(ctx),
        "regions": regions,
    }
