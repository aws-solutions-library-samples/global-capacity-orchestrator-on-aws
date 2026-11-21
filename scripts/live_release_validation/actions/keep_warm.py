"""keep-warm: prove back-to-back keep-warm GPU Jobs share one node and its idle time is recorded."""

from __future__ import annotations

from typing import Any

from ..checks.cluster import cluster_kubectl
from ..checks.keep_warm import run_keep_warm_probe
from ..models import RunContext


def action_keep_warm(ctx: RunContext) -> dict[str, Any]:
    """Run two keep-warm GPU Jobs back to back in the first Region and read their records.

    The second Job must run on the node the first one left idle, with no new
    NodeClaim in ``gpu-keep-warm-pool``; the first Job's idle record must close
    as ``reused``; and once the node is reclaimed, about one cdk.json
    ``gpu_keep_warm.window`` after the second Job finished, the second Job's
    record must close as ``reclaimed`` with that idle time
    (:mod:`..checks.keep_warm`). The tunnelled kubectl session reads the
    NodeClaims and watches the node go.
    """
    with cluster_kubectl(ctx, ctx.deployment_regions[0]) as kubectl:
        evidence = run_keep_warm_probe(ctx, kubectl)
    ctx.checkpoint.state["keep_warm_validation"] = evidence
    ctx.persist()
    return evidence
