"""Platform workload hosting checks for the live cluster.

The ``platform-workloads`` action proves, on every deployed Region, that the
``gco-system`` services are hosted the way the manifests promise
(``lambda/kubectl-applier-simple/manifests/README.md``, "Platform Workload
Contract") and that the run left them healthy:

* every platform Deployment has converged at its current generation with all
  replicas updated, available, and ready, and no live container has restarted
  since its pod started — a crash loop or an OOM kill during the run is a
  finding, not noise;
* every multi-replica service carries a PodDisruptionBudget with
  ``maxUnavailable: 1`` that currently allows a voluntary disruption, so a node
  drain can proceed without emptying the service;
* the shipped autoscalers exist exactly when cdk.json says so — the
  inference-proxy HPA always, the manifest-processor HPA only when
  ``manifest_processor.autoscaling.enabled`` — target the right Deployment,
  and can read their metrics; and
* the Auto Mode network-policy switch (``kube-system/amazon-vpc-cni``) carries
  the value rendered from ``eks_cluster.network_policy_enforcement``.

States that heal by waiting (a rollout in flight, a budget still counting
healthy pods, an HPA awaiting its first metric sample) are polled for a bounded
time. Anything that cannot heal — a missing object, a restarted container, a
wrong budget or autoscaler shape, a wrong switch value — fails immediately with
the snapshot as evidence.
"""

from __future__ import annotations

import time
from typing import Any

from ..models import RunContext
from .cluster import KubectlRunner, kubectl_json
from .opencost import _cost_monitoring_configured

PLATFORM_NAMESPACE = "gco-system"
#: Deployments every topology ships, in manifest order (30-33).
PLATFORM_DEPLOYMENTS: tuple[str, ...] = (
    "health-monitor",
    "manifest-processor",
    "inference-monitor",
    "inference-proxy",
)
#: Present only when cdk.json ``cost_monitoring`` (and ``cluster_observability``,
#: its data source) is enabled (34).
COST_MONITOR_DEPLOYMENT = "cost-monitor"
#: The multi-replica services and their budgets; ``<name>-pdb`` in the manifests.
DISRUPTION_BUDGETS: tuple[str, ...] = tuple(f"{name}-pdb" for name in PLATFORM_DEPLOYMENTS)
#: ``(HPA name, Deployment it must target)``.
INFERENCE_PROXY_AUTOSCALER = ("inference-proxy-hpa", "inference-proxy")
MANIFEST_PROCESSOR_AUTOSCALER = ("manifest-processor-hpa", "manifest-processor")
ENFORCEMENT_SWITCH_NAMESPACE = "kube-system"
ENFORCEMENT_SWITCH_NAME = "amazon-vpc-cni"
#: Both spellings the manifest renders (see 06-network-policy-controller.yaml).
ENFORCEMENT_SWITCH_KEYS: tuple[str, ...] = (
    "enable-network-policy-controller",
    "enable-network-policy",
)
#: Deployments were rolled out long before this action runs; ten minutes covers
#: a node consolidation that happens to be rescheduling a pod as we look.
_CONVERGENCE_TIMEOUT_SECONDS = 600


class PlatformWorkloadValidationError(RuntimeError):
    """The platform services are not hosted as the manifests promise."""


def expected_deployments(ctx: RunContext) -> tuple[str, ...]:
    """Return the gco-system Deployments this cdk.json deploys."""
    if _cost_monitoring_configured(ctx):
        return (*PLATFORM_DEPLOYMENTS, COST_MONITOR_DEPLOYMENT)
    return PLATFORM_DEPLOYMENTS


def manifest_processor_autoscaling_enabled(ctx: RunContext) -> bool:
    """Return whether cdk.json opts the manifest processor into its HPA (default off)."""
    block = ctx.cdk_context.get("manifest_processor")
    autoscaling = block.get("autoscaling") if isinstance(block, dict) else None
    if isinstance(autoscaling, dict) and "enabled" in autoscaling:
        return bool(autoscaling["enabled"])
    return False


def network_policy_enforcement_enabled(ctx: RunContext) -> bool:
    """Return whether cdk.json keeps the Auto Mode policy controller on (default on)."""
    block = ctx.cdk_context.get("eks_cluster")
    if isinstance(block, dict) and "network_policy_enforcement" in block:
        return bool(block["network_policy_enforcement"])
    return True


def _int(value: Any) -> int:
    return int(value or 0)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _deployment_snapshot(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    name: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deployment = kubectl_json(
        kubectl,
        record,
        "get",
        "deployment",
        name,
        "--namespace",
        PLATFORM_NAMESPACE,
        timeout=timeout,
    )
    if deployment is None:
        return {"exists": False}
    metadata = _dict(deployment.get("metadata"))
    spec = _dict(deployment.get("spec"))
    status = _dict(deployment.get("status"))
    desired = _int(spec.get("replicas"))
    ready = _int(status.get("readyReplicas"))
    available = _int(status.get("availableReplicas"))
    updated = _int(status.get("updatedReplicas"))
    generation = _int(metadata.get("generation"))
    observed = _int(status.get("observedGeneration"))

    pods_payload = kubectl_json(
        kubectl,
        record,
        "get",
        "pods",
        "--namespace",
        PLATFORM_NAMESPACE,
        "--selector",
        f"app={name}",
        timeout=timeout,
    )
    pods: list[dict[str, Any]] = []
    for item in _list(_dict(pods_payload).get("items")):
        pod_metadata = _dict(_dict(item).get("metadata"))
        if pod_metadata.get("deletionTimestamp"):
            # A pod on its way out (scale-down, drain, rollout) is no longer
            # part of the service; its readiness and restarts are history.
            continue
        pod_status = _dict(_dict(item).get("status"))
        containers = [_dict(entry) for entry in _list(pod_status.get("containerStatuses"))]
        init_containers = [_dict(entry) for entry in _list(pod_status.get("initContainerStatuses"))]
        pods.append(
            {
                "name": pod_metadata.get("name"),
                "phase": pod_status.get("phase"),
                "ready": (
                    pod_status.get("phase") == "Running"
                    and bool(containers)
                    and all(entry.get("ready") is True for entry in containers)
                ),
                "restarts": sum(
                    _int(entry.get("restartCount")) for entry in (*init_containers, *containers)
                ),
            }
        )
    converged = (
        desired >= 1
        and observed >= generation
        and ready == desired
        and available == desired
        and updated == desired
        and len(pods) == desired
        and all(pod["ready"] for pod in pods)
    )
    return {
        "exists": True,
        "desired": desired,
        "ready": ready,
        "available": available,
        "updated": updated,
        "generation": generation,
        "observed_generation": observed,
        "pods": pods,
        "restarts": sum(pod["restarts"] for pod in pods),
        "failed_pods": [pod["name"] for pod in pods if pod["phase"] == "Failed"],
        "converged": converged,
    }


def _budget_snapshot(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    name: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    budget = kubectl_json(
        kubectl,
        record,
        "get",
        "poddisruptionbudget",
        name,
        "--namespace",
        PLATFORM_NAMESPACE,
        timeout=timeout,
    )
    if budget is None:
        return {"exists": False}
    spec = _dict(budget.get("spec"))
    status = _dict(budget.get("status"))
    return {
        "exists": True,
        "max_unavailable": spec.get("maxUnavailable"),
        "disruptions_allowed": _int(status.get("disruptionsAllowed")),
        "current_healthy": _int(status.get("currentHealthy")),
        "desired_healthy": _int(status.get("desiredHealthy")),
        "expected_pods": _int(status.get("expectedPods")),
    }


def _autoscaler_snapshot(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    name: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    autoscaler = kubectl_json(
        kubectl,
        record,
        "get",
        "horizontalpodautoscaler",
        name,
        "--namespace",
        PLATFORM_NAMESPACE,
        timeout=timeout,
    )
    if autoscaler is None:
        return {"exists": False}
    spec = _dict(autoscaler.get("spec"))
    status = _dict(autoscaler.get("status"))
    conditions = {
        _dict(entry).get("type"): _dict(entry).get("status")
        for entry in _list(status.get("conditions"))
    }
    return {
        "exists": True,
        "target": _dict(spec.get("scaleTargetRef")).get("name"),
        "min_replicas": spec.get("minReplicas"),
        "max_replicas": spec.get("maxReplicas"),
        "current_replicas": _int(status.get("currentReplicas")),
        "desired_replicas": _int(status.get("desiredReplicas")),
        "able_to_scale": conditions.get("AbleToScale") == "True",
        "scaling_active": conditions.get("ScalingActive") == "True",
    }


def _enforcement_switch_snapshot(
    kubectl: KubectlRunner,
    record: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    configmap = kubectl_json(
        kubectl,
        record,
        "get",
        "configmap",
        ENFORCEMENT_SWITCH_NAME,
        "--namespace",
        ENFORCEMENT_SWITCH_NAMESPACE,
        timeout=timeout,
    )
    if configmap is None:
        return {"exists": False, "values": {}}
    data = _dict(configmap.get("data"))
    return {"exists": True, "values": {key: data.get(key) for key in ENFORCEMENT_SWITCH_KEYS}}


def _snapshot(ctx: RunContext, kubectl: KubectlRunner, record: dict[str, Any]) -> dict[str, Any]:
    timeout = float(ctx.settings.command_timeout_seconds)
    deployments = {
        name: _deployment_snapshot(kubectl, record, name, timeout=timeout)
        for name in expected_deployments(ctx)
    }
    budgets = {
        name: _budget_snapshot(kubectl, record, name, timeout=timeout)
        for name in DISRUPTION_BUDGETS
    }
    autoscalers = {
        name: _autoscaler_snapshot(kubectl, record, name, timeout=timeout)
        for name, _target in (INFERENCE_PROXY_AUTOSCALER, MANIFEST_PROCESSOR_AUTOSCALER)
    }
    return {
        "deployments": deployments,
        "budgets": budgets,
        "autoscalers": autoscalers,
        "manifest_processor_autoscaling": manifest_processor_autoscaling_enabled(ctx),
        "network_policy_enforcement": network_policy_enforcement_enabled(ctx),
        "enforcement_switch": _enforcement_switch_snapshot(kubectl, record, timeout=timeout),
    }


def _violations(snapshot: dict[str, Any]) -> list[str]:
    """Return contract breaches that waiting cannot heal."""
    problems: list[str] = []
    for name, deployment in snapshot["deployments"].items():
        if not deployment["exists"]:
            problems.append(f"Deployment {name} is missing")
            continue
        if deployment["restarts"]:
            problems.append(
                f"Deployment {name} has {deployment['restarts']} container restart(s) "
                "across its live pods"
            )
        if deployment["failed_pods"]:
            problems.append(f"Deployment {name} has failed pod(s): {deployment['failed_pods']}")
    for name, budget in snapshot["budgets"].items():
        if not budget["exists"]:
            problems.append(f"PodDisruptionBudget {name} is missing")
        elif budget["max_unavailable"] not in (1, "1"):
            problems.append(
                f"PodDisruptionBudget {name} has maxUnavailable {budget['max_unavailable']!r}, "
                "expected 1"
            )
    expected_autoscalers = {INFERENCE_PROXY_AUTOSCALER[0]: INFERENCE_PROXY_AUTOSCALER[1]}
    if snapshot["manifest_processor_autoscaling"]:
        expected_autoscalers[MANIFEST_PROCESSOR_AUTOSCALER[0]] = MANIFEST_PROCESSOR_AUTOSCALER[1]
    for name, autoscaler in snapshot["autoscalers"].items():
        target = expected_autoscalers.get(name)
        if target is None:
            if autoscaler["exists"]:
                problems.append(
                    f"HorizontalPodAutoscaler {name} exists although cdk.json leaves "
                    "manifest_processor.autoscaling disabled"
                )
        elif not autoscaler["exists"]:
            problems.append(f"HorizontalPodAutoscaler {name} is missing")
        elif autoscaler["target"] != target:
            problems.append(
                f"HorizontalPodAutoscaler {name} targets {autoscaler['target']!r}, "
                f"expected {target!r}"
            )
    switch = snapshot["enforcement_switch"]
    expected_value = "true" if snapshot["network_policy_enforcement"] else "false"
    if not switch["exists"]:
        problems.append(
            f"ConfigMap {ENFORCEMENT_SWITCH_NAMESPACE}/{ENFORCEMENT_SWITCH_NAME} is missing"
        )
    else:
        for key in ENFORCEMENT_SWITCH_KEYS:
            if switch["values"].get(key) != expected_value:
                problems.append(
                    f"ConfigMap {ENFORCEMENT_SWITCH_NAME} key {key} is "
                    f"{switch['values'].get(key)!r}, expected {expected_value!r}"
                )
    return problems


def _pending(snapshot: dict[str, Any]) -> list[str]:
    """Return conditions that a bounded wait may still satisfy."""
    waiting: list[str] = []
    for name, deployment in snapshot["deployments"].items():
        if deployment["exists"] and not deployment["converged"]:
            waiting.append(
                f"Deployment {name} not converged "
                f"(desired={deployment['desired']} ready={deployment['ready']} "
                f"available={deployment['available']} updated={deployment['updated']} "
                f"live_pods={len(deployment['pods'])} "
                f"generation={deployment['generation']}/{deployment['observed_generation']})"
            )
    for name, budget in snapshot["budgets"].items():
        if budget["exists"] and budget["disruptions_allowed"] < 1:
            waiting.append(
                f"PodDisruptionBudget {name} allows no disruption "
                f"(healthy={budget['current_healthy']} desired={budget['desired_healthy']})"
            )
    for name, autoscaler in snapshot["autoscalers"].items():
        if autoscaler["exists"] and not (
            autoscaler["able_to_scale"] and autoscaler["scaling_active"]
        ):
            waiting.append(
                f"HorizontalPodAutoscaler {name} not active "
                f"(AbleToScale={autoscaler['able_to_scale']} "
                f"ScalingActive={autoscaler['scaling_active']})"
            )
    return waiting


def verify_platform_workloads(
    ctx: RunContext,
    region: str,
    kubectl: KubectlRunner,
) -> dict[str, Any]:
    """Poll one Region until every platform workload meets the hosting contract.

    Returns the converged snapshot. Raises ``PlatformWorkloadValidationError``
    on the first snapshot that breaches the contract, or when the transient
    conditions are still pending at the deadline; every observation is
    checkpointed under ``platform_workloads.<region>`` first.
    """
    with ctx.state_lock:
        record = ctx.checkpoint.state.setdefault("platform_workloads", {}).setdefault(region, {})
    deadline = time.monotonic() + _CONVERGENCE_TIMEOUT_SECONDS
    while True:
        snapshot = _snapshot(ctx, kubectl, record)
        snapshot["violations"] = _violations(snapshot)
        snapshot["pending"] = _pending(snapshot)
        with ctx.state_lock:
            record["last_snapshot"] = snapshot
            record["observations"] = _int(record.get("observations")) + 1
        ctx.persist()
        if snapshot["violations"]:
            raise PlatformWorkloadValidationError(
                f"platform workloads in {region} breach the hosting contract: "
                + "; ".join(snapshot["violations"])
            )
        if not snapshot["pending"]:
            return snapshot
        if time.monotonic() >= deadline:
            raise PlatformWorkloadValidationError(
                f"platform workloads in {region} did not converge within "
                f"{_CONVERGENCE_TIMEOUT_SECONDS}s: " + "; ".join(snapshot["pending"])
            )
        time.sleep(ctx.settings.poll_interval_seconds)
