"""policy: prove the deployed job-validation policy is fully readable.

Read-only. Adds no owned resources and mutates nothing, so it needs no cleanup
and can run alongside the lifecycle actions.
"""

from __future__ import annotations

from typing import Any

from ..checks.policy import _validate_region_policy
from ..models import RunContext


def action_policy(ctx: RunContext) -> dict[str, Any]:
    """Require every Region to report all three admission layers.

    ``GET /api/v1/policy`` answers "will this cluster admit the job I am about
    to pay to run" in three layers: the front-door per-manifest caps, the
    per-container ``LimitRange``, and the namespace aggregate ``ResourceQuota``.

    The cluster-read layers are fail-soft by design, and the degraded response
    is an HTTP 200 carrying a per-namespace ``status`` field. Every
    transport-level check in this harness therefore passes while the endpoint
    reports nothing usable -- which is exactly what happened on 2026-08-26, when
    all ten actions were green and ``cluster_enforcement."gco-jobs"`` was
    ``{"status": "unavailable", "reason": "403 Forbidden"}``. So this action
    asserts on the body, per Region and per namespace.

    It also requires the project's own ECR hostnames to be present in
    ``trusted_registries``, since CDK appends them at synth time and their
    absence would reject every job pulling a project-built image while no
    offline check could predict it.
    """
    regions: dict[str, Any] = {}
    evidence: dict[str, Any] = {"regions": regions}
    try:
        for region in ctx.deployment_regions:
            regions[region] = _validate_region_policy(ctx, region)
    except BaseException as exc:
        evidence["result"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        with ctx.state_lock:
            ctx.checkpoint.state["policy"] = evidence
            ctx.persist_callback(ctx.checkpoint)
        raise

    evidence["result"] = "passed"
    with ctx.state_lock:
        ctx.checkpoint.state["policy"] = evidence
        ctx.persist_callback(ctx.checkpoint)
    return evidence
