"""Scheduler enablement resolution for the ``schedulers`` action.

The harness has no direct Kubernetes access, so scheduler validation rests on
one architectural fact: the default kube-scheduler ignores pods whose
``spec.schedulerName`` it does not own, and Kueue's webhook keeps a
queue-labeled Job suspended until quota admission. A probe Job that names a
scheduler (or carries the Kueue queue label) therefore only ever completes if
that scheduler actually did its work — completion *is* the scheduling proof,
observable through the deployed manifest API like every other validation Job.

This module resolves which schedulers the run must probe: the cdk.json helm
block decides, and ``--optional-schedulers`` run overrides force off-by-default
schedulers on (threaded to CDK as the ``helm_enabled_overrides`` context by the
runner, so the deployed chart set and this resolution share one source).
"""

from __future__ import annotations

from typing import Any

from ..models import RunContext

#: Schedulers the action can probe, in probe order: name -> cdk.json helm key.
#:
#: The Kubeflow Trainer is deliberately NOT probed here: it is a workload
#: controller (TrainJob -> JobSet compilation), not a pod scheduler, so the
#: "completion proves the scheduler did its work" contract this action rests
#: on does not apply. Its live proof rides two other rails instead: the
#: examples harness runs the kubeflow-trainjob example end to end (gang
#: all-reduce through the shipped runtime), and the topology action's
#: helmValidation asserts the kubeflow-trainer release converged exactly as
#: the deployment config demanded.
PROBED_SCHEDULERS: dict[str, str] = {
    "volcano": "volcano",
    "kueue": "kueue",
    "yunikorn": "yunikorn",
    "slurm": "slurm",
}

#: Off-by-default schedulers a run may force-enable with --optional-schedulers.
OPTIONAL_SCHEDULERS: tuple[str, ...] = ("yunikorn", "slurm")


def effective_scheduler_enablement(ctx: RunContext) -> dict[str, dict[str, Any]]:
    """Resolve every probed scheduler's effective enablement and its source.

    Mirrors ``gco.stacks.regional_stack._helm_chart_enabled`` semantics for
    the probed keys: a run override wins, then the cdk.json toggle, and a
    missing key defaults to enabled (the historical chart_map behavior).
    """
    helm_config = ctx.cdk_context.get("helm")
    if not isinstance(helm_config, dict):
        helm_config = {}
    overrides = set(ctx.settings.optional_schedulers)
    unknown = sorted(overrides - set(OPTIONAL_SCHEDULERS))
    if unknown:
        raise RuntimeError(
            "optional_schedulers contains names that are not optional schedulers: "
            + ", ".join(unknown)
        )

    enablement: dict[str, dict[str, Any]] = {}
    for scheduler, helm_key in PROBED_SCHEDULERS.items():
        if scheduler in overrides:
            enablement[scheduler] = {"enabled": True, "source": "run-override"}
            continue
        chart_config = helm_config.get(helm_key)
        configured = (
            bool(chart_config.get("enabled", True)) if isinstance(chart_config, dict) else True
        )
        enablement[scheduler] = {"enabled": configured, "source": f"cdk.json helm.{helm_key}"}
    return enablement
