"""schedulers: prove every enabled batch scheduler against a real workload."""

from __future__ import annotations

from typing import Any

from ..checks.jobs import _run_api_transport_lifecycle
from ..checks.pcs import effective_pcs_enablement, probe_pcs
from ..checks.schedulers import (
    PROBED_SCHEDULERS,
    effective_scheduler_enablement,
)
from ..models import RunContext


def action_schedulers(ctx: RunContext) -> dict[str, Any]:
    """Run one scheduling-proof Job per enabled scheduler and record evidence.

    Volcano, YuniKorn: a Job whose pods name the scheduler completes only if
    that scheduler bound them. Kueue: a queue-labeled Job stays webhook-
    suspended until the deployed gco-default queue admits it. Slurm: a probe
    Job submits a real batch job through slurmrestd and requires COMPLETED.
    AWS PCS (the second Slurm backend, on for every release run through the
    ``pcs_overrides`` context): a batch job submitted through the GCO API
    with ``backend=pcs`` must reach COMPLETED on a node PCS launched from
    zero, land in accounting with its caller attribution, and its output
    file must be readable from the managed login node over SSM.
    Disabled schedulers are recorded as skipped with the configuration source;
    KEDA and KubeRay carry derived evidence (see the reasons in the result).
    """
    enablement = effective_scheduler_enablement(ctx)
    results: dict[str, dict[str, Any]] = {}
    for scheduler in PROBED_SCHEDULERS:
        state = enablement[scheduler]
        if not state["enabled"]:
            results[scheduler] = {
                "status": "skipped",
                "reason": (
                    f"Disabled by {state['source']}; pass --optional-schedulers to "
                    "force-enable an off-by-default scheduler for the run"
                ),
            }
            continue
        lifecycle = _run_api_transport_lifecycle(
            ctx,
            manifest_filename=f"{scheduler}-smoke-job.yaml",
            path=scheduler,
            marker_prefix=scheduler.upper(),
        )
        results[scheduler] = {"status": "validated", "source": state["source"], **lifecycle}

    pcs_state = effective_pcs_enablement(ctx)
    enablement["pcs"] = pcs_state
    if pcs_state["enabled"]:
        results["pcs"] = {"status": "validated", "source": pcs_state["source"], **probe_pcs(ctx)}
    else:
        results["pcs"] = {
            "status": "skipped",
            "reason": (
                f"Disabled by {pcs_state['source']}; a release run turns AWS PCS on through "
                "the pcs_overrides CDK context"
            ),
        }

    results["keda"] = {
        "status": "derived",
        "reason": (
            "KEDA is a mandatory platform chart; its ScaledJob scaling path is "
            "exercised end to end by the sqs action (the queue processor scales "
            "from zero to apply that action's Job) and its chart convergence by "
            "the topology action"
        ),
    }
    results["kuberay"] = {
        "status": "chart-level",
        "reason": (
            "RayCluster is a custom resource outside the manifest gateway's kind "
            "allowlist, so no transport can carry a Ray workload; operator and "
            "chart convergence are proved by the topology action"
        ),
    }

    evidence = {"enablement": enablement, "schedulers": results}
    ctx.checkpoint.state["scheduler_validation"] = evidence
    ctx.persist()
    return evidence
