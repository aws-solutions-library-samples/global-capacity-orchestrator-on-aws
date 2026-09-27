"""network-posture: the shipped NetworkPolicies decide traffic on the live cluster."""

from __future__ import annotations

from typing import Any

from ..checks.cluster import cluster_kubectl
from ..checks.network_posture import NetworkPostureProbe
from ..checks.opencost import _cost_monitoring_configured
from ..checks.platform_workloads import network_policy_enforcement_enabled
from ..models import RunContext


def action_network_posture(ctx: RunContext) -> dict[str, Any]:
    """Require every Region's cluster to enforce the documented zero-trust posture.

    For every deployed Region, open the tunnelled kubectl session, start one
    digest-pinned listener in ``gco-system``, one in ``gco-jobs``, and a
    stand-in model pod in ``gco-inference``, then dial them (and the live
    inference-monitor and cost-monitor, and an AWS-hosted HTTPS endpoint) from
    throwaway client pods whose exit code is the verdict: same-namespace job
    traffic, the inference-monitor's TLS metrics port 9443, and HTTPS egress
    must be reachable; cross-namespace ingress into either namespace, the
    cost monitor's 8443 from anything but the manifest processor, the model
    port 8443 from anything but the inference proxy, and non-443 egress from
    ``gco-jobs`` must be blocked; and the loopback-bound plaintext ports
    (inference-monitor 9090, cost-monitor 8080) must not answer on the pod
    network. ``GET /api/v1/cost/status`` must answer 200, proving the manifest
    processor reaches the cost monitor on 8443. Every probe and listener is a
    run-labelled Job deleted before the action returns.

    When cdk.json sets ``eks_cluster.network_policy_enforcement: false`` the
    deny verdicts are recorded as skipped — that mode promises none — and the
    reachability and loopback probes still have to pass; with cost monitoring
    off the cost-monitor probes are skipped with that reason.
    """
    regions: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        with cluster_kubectl(ctx, region) as kubectl:
            regions[region] = NetworkPostureProbe(ctx, region, kubectl).run()
    return {
        "network_policy_enforcement": network_policy_enforcement_enabled(ctx),
        "cost_monitoring": _cost_monitoring_configured(ctx),
        "regions": regions,
    }
