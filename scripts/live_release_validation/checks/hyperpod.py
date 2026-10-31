"""SageMaker HyperPod: the regional stack's instance group joins the EKS cluster and runs work.

Every release run turns HyperPod on through the run-scoped ``hyperpod_overrides``
CDK context (:data:`RELEASE_HYPERPOD_OVERRIDES`): one small GPU instance group
whose node joins the regional EKS cluster during the deploy. The probe then
proves the path the feature exists for:

1. SageMaker describes the cluster the stack published as ``InService`` with
   every instance group at its target count, and lists every instance
   ``Running``;
2. a Job labelled for the ``gco-hyperpod`` Kueue queue, requesting one GPU,
   completes through the GCO API (the same crash-safe lifecycle every
   scheduler probe uses), which proves the queue admitted it onto the
   HyperPod flavor and the device plugin pinned to those nodes advertised the
   GPU;
3. the API's placement report says the pod ran on a ``hyperpod`` node.

The account preflight that runs before the deploy (``actions/preflight.py``)
checks the same block with ``cli.hyperpod_preflight``.
"""

from __future__ import annotations

import json
from typing import Any

from cli.hyperpod_preflight import raw_hyperpod_config
from gco.hyperpod_config import (
    HYPERPOD_OVERRIDES_CONTEXT_KEY,
    HyperpodConfigError,
    apply_hyperpod_overrides,
    cluster_name,
    hyperpod_enabled,
    validate_hyperpod_config,
)

from ..models import RunContext
from .jobs import _run_api_transport_lifecycle

#: The ``hyperpod_overrides`` context every release-run CDK invocation carries:
#: the cheapest GPU type the HyperPod ``for cluster usage`` quotas commonly
#: allow, one instance, so the proof costs a single small node for the run.
RELEASE_HYPERPOD_OVERRIDES: dict[str, Any] = {
    "enabled": True,
    "instance_groups": [
        {"name": "gco-hyperpod", "instance_type": "ml.g4dn.xlarge", "instance_count": 1}
    ],
}

#: SageMaker's healthy states for the cluster and its instances.
_CLUSTER_READY = "InService"
_INSTANCE_READY = "Running"
_MANIFEST = "hyperpod-smoke-job.yaml"
_PATH = "hyperpod"


class HyperpodValidationError(RuntimeError):
    """The deployed HyperPod cluster did not behave as the block configured."""


def hyperpod_overrides_json() -> str:
    """Canonical JSON for the ``hyperpod_overrides`` context and the resume identity."""
    return json.dumps(RELEASE_HYPERPOD_OVERRIDES, sort_keys=True, separators=(",", ":"))


def effective_hyperpod_config(ctx: RunContext, region: str) -> dict[str, Any]:
    """The validated ``hyperpod`` block the deploy used for ``region``.

    cdk.json's block with the Region's ``hyperpod_regions`` overlay, then the
    run's ``hyperpod_overrides`` (from the settings, so a sibling harness that
    sets none sees cdk.json alone), validated the way the regional stack
    validates it.
    """
    overrides = ctx.settings.hyperpod_overrides_json or None
    try:
        raw = apply_hyperpod_overrides(raw_hyperpod_config(ctx.cdk_context, region), overrides)
        return validate_hyperpod_config(raw)
    except HyperpodConfigError as exc:
        raise HyperpodValidationError(f"{region}: the hyperpod block is invalid: {exc}") from exc


def effective_hyperpod_enablement(ctx: RunContext) -> dict[str, Any]:
    """Whether the run deploys HyperPod in its probe Region, and what decided it."""
    region = ctx.deployment_regions[0]
    config = effective_hyperpod_config(ctx, region)
    enabled = hyperpod_enabled(config)
    overrides = ctx.settings.hyperpod_overrides_json
    if overrides and json.loads(overrides).get("enabled"):
        source = f"run-override ({HYPERPOD_OVERRIDES_CONTEXT_KEY})"
    else:
        source = "cdk.json hyperpod"
    return {
        "enabled": enabled,
        "source": source,
        "region": region,
        "cluster_name": cluster_name(config, ctx.config.project_name),
        "instance_groups": [
            {
                "name": group["name"],
                "instance_type": group["instance_type"],
                "instance_count": group["instance_count"],
            }
            for group in config["instance_groups"]
        ],
    }


def _check_cluster(ctx: RunContext, region: str, config: dict[str, Any]) -> dict[str, Any]:
    """SageMaker's view of the deployed cluster: InService, every group full, every node Running."""
    name = cluster_name(config, ctx.config.project_name)
    sagemaker = ctx.session.client("sagemaker", region_name=region)
    cluster = sagemaker.describe_cluster(ClusterName=name)
    status = str(cluster.get("ClusterStatus") or "")
    problems: list[str] = []
    if status != _CLUSTER_READY:
        failure = str(cluster.get("FailureMessage") or "")
        problems.append(
            f"cluster {name} is {status or 'unknown'}, not {_CLUSTER_READY}"
            + (f" ({failure})" if failure else "")
        )
    expected = {str(g["name"]): int(g["instance_count"]) for g in config["instance_groups"]}
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in cluster.get("InstanceGroups") or []:
        group_name = str(group.get("InstanceGroupName") or "")
        current = int(group.get("CurrentCount") or 0)
        target = int(group.get("TargetCount") or 0)
        seen.add(group_name)
        groups.append(
            {
                "name": group_name,
                "instance_type": group.get("InstanceType"),
                "current": current,
                "target": target,
                "status": group.get("Status"),
            }
        )
        wanted = expected.get(group_name)
        if wanted is None:
            problems.append(f"instance group {group_name} is not in the block")
        elif current != wanted or target != wanted:
            problems.append(
                f"instance group {group_name} has {current} of {target} instances, "
                f"the block asks for {wanted}"
            )
    for missing in sorted(set(expected) - seen):
        problems.append(f"instance group {missing} is missing from the cluster")

    nodes: list[dict[str, Any]] = []
    for page in sagemaker.get_paginator("list_cluster_nodes").paginate(ClusterName=name):
        for node in page.get("ClusterNodeSummaries") or []:
            node_status = str((node.get("InstanceStatus") or {}).get("Status") or "")
            nodes.append(
                {
                    "instance_id": node.get("InstanceId"),
                    "instance_group": node.get("InstanceGroupName"),
                    "instance_type": node.get("InstanceType"),
                    "status": node_status,
                }
            )
            if node_status != _INSTANCE_READY:
                problems.append(
                    f"instance {node.get('InstanceId')} in {node.get('InstanceGroupName')} is "
                    f"{node_status or 'unknown'}, not {_INSTANCE_READY}"
                )
    if len(nodes) != sum(expected.values()):
        problems.append(
            f"SageMaker lists {len(nodes)} instance(s), the block asks for {sum(expected.values())}"
        )
    if problems:
        raise HyperpodValidationError(f"{region}: " + "; ".join(problems))
    return {
        "name": name,
        "arn": cluster.get("ClusterArn"),
        "status": status,
        "node_recovery": cluster.get("NodeRecovery"),
        "instance_groups": groups,
        "nodes": nodes,
    }


def probe_hyperpod(ctx: RunContext) -> dict[str, Any]:
    """Prove the deployed HyperPod capacity end to end; raises :class:`HyperpodValidationError`."""
    region = ctx.deployment_regions[0]
    config = effective_hyperpod_config(ctx, region)
    evidence: dict[str, Any] = {
        "region": region,
        "cluster": _check_cluster(ctx, region, config),
    }
    lifecycle = _run_api_transport_lifecycle(
        ctx, manifest_filename=_MANIFEST, path=_PATH, marker_prefix=_PATH.upper()
    )
    scheduling = lifecycle.get("scheduling") or {}
    capacity_type = scheduling.get("node_capacity_type")
    if capacity_type != "hyperpod":
        raise HyperpodValidationError(
            f"{region}: the hyperpod probe Job ran on a {capacity_type or 'unknown'} node "
            f"({scheduling.get('node_name') or 'no node reported'}), not a HyperPod node"
        )
    evidence["job"] = lifecycle
    return evidence
