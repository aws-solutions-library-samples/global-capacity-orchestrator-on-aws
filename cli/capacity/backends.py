"""Per-Region capacity of the SageMaker HyperPod and AWS PCS backends.

``gco capacity recommend-region --backend hyperpod|pcs``, ``gco jobs submit-sqs
--auto-region`` (for a manifest routed to HyperPod) and ``gco slurm submit
--auto-region`` rank Regions on what this module reads: whether the regional
stack publishes the backend at all, and how much of it is healthy and free.
Everything comes from the operator's own credentials, the way ``gco stacks
hyperpod status`` and ``gco slurm status`` already read it: CloudFormation
outputs, SageMaker, PCS and EC2, plus the regional API's Slurm status. Nothing
here reaches into a cluster.

What each backend can tell the picker from outside the cluster:

* **HyperPod**: the cluster SageMaker describes under the stack's
  ``HyperpodClusterName`` output: its status and every instance group's
  type, current and target counts. The GPU count of an ``ml.*`` type is
  EC2's description of its EC2 twin (``ml.g5.8xlarge`` -> ``g5.8xlarge``),
  so a Region's HyperPod GPU pool can be sized without a node inventory.
  How many of those GPUs are busy is not visible from here (the CloudWatch
  agent does not run on HyperPod nodes; Kueue's queue usage has no API
  route), so the Region's shared job queue stands in for load.
* **PCS**: the cluster behind the stack's ``PcsClusterId`` output, its
  compute node groups' scaling bounds, the instances PCS has launched for
  them (the ``aws:pcs:compute-node-group-id`` tag) and, through the regional
  API, Slurm's pending and running jobs and idle nodes. Headroom is how many
  more instances the groups may launch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from gco.hyperpod_config import HYPERPOD_OUTPUT_CLUSTER_NAME, INSTANCE_TYPE_PATTERN
from gco.pcs_config import (
    LOGIN_NODE_GROUP_NAME,
    PCS_BACKEND,
    PCS_NODE_GROUP_INSTANCE_TAG,
    PCS_OUTPUT_CLUSTER_ID,
    PCS_OUTPUT_CLUSTER_NAME,
    PCS_OUTPUT_LOGIN_NODE_GROUP_ID,
    PCS_OUTPUT_MODE,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BACKENDS",
    "BACKEND_EKS",
    "BACKEND_HYPERPOD",
    "BACKEND_PCS",
    "HyperpodCapacity",
    "HyperpodInstanceGroup",
    "NoEligibleRegionError",
    "PcsCapacity",
    "PcsNodeGroup",
    "ec2_equivalent",
    "hyperpod_capacity",
    "ml_instance_type",
    "pcs_capacity",
    "sum_gpus",
]

#: The capacity pools a Region recommendation can be asked for. ``eks`` is the
#: Region's EKS cluster (Karpenter / Auto Mode node pools), the default and the
#: only pool the picker knew before the backend-aware ranking existed.
BACKEND_EKS = "eks"
BACKEND_HYPERPOD = "hyperpod"
BACKEND_PCS = PCS_BACKEND
BACKENDS = (BACKEND_EKS, BACKEND_HYPERPOD, BACKEND_PCS)

#: What SageMaker reports for a cluster and an instance group that are fully up.
HYPERPOD_HEALTHY_STATUS = "InService"
#: What PCS reports for a cluster that takes work.
PCS_HEALTHY_STATUS = "ACTIVE"

#: Instance states counted as a node group's launched instances: PCS tags an
#: instance at launch, so a ``pending`` one is capacity already spoken for.
_LAUNCHED_INSTANCE_STATES = ("pending", "running")

ClientFactory = Callable[..., Any]
GpuLookup = Callable[[str], int | None]
SlurmStatusReader = Callable[[], Mapping[str, Any]]


class NoEligibleRegionError(RuntimeError):
    """No deployed Region can take the work; ``ineligible`` says why for each."""

    def __init__(self, message: str, ineligible: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.ineligible = ineligible


def ec2_equivalent(instance_type: str) -> str:
    """The EC2 instance type a SageMaker ``ml.*`` type is built on (``ml.`` stripped)."""
    return instance_type.removeprefix("ml.")


def ml_instance_type(instance_type: str) -> str:
    """The SageMaker spelling of an instance type (``ml.`` prefixed when missing)."""
    return instance_type if instance_type.startswith("ml.") else f"ml.{instance_type}"


# ---------------------------------------------------------------------------
# SageMaker HyperPod
# ---------------------------------------------------------------------------


@dataclass
class HyperpodInstanceGroup:
    """One instance group as SageMaker reports it, with its GPU count per node."""

    name: str
    instance_type: str
    current: int
    target: int
    status: str
    #: GPUs on one node of the group, from EC2's description of the type's EC2
    #: twin; ``None`` when EC2 could not describe it (unknown or not offered).
    gpus_per_instance: int | None = None

    @property
    def gpus(self) -> int | None:
        """GPUs on the nodes present, ``None`` when the per-node count is unknown."""
        return None if self.gpus_per_instance is None else self.current * self.gpus_per_instance


@dataclass
class HyperpodCapacity:
    """What one Region's regional stack and SageMaker say about its HyperPod cluster."""

    #: The regional stack publishes a HyperPod cluster.
    deployed: bool
    cluster_name: str | None = None
    #: SageMaker's ``ClusterStatus``; ``None`` until described.
    status: str | None = None
    #: Cluster and every instance group ``InService`` with every node present.
    healthy: bool = False
    instance_groups: list[HyperpodInstanceGroup] = field(default_factory=list)
    nodes_current: int = 0
    nodes_target: int = 0
    #: GPUs on the nodes present across every group; ``None`` when any group's
    #: per-node count is unknown.
    gpus_total: int | None = None
    #: ``cluster`` when SageMaker could not describe the cluster.
    unavailable_signals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sum_gpus(groups: Iterable[HyperpodInstanceGroup]) -> int | None:
    """GPUs on the present nodes of ``groups``, ``None`` when any count is unknown."""
    total = 0
    for group in groups:
        if group.gpus is None:
            return None
        total += group.gpus
    return total


def hyperpod_capacity(
    region: str,
    outputs: Mapping[str, str] | None,
    *,
    client_factory: ClientFactory,
    gpus_per_instance: GpuLookup,
) -> HyperpodCapacity:
    """The Region's HyperPod cluster as its stack outputs and SageMaker describe it.

    ``outputs`` are the regional stack's CloudFormation outputs, ``None`` when
    they could not be read (reported as the ``cluster`` signal missing rather
    than as "not deployed", so the Region is not silently dropped).
    ``gpus_per_instance`` answers the GPU count of an ``ml.*`` type, or
    ``None`` when it cannot.
    """
    if outputs is None:
        return HyperpodCapacity(
            deployed=False,
            unavailable_signals=["cluster"],
            errors=["Regional stack outputs could not be read"],
        )
    name = outputs.get(HYPERPOD_OUTPUT_CLUSTER_NAME)
    if not name:
        return HyperpodCapacity(deployed=False)
    capacity = HyperpodCapacity(deployed=True, cluster_name=name)
    try:
        sagemaker = client_factory("sagemaker", region_name=region)
        cluster = sagemaker.describe_cluster(ClusterName=name)
    except Exception as exc:
        logger.debug("Failed to describe HyperPod cluster %s in %s: %s", name, region, exc)
        capacity.unavailable_signals.append("cluster")
        capacity.errors.append(f"HyperPod cluster {name} could not be described: {exc}")
        return capacity
    capacity.status = str(cluster.get("ClusterStatus") or "")
    for group in cluster.get("InstanceGroups") or []:
        instance_type = str(group.get("InstanceType") or "")
        capacity.instance_groups.append(
            HyperpodInstanceGroup(
                name=str(group.get("InstanceGroupName") or ""),
                instance_type=instance_type,
                current=int(group.get("CurrentCount") or 0),
                target=int(group.get("TargetCount") or 0),
                status=str(group.get("Status") or ""),
                gpus_per_instance=(
                    gpus_per_instance(instance_type)
                    if INSTANCE_TYPE_PATTERN.match(instance_type)
                    else None
                ),
            )
        )
    capacity.nodes_current = sum(group.current for group in capacity.instance_groups)
    capacity.nodes_target = sum(group.target for group in capacity.instance_groups)
    capacity.gpus_total = sum_gpus(capacity.instance_groups)
    capacity.healthy = capacity.status == HYPERPOD_HEALTHY_STATUS and all(
        group.status == HYPERPOD_HEALTHY_STATUS and group.current >= group.target
        for group in capacity.instance_groups
    )
    return capacity


# ---------------------------------------------------------------------------
# AWS PCS
# ---------------------------------------------------------------------------


@dataclass
class PcsNodeGroup:
    """One compute node group as PCS reports it, with the instances it has launched."""

    id: str
    name: str
    status: str
    instance_types: list[str] = field(default_factory=list)
    min_nodes: int = 0
    max_nodes: int = 0
    #: Instances PCS has launched for the group (pending or running); ``None``
    #: when EC2 could not be asked.
    running: int | None = None


@dataclass
class PcsCapacity:
    """What one Region's regional stack, PCS, EC2 and Slurm say about its PCS cluster."""

    #: The regional stack publishes a PCS cluster.
    deployed: bool
    cluster_id: str | None = None
    cluster_name: str | None = None
    #: ``managed`` (GCO created the cluster) or ``existing`` (operator-run).
    mode: str | None = None
    #: PCS's cluster ``status``; ``None`` until read.
    status: str | None = None
    #: The cluster is ``ACTIVE``.
    healthy: bool = False
    #: Compute node groups, the login node group excluded.
    node_groups: list[PcsNodeGroup] = field(default_factory=list)
    #: Sums over the compute node groups; ``None`` when they could not be read.
    max_nodes: int | None = None
    running_nodes: int | None = None
    #: Instances the compute node groups may still launch (``max - running``).
    headroom: int | None = None
    #: Slurm's view through the regional API; ``None`` when it could not be read.
    slurm_pending_jobs: int | None = None
    slurm_running_jobs: int | None = None
    slurm_idle_nodes: int | None = None
    #: Any of ``cluster`` (PCS), ``nodes`` (node groups or EC2), ``slurm`` (the API).
    unavailable_signals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _compute_node_groups(pcs: Any, cluster_id: str, login_group_id: str) -> list[PcsNodeGroup]:
    """Every compute node group of the cluster except the login node group."""
    groups: list[PcsNodeGroup] = []
    paginator = pcs.get_paginator("list_compute_node_groups")
    for page in paginator.paginate(clusterIdentifier=cluster_id):
        for summary in page.get("computeNodeGroups") or []:
            group_id = str(summary.get("id") or "")
            name = str(summary.get("name") or "")
            if not group_id or group_id == login_group_id or name == LOGIN_NODE_GROUP_NAME:
                continue
            detail = (
                pcs.get_compute_node_group(
                    clusterIdentifier=cluster_id, computeNodeGroupIdentifier=group_id
                ).get("computeNodeGroup")
                or {}
            )
            scaling = detail.get("scalingConfiguration") or {}
            groups.append(
                PcsNodeGroup(
                    id=group_id,
                    name=str(detail.get("name") or name),
                    status=str(detail.get("status") or summary.get("status") or ""),
                    instance_types=[
                        str(config.get("instanceType"))
                        for config in detail.get("instanceConfigs") or []
                        if config.get("instanceType")
                    ],
                    min_nodes=int(scaling.get("minInstanceCount") or 0),
                    max_nodes=int(scaling.get("maxInstanceCount") or 0),
                )
            )
    return groups


def _launched_instances_by_group(ec2: Any, group_ids: list[str]) -> dict[str, int]:
    """How many instances PCS has launched (pending or running) per node group id."""
    counts = dict.fromkeys(group_ids, 0)
    if not group_ids:
        return counts
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {"Name": f"tag:{PCS_NODE_GROUP_INSTANCE_TAG}", "Values": group_ids},
            {"Name": "instance-state-name", "Values": list(_LAUNCHED_INSTANCE_STATES)},
        ]
    ):
        for reservation in page.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                for tag in instance.get("Tags") or []:
                    if tag.get("Key") == PCS_NODE_GROUP_INSTANCE_TAG and tag.get("Value") in counts:
                        counts[str(tag["Value"])] += 1
    return counts


def _state_count(by_state: Any, state: str) -> int:
    """The count the Slurm status reports under one state flag (``0`` when absent)."""
    if not isinstance(by_state, Mapping):
        return 0
    try:
        return int(by_state.get(state) or 0)
    except TypeError, ValueError:
        return 0


def pcs_capacity(
    region: str,
    outputs: Mapping[str, str] | None,
    *,
    client_factory: ClientFactory,
    slurm_status: SlurmStatusReader,
) -> PcsCapacity:
    """The Region's PCS cluster as its stack outputs, PCS, EC2 and Slurm describe it.

    ``outputs`` are the regional stack's CloudFormation outputs (``None`` when
    unreadable, reported as the ``cluster`` signal missing). ``slurm_status``
    fetches ``GET /api/v1/slurm/status?backend=pcs`` from the regional API; the
    three sources are read independently so one failure costs one signal.
    """
    if outputs is None:
        return PcsCapacity(
            deployed=False,
            unavailable_signals=["cluster", "nodes", "slurm"],
            errors=["Regional stack outputs could not be read"],
        )
    cluster_id = outputs.get(PCS_OUTPUT_CLUSTER_ID)
    if not cluster_id:
        return PcsCapacity(deployed=False)
    capacity = PcsCapacity(
        deployed=True,
        cluster_id=cluster_id,
        cluster_name=outputs.get(PCS_OUTPUT_CLUSTER_NAME) or None,
        mode=outputs.get(PCS_OUTPUT_MODE) or None,
    )
    login_group_id = outputs.get(PCS_OUTPUT_LOGIN_NODE_GROUP_ID) or ""

    pcs: Any = None
    try:
        pcs = client_factory("pcs", region_name=region)
        cluster = pcs.get_cluster(clusterIdentifier=cluster_id).get("cluster") or {}
    except Exception as exc:
        logger.debug("Failed to read PCS cluster %s in %s: %s", cluster_id, region, exc)
        capacity.unavailable_signals.append("cluster")
        capacity.errors.append(f"PCS cluster {cluster_id} could not be read: {exc}")
    else:
        capacity.status = str(cluster.get("status") or "")
        capacity.cluster_name = str(cluster.get("name") or capacity.cluster_name or "") or None
        capacity.healthy = capacity.status == PCS_HEALTHY_STATUS

    try:
        if pcs is None:
            pcs = client_factory("pcs", region_name=region)
        groups = _compute_node_groups(pcs, cluster_id, login_group_id)
        launched = _launched_instances_by_group(
            client_factory("ec2", region_name=region), [group.id for group in groups]
        )
    except Exception as exc:
        logger.debug("Failed to read PCS node groups of %s in %s: %s", cluster_id, region, exc)
        capacity.unavailable_signals.append("nodes")
        capacity.errors.append(f"PCS compute node groups of {cluster_id} could not be read: {exc}")
    else:
        for group in groups:
            group.running = launched.get(group.id, 0)
        capacity.node_groups = groups
        capacity.max_nodes = sum(group.max_nodes for group in groups)
        capacity.running_nodes = sum(group.running or 0 for group in groups)
        capacity.headroom = sum(max(0, group.max_nodes - (group.running or 0)) for group in groups)

    try:
        status = slurm_status()
    except Exception as exc:
        logger.debug("Failed to read Slurm status of PCS in %s: %s", region, exc)
        capacity.unavailable_signals.append("slurm")
        capacity.errors.append(f"Slurm status of the PCS backend could not be read: {exc}")
    else:
        jobs = status.get("jobs") if isinstance(status, Mapping) else None
        nodes = status.get("nodes") if isinstance(status, Mapping) else None
        job_states = jobs.get("by_state") if isinstance(jobs, Mapping) else None
        node_states = nodes.get("by_state") if isinstance(nodes, Mapping) else None
        capacity.slurm_pending_jobs = _state_count(job_states, "PENDING")
        capacity.slurm_running_jobs = _state_count(job_states, "RUNNING")
        capacity.slurm_idle_nodes = _state_count(node_states, "IDLE")
    return capacity
