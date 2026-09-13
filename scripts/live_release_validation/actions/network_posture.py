"""network-posture: the shipped NetworkPolicies decide traffic on the live cluster."""

from __future__ import annotations

from typing import Any

from ..checks.cluster import cluster_kubectl
from ..checks.network_posture import NetworkPostureProbe
from ..checks.platform_workloads import network_policy_enforcement_enabled
from ..models import RunContext


def action_network_posture(ctx: RunContext) -> dict[str, Any]:
    """Require every Region's cluster to enforce the documented zero-trust posture.

    For every deployed Region, open the tunnelled kubectl session, start one
    digest-pinned listener in ``gco-system`` and one in ``gco-jobs``, then dial
    them (and the live inference-monitor's metrics port, and an AWS-hosted
    HTTPS endpoint) from throwaway client pods whose exit code is the verdict:
    same-namespace job traffic, the metrics port, and HTTPS egress must be
    reachable; cross-namespace ingress into either namespace and non-443
    egress from ``gco-jobs`` must be blocked. Every probe and listener is a
    run-labelled Job deleted before the action returns.

    When cdk.json sets ``eks_cluster.network_policy_enforcement: false`` the
    deny verdicts are recorded as skipped — that mode promises none — and the
    reachability probes still have to pass.
    """
    regions: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        with cluster_kubectl(ctx, region) as kubectl:
            regions[region] = NetworkPostureProbe(ctx, region, kubectl).run()
    return {
        "network_policy_enforcement": network_policy_enforcement_enabled(ctx),
        "regions": regions,
    }
