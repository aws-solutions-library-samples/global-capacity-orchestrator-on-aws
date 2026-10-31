"""hyperpod: prove the SageMaker HyperPod instance group joined the cluster and runs work."""

from __future__ import annotations

from typing import Any

from ..checks.hyperpod import effective_hyperpod_enablement, probe_hyperpod
from ..models import RunContext


def action_hyperpod(ctx: RunContext) -> dict[str, Any]:
    """Prove the deployed HyperPod capacity, or record why nothing was deployed.

    With HyperPod on (every release run turns it on through the
    ``hyperpod_overrides`` CDK context), SageMaker must describe the stack's
    cluster ``InService`` with every instance group at its target count and
    every instance ``Running``; a Job labelled for the ``gco-hyperpod`` Kueue
    queue and requesting one GPU must complete through the GCO API; and the
    API's placement report must put its pod on a ``hyperpod`` node. With
    HyperPod off, the action records the configuration source and skips.
    """
    state = effective_hyperpod_enablement(ctx)
    if state["enabled"]:
        result: dict[str, Any] = {
            "status": "validated",
            "source": state["source"],
            **probe_hyperpod(ctx),
        }
    else:
        result = {
            "status": "skipped",
            "reason": (
                f"Disabled by {state['source']}; a release run turns SageMaker HyperPod on "
                "through the hyperpod_overrides CDK context"
            ),
        }
    evidence = {"enablement": state, "hyperpod": result}
    ctx.checkpoint.state["hyperpod_validation"] = evidence
    ctx.persist()
    return evidence
