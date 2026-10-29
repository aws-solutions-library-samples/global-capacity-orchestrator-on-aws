"""Schema, defaults and derived views for AWS PCS on GCO (``pcs``).

AWS Parallel Computing Service (PCS) is a managed Slurm: AWS runs the
controller (slurmctld, slurmdbd and, when enabled, slurmrestd) and launches
EC2 compute nodes in the operator's account from launch templates. GCO can
front a PCS cluster with the same ``gco slurm`` CLI, ``/api/v1/slurm/*`` API
and ``slurm_*`` MCP tools it uses for the in-cluster Slinky cluster
(``helm.slurm``): the API talks to PCS's slurmrestd over the regional VPC and
authenticates with a JWT it mints from the signing key PCS keeps in Secrets
Manager. The two backends may be enabled together in one region; callers
name one with ``backend=`` only when both are on.

The ``pcs`` block (global, with ``pcs_regions`` per-region overlays) is the
whole description of what GCO deploys or connects to:

- ``mode: existing`` points GCO at a cluster the operator already runs
  (``cluster_id``). GCO deploys no PCS infrastructure; it resolves the REST
  endpoint and the JWT key at runtime and grants the API pod exactly the two
  reads it needs. The cluster must sit in the regional VPC (or a peered
  range named in ``endpoint_cidr``) with tcp/6820 open to the EKS cluster.
- ``mode: managed`` makes the regional stack create the cluster and its
  compute node groups, queues and login node. Node groups are the PCS
  equivalent of Slinky partitions: each names its EC2 instance types, scaling
  bounds, purchase option (On-Demand, Spot, Capacity Block or an interruptible
  reservation), AMI (the PCS sample AMI, the PCS-ready DLAMI or an AMI id),
  EFA and placement-group settings, and optional Slurm overrides. Queues map
  onto node groups; exactly one is the default. Shared storage follows the
  Slinky block: the regional EFS at ``/home`` and FSx for Lustre at
  ``/scratch`` when the region has it, mounted by PCS's AWS-maintained node
  lifecycle scripts.
- ``identity`` is the POSIX identity the API acts as toward Slurm (PCS does
  not support ``X-SLURM-USER-NAME`` impersonation, so the identity is fixed
  by configuration and the caller's IAM identity travels in each job's
  comment, exactly as with Slinky). It must exist on the compute nodes; the
  default is the sample AMIs' ``ec2-user`` (uid/gid 1000).

This module owns the block's shape so the config loader, the regional stack,
the kubectl-applier manifests, the ``gco stacks pcs`` and ``gco slurm`` CLIs,
the MCP tools and the live validation harness agree on names and defaults.
Like :mod:`gco.slurm_config` it imports nothing from ``aws_cdk``: the CLI runs
without the CDK toolchain installed. It raises :class:`PcsConfigError` (a
``ValueError``); the loader re-raises with its own exception type.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Fixed names shared by the stack, the manifests, the API and the CLI
# ---------------------------------------------------------------------------

#: ``cdk.json`` block key (and ``feature_enabled_overrides`` name).
PCS_FEATURE_KEY = "pcs"
#: Per-region overlays: ``pcs_regions: {"us-east-1": {...}}`` deep-merges over
#: the global block, like ``fsx_lustre_regions``.
PCS_REGIONS_KEY = "pcs_regions"

#: The two Slurm backends the API can front. ``slinky`` is the in-cluster
#: cluster from ``helm.slurm``; ``pcs`` is this block.
SLINKY_BACKEND = "slinky"
PCS_BACKEND = "pcs"
SLURM_BACKENDS: tuple[str, ...] = (SLINKY_BACKEND, PCS_BACKEND)

#: slurmrestd's port on the PCS controller (fixed by the service; plain HTTP,
#: reachable only inside the cluster's VPC).
PCS_RESTAPI_PORT = 6820
#: slurmctld's port on the PCS controller, which also serves the OpenMetrics
#: endpoints (``/metrics/jobs``, ``/metrics/nodes``, ``/metrics/partitions``,
#: ``/metrics/scheduler``) once the cluster's Slurm settings enable them.
PCS_CONTROLLER_PORT = 6817
#: The cluster-level ``slurm.conf`` settings PCS documents for its Slurm
#: metrics (Slurm 25.11+): the OpenMetrics plugin and slurmctld's HTTP
#: listener, which PCS keeps off by default. ``PrivateData`` must stay unset.
PCS_METRICS_SLURM_SETTINGS: dict[str, str] = {
    "MetricsType": "metrics/openmetrics",
    "CommunicationParameters": "enable_http",
}
#: The Prometheus ``job`` label of the managed cluster's metrics scrape (the
#: ``ScrapeConfig``'s ``jobName``); the PCS dashboard and alerts select on
#: it, and the Slinky ones select on theirs, so the two clusters' ``slurm_*``
#: series never mix in a Region that runs both.
PCS_METRICS_JOB = "pcs-slurm-controller"

#: Slurm versions PCS offers whose slurmrestd serves the OpenAPI plugin
#: version every GCO surface addresses (``gco.slurm_config.SLURM_API_VERSION``,
#: ``v0.0.44``): each Slurm release ships its newest ``data_parser`` plugin
#: plus the three before it. 25.11 introduced v0.0.44; 26.05 still serves it;
#: 25.05 stops at v0.0.43 and is rejected by name.
PCS_SUPPORTED_SLURM_VERSIONS: tuple[str, ...] = ("25.11", "26.05")

PCS_MODES: tuple[str, ...] = ("managed", "existing")
#: ``AWS::PCS::Cluster`` ``Size``: SMALL is 32 nodes / 256 jobs, MEDIUM 512 /
#: 8192, LARGE 2048 / 16384.
PCS_CLUSTER_SIZES: tuple[str, ...] = ("SMALL", "MEDIUM", "LARGE")
#: ``CreateComputeNodeGroup.purchaseOption``.
PCS_PURCHASE_OPTIONS: tuple[str, ...] = (
    "ONDEMAND",
    "SPOT",
    "CAPACITY_BLOCK",
    "INTERRUPTIBLE_CAPACITY_RESERVATION",
)
#: ``SpotOptions.allocationStrategy``; PCS defaults to price-capacity-optimized.
PCS_SPOT_ALLOCATION_STRATEGIES: tuple[str, ...] = (
    "lowest-price",
    "capacity-optimized",
    "price-capacity-optimized",
)
#: ``capacity_reservation.type`` and the purchase option each one implies: a
#: targeted ODCR is consumed by On-Demand launches, a Capacity Block needs
#: ``CAPACITY_BLOCK`` (and ``MarketType=capacity-block`` in the launch
#: template), an interruptible ODCR needs its own option.
CAPACITY_RESERVATION_TYPES: dict[str, str] = {
    "odcr": "ONDEMAND",
    "capacity_block": "CAPACITY_BLOCK",
    "interruptible": "INTERRUPTIBLE_CAPACITY_RESERVATION",
}
#: ``ami`` keywords besides a literal ``ami-`` id: the PCS sample AMI
#: (Amazon Linux 2023, demonstration grade) or the PCS-ready DLAMI (Ubuntu
#: 24.04 with NVIDIA drivers, CUDA, EFA and the Lustre client; the production
#: choice for accelerators).
PCS_AMI_CHOICES: tuple[str, ...] = ("sample", "dlami")
PCS_AMI_ARCHITECTURES: tuple[str, ...] = ("x86_64", "arm64")

#: PCS resource names: node groups and queues are 3-25 characters, clusters
#: 3-40, each a letter followed by letters, digits and hyphens, never the
#: ``pcs_`` prefix the service uses for generated ids.
PCS_NAME_PATTERN = re.compile(r"^(?!pcs_)[A-Za-z][A-Za-z0-9-]{2,24}$")
PCS_CLUSTER_NAME_PATTERN = re.compile(r"^(?!pcs_)[A-Za-z][A-Za-z0-9-]{2,39}$")
#: Generated cluster ids (``aws pcs list-clusters``); ``existing`` mode wants
#: the id, not the name, because the IAM grants are written against the ARN.
PCS_CLUSTER_ID_PATTERN = re.compile(r"^pcs_[a-zA-Z0-9]+$")
AMI_ID_PATTERN = re.compile(r"^ami-[a-f0-9]{8,17}$")
CAPACITY_RESERVATION_ID_PATTERN = re.compile(r"^cr-[a-f0-9]{8,17}$")
#: EC2 instance type: family (with an optional dashed variant such as
#: ``p6-b200`` or ``u7i-12tb``) dot size.
INSTANCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)?\.[a-z0-9]+$")
#: Graviton families: a generation digit followed by ``g`` and optional
#: suffix letters (``c7g``, ``c7gn``, ``m6gd``, ``hpc7g``, ``g5g``, ``im4gn``).
#: ``a1`` is the first-generation exception.
_GRAVITON_FAMILY_PATTERN = re.compile(r"^[a-z]+\d+g[a-z]*$")
AVAILABILITY_ZONE_PATTERN = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d[a-z]$")
#: POSIX user names as useradd accepts them.
POSIX_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: Where ``identity.dir`` may point when the managed cluster mounts the
#: shared EFS home: one directory straight under ``/home``, the one the
#: ``ensure-identity-home`` lifecycle action creates on every node.
EFS_HOME_DIR_PATTERN = re.compile(r"^/home/[a-z_][a-z0-9_-]{0,31}$")
#: Slurm parameter names (``Features``, ``MaxTime``, ``Default``...).
SLURM_PARAMETER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
#: gres.conf record attributes PCS accepts in ``gresCustomSettings``.
GRES_ATTRIBUTES: frozenset[str] = frozenset(
    {"AutoDetect", "Name", "Type", "Count", "Cores", "Links", "Flags", "File", "MultipleFiles"}
)

#: ``AccountingRequest.defaultPurgeTimeInDays``: -1 keeps records for the
#: cluster's lifetime; otherwise 1-10000 days (0 is rejected by the API).
PURGE_NEVER = -1
PURGE_DAYS_BOUNDS = (1, 10000)
SCALE_DOWN_IDLE_BOUNDS = (1, 10_000_000)
#: A SMALL cluster caps at 32 compute nodes; LARGE at 2048.
MAX_NODES_LIMIT = 2048

#: Where the regional EFS access point for PCS homes is rooted: PCS homes
#: never mix with the EFS root the ``gco-jobs`` claim exposes to job pods, nor
#: with Slinky's ``slurm/home`` subdirectory.
PCS_HOME_EFS_PATH = "/pcs/home"
PCS_HOME_MOUNT_PATH = "/home"
PCS_SCRATCH_MOUNT_PATH = "/scratch"
#: The managed login node group's PCS name. ``gco slurm shell`` / ``output``
#: find the login node through it (the group of that name, then the EC2
#: instance PCS tags with the group's id), so a compute group may not take it.
LOGIN_NODE_GROUP_NAME = "login"
#: The EC2 tag PCS puts on every instance it launches, valued with the
#: compute node group's id (not its name).
PCS_NODE_GROUP_INSTANCE_TAG = "aws:pcs:compute-node-group-id"

#: CloudFormation output keys the regional stack publishes for the block.
#: ``gco/stacks/pcs_cluster.py`` pins the logical ids to exactly these
#: strings; ``cli/pcs_login.py`` and ``gco stacks pcs status`` read them by
#: key from ``describe-stacks``.
PCS_OUTPUT_CLUSTER_ID = "PcsClusterId"
PCS_OUTPUT_MODE = "PcsMode"
PCS_OUTPUT_RESTAPI_PORT = "PcsRestApiPort"
PCS_OUTPUT_SLURM_VERSION = "PcsSlurmVersion"
PCS_OUTPUT_RESTAPI_MODE = "PcsRestApiMode"
PCS_OUTPUT_CLUSTER_NAME = "PcsClusterName"
PCS_OUTPUT_SECURITY_GROUP_ID = "PcsSecurityGroupId"
PCS_OUTPUT_NODE_ROLE_ARN = "PcsNodeRoleArn"
PCS_OUTPUT_LOGIN_NODE_GROUP_ID = "PcsLoginNodeGroupId"

#: Per-node-group defaults for keys an operator leaves out.
NODE_GROUP_DEFAULTS: dict[str, Any] = {
    # EC2 instance types PCS may launch (one architecture per group; the
    # AMI is single-arch). Capacity Blocks require exactly the reserved type.
    "instance_types": [],
    "min_nodes": 0,
    "max_nodes": 4,
    # null = derive: ONDEMAND, or the option the capacity reservation type
    # implies. An explicit value must agree with the reservation.
    "purchase_option": None,
    # SPOT only; null = PCS's default (price-capacity-optimized).
    "spot_allocation_strategy": None,
    # {"type": "odcr" | "capacity_block" | "interruptible", "id": "cr-..."}
    "capacity_reservation": None,
    # Restrict launches to these AZs (a Capacity Block lives in one); [] =
    # every private subnet of the regional VPC.
    "availability_zones": [],
    # "sample" (PCS sample AMI), "dlami" (PCS-ready DLAMI) or an ami- id.
    "ami": "sample",
    # Elastic Fabric Adapter: one EFA interface per network card, efa_interfaces
    # cards (p5-class instances have 32, most EFA types one; the preflight
    # checks the type's maximum). EFA-capable instance types only, and the
    # group must name exactly one availability zone (EFA traffic stays in
    # one subnet).
    "efa": False,
    "efa_interfaces": 1,
    # A cluster placement group for the group's instances. A placement group
    # lives in one Availability Zone, so the group must name exactly one.
    "placement_group": False,
    # Node-group level slurm.conf overrides (CpuSpecList, Features, Gres,
    # MemSpecLimit, Parameters, RealMemory, Sockets, Weight) and gres.conf
    # records (PCS generates the GPU record itself for GPU instance types;
    # declare one only to replace it).
    "slurm_custom_settings": {},
    "gres_custom_settings": [],
    # Override the cluster's idle scale-down for this group; null = inherit.
    "scale_down_idle_seconds": None,
}

QUEUE_DEFAULTS: dict[str, Any] = {
    "node_groups": [],
    "default": False,
    # Queue-level slurm.conf overrides (MaxTime, DefaultTime, Exclusive,
    # PriorityTier, AllowAccounts, QOS...).
    "slurm_custom_settings": {},
}

#: The block with every knob at its default. PCS is off; when it is on in
#: managed mode, the shipped definitions give one On-Demand CPU node group
#: that scales from zero behind one default queue, accounting with a 30-day
#: purge, the regional EFS as ``/home``, FSx as ``/scratch`` where the region
#: has it, and one t3.medium login node for ``gco slurm shell``/``output``.
PCS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "mode": "managed",
    # existing mode: the cluster to connect to (aws pcs list-clusters).
    "cluster_id": None,
    # The IPv4 range the API pod may reach slurmrestd in; null = the regional
    # VPC CIDR. Set it for a peered VPC in existing mode.
    "endpoint_cidr": None,
    "identity": {
        "name": "ec2-user",
        "uid": 1000,
        "gid": 1000,
        # Supplementary group ids (the primary gid is always included).
        "gids": [],
        # Home directory and default working directory for submitted jobs;
        # null = /home/<name>. Required when storage.home is "none".
        "dir": None,
        "shell": "/bin/bash",
        "gecos": None,
    },
    "managed": {
        # PCS cluster name; null = "<project_name>-pcs". Changing it replaces
        # the cluster.
        "cluster_name": None,
        "slurm_version": "25.11",
        "size": "SMALL",
        "accounting": {
            # slurmdbd (sacct, `gco slurm accounting`); purge_days -1 = never.
            "enabled": True,
            "purge_days": 30,
        },
        # Idle time before PCS scales a node group's instances down.
        "scale_down_idle_seconds": 600,
        "storage": {
            # /home on compute and login nodes: "efs" (the regional file
            # system, through a PCS-only access point) or "none".
            "home": "efs",
            # /scratch: "auto" (FSx for Lustre when fsx_lustre is enabled in
            # the region), "fsx" (require it) or "none".
            "scratch": "auto",
        },
        "login": {
            # One always-on On-Demand login node reached over SSM: the target
            # of `gco slurm shell` and the reader for `gco slurm output`.
            "enabled": True,
            "instance_type": "t3.medium",
            "ami": "sample",
            "availability_zones": [],
        },
        "node_groups": [
            {
                "name": "gco-compute",
                "instance_types": ["c6i.large"],
                "min_nodes": 0,
                "max_nodes": 4,
            }
        ],
        "queues": [{"name": "gco", "node_groups": ["gco-compute"], "default": True}],
        "metrics": {
            # Scrape the controller's Slurm metrics into the in-cluster
            # Prometheus beside Slinky's.
            "enabled": False,
        },
        "lifecycle_logs": {
            # Forward each node's lifecycle-action logs to CloudWatch Logs
            # (/aws/pcs/<cluster id>/lifecycle) with PCS's maintained script.
            "enabled": True,
        },
        # Cluster-level slurm.conf / slurmdbd.conf / cgroup.conf overrides
        # (parameter -> value).
        "slurm_custom_settings": {},
        "slurmdbd_custom_settings": {},
        "cgroup_custom_settings": {},
        # What happens to the cluster, node groups and queues on destroy.
        "removal_policy": "destroy",
    },
}


class PcsConfigError(ValueError):
    """Raised when the ``pcs`` block is malformed."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _deep_merge(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge mappings; lists and scalars replace. ``None`` over a mapping keeps it."""
    merged: dict[str, Any] = copy.deepcopy(dict(defaults))
    for key, value in overrides.items():
        if value is None and isinstance(merged.get(key), Mapping):
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _reject_unknown_keys(block: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    allowed_set = set(allowed)
    unknown = sorted(str(key) for key in block if key not in allowed_set)
    if unknown:
        raise PcsConfigError(
            f"{path} contains unknown key(s): {', '.join(unknown)}; "
            f"allowed keys: {', '.join(sorted(allowed_set))}"
        )


def _require_object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PcsConfigError(f"{path} must be an object, got {value!r}")
    return value


def _require_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise PcsConfigError(f"{path} must be a boolean, got {value!r}")
    return value


def _require_int(value: object, bounds: tuple[int, int], path: str) -> int:
    """An exact integer (``bool`` rejected) inside the inclusive ``bounds``."""
    low, high = bounds
    if type(value) is not int or not low <= value <= high:
        raise PcsConfigError(f"{path} must be an integer between {low} and {high}, got {value!r}")
    return value


def _require_choice(value: object, choices: Iterable[str], path: str) -> str:
    options = tuple(choices)
    if not isinstance(value, str) or value not in options:
        raise PcsConfigError(f"{path} must be one of {', '.join(options)}, got {value!r}")
    return value


def _require_nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PcsConfigError(f"{path} must be a non-empty string, got {value!r}")
    return value.strip()


def _require_string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PcsConfigError(f"{path} must be a list of non-empty strings, got {value!r}")
    return [item.strip() for item in value]


def _validate_custom_settings(raw: object, path: str) -> dict[str, str]:
    """A ``parameter -> value`` mapping rendered as PCS custom settings."""
    block = _require_object(raw, path)
    settings: dict[str, str] = {}
    for key, value in block.items():
        name = str(key)
        if not SLURM_PARAMETER_PATTERN.match(name):
            raise PcsConfigError(f"{path} key {name!r} is not a Slurm parameter name")
        if type(value) is bool or not isinstance(value, str | int | float):
            raise PcsConfigError(f"{path}.{name} must be a string or a number, got {value!r}")
        text = str(value).strip()
        if not text or "\n" in text:
            raise PcsConfigError(
                f"{path}.{name} must be a non-empty single-line value, got {value!r}"
            )
        settings[name] = text
    return settings


def _validate_gres_records(raw: object, path: str) -> list[dict[str, str]]:
    """``gres.conf`` records: a list of attribute -> value mappings."""
    if not isinstance(raw, list):
        raise PcsConfigError(f"{path} must be a list of gres.conf records, got {raw!r}")
    records: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        record_path = f"{path}[{index}]"
        block = _require_object(item, record_path)
        if not block:
            raise PcsConfigError(f"{record_path} must not be empty")
        record: dict[str, str] = {}
        for key, value in block.items():
            name = str(key)
            if name not in GRES_ATTRIBUTES:
                raise PcsConfigError(
                    f"{record_path} has unknown gres.conf attribute {name!r}; "
                    f"allowed: {', '.join(sorted(GRES_ATTRIBUTES))}"
                )
            if type(value) is bool or not isinstance(value, str | int):
                raise PcsConfigError(
                    f"{record_path}.{name} must be a string or an integer, got {value!r}"
                )
            record[name] = str(value).strip()
        if "Name" not in record and "AutoDetect" not in record:
            raise PcsConfigError(f"{record_path} must declare Name or AutoDetect")
        records.append(record)
    return records


def instance_architecture(instance_type: str) -> str:
    """``"arm64"`` for Graviton families, ``"x86_64"`` otherwise.

    PCS launches one AMI per node group and an AMI has one architecture, so
    the group's instance types must agree. The rule is the family naming
    convention (a ``g`` right after the generation digit), which is what the
    AMI lookup and the preflight compare against ``describe_instance_types``.
    """
    family = instance_type.split(".", 1)[0]
    if family == "a1" or _GRAVITON_FAMILY_PATTERN.match(family):
        return "arm64"
    return "x86_64"


def _validate_instance_types(raw: object, path: str) -> tuple[list[str], str]:
    types = _require_string_list(raw, path)
    if not types:
        raise PcsConfigError(f"{path} must name at least one EC2 instance type")
    for instance_type in types:
        if not INSTANCE_TYPE_PATTERN.match(instance_type):
            raise PcsConfigError(f"{path} has an invalid EC2 instance type {instance_type!r}")
    if len(set(types)) != len(types):
        raise PcsConfigError(f"{path} lists an instance type twice")
    architectures = {instance_architecture(t) for t in types}
    if len(architectures) != 1:
        raise PcsConfigError(
            f"{path} mixes architectures ({', '.join(sorted(architectures))}); a node group "
            "launches one AMI, so every instance type must share its architecture"
        )
    return types, architectures.pop()


def _validate_ami(value: object, path: str) -> str:
    if not isinstance(value, str) or not (value in PCS_AMI_CHOICES or AMI_ID_PATTERN.match(value)):
        raise PcsConfigError(
            f"{path} must be 'sample', 'dlami' or an AMI id (ami-...), got {value!r}"
        )
    return value


def _validate_availability_zones(raw: object, path: str) -> list[str]:
    zones = _require_string_list(raw, path)
    for zone in zones:
        if not AVAILABILITY_ZONE_PATTERN.match(zone):
            raise PcsConfigError(f"{path} has an invalid availability zone {zone!r}")
    if len(set(zones)) != len(zones):
        raise PcsConfigError(f"{path} lists an availability zone twice")
    return zones


def _validate_capacity_reservation(raw: object, path: str) -> dict[str, str] | None:
    if raw is None:
        return None
    block = _require_object(raw, path)
    _reject_unknown_keys(block, ("type", "id"), path)
    kind = _require_choice(block.get("type"), CAPACITY_RESERVATION_TYPES, f"{path}.type")
    reservation_id = _require_nonempty_string(block.get("id"), f"{path}.id")
    if not CAPACITY_RESERVATION_ID_PATTERN.match(reservation_id):
        raise PcsConfigError(
            f"{path}.id must be a capacity reservation id (cr-...), got {reservation_id!r}"
        )
    return {"type": kind, "id": reservation_id}


def _validate_node_group(raw: object, index: int) -> dict[str, Any]:
    path = f"pcs.managed.node_groups[{index}]"
    block = _require_object(raw, path)
    _reject_unknown_keys(block, (*NODE_GROUP_DEFAULTS.keys(), "name"), path)
    name = _require_nonempty_string(block.get("name"), f"{path}.name")
    if not PCS_NAME_PATTERN.match(name):
        raise PcsConfigError(
            f"{path}.name {name!r} is not a PCS compute node group name: 3-25 characters, a "
            "letter followed by letters, digits and hyphens"
        )
    group = _deep_merge(NODE_GROUP_DEFAULTS, {k: v for k, v in block.items() if k != "name"})
    group["name"] = name
    path = f"pcs.managed.node_groups.{name}"

    group["instance_types"], group["_architecture"] = _validate_instance_types(
        group["instance_types"], f"{path}.instance_types"
    )
    min_nodes = _require_int(group["min_nodes"], (0, MAX_NODES_LIMIT), f"{path}.min_nodes")
    max_nodes = _require_int(group["max_nodes"], (1, MAX_NODES_LIMIT), f"{path}.max_nodes")
    if min_nodes > max_nodes:
        raise PcsConfigError(
            f"{path}.min_nodes must not exceed max_nodes, got {min_nodes} > {max_nodes}"
        )

    reservation = _validate_capacity_reservation(
        group["capacity_reservation"], f"{path}.capacity_reservation"
    )
    group["capacity_reservation"] = reservation
    implied = CAPACITY_RESERVATION_TYPES[reservation["type"]] if reservation else "ONDEMAND"
    purchase = group["purchase_option"]
    if purchase is None:
        purchase = implied
    else:
        purchase = _require_choice(purchase, PCS_PURCHASE_OPTIONS, f"{path}.purchase_option")
        if reservation and purchase != implied:
            raise PcsConfigError(
                f"{path}.purchase_option is {purchase!r} but capacity_reservation.type "
                f"{reservation['type']!r} requires {implied!r} (or leave purchase_option unset)"
            )
        if purchase in ("CAPACITY_BLOCK", "INTERRUPTIBLE_CAPACITY_RESERVATION") and not reservation:
            raise PcsConfigError(
                f"{path}.purchase_option {purchase!r} needs capacity_reservation to name the "
                "reservation the launch template targets"
            )
    group["purchase_option"] = purchase
    if reservation and reservation["type"] == "capacity_block" and len(group["instance_types"]) > 1:
        raise PcsConfigError(
            f"{path}.instance_types must name exactly the Capacity Block's instance type when "
            "capacity_reservation.type is capacity_block"
        )
    group["availability_zones"] = _validate_availability_zones(
        group["availability_zones"], f"{path}.availability_zones"
    )
    if reservation and len(group["availability_zones"]) != 1:
        raise PcsConfigError(
            f"{path}.capacity_reservation needs exactly one availability zone in "
            f"availability_zones (a reservation lives in one zone; the node group's subnets must "
            f"match it), got {group['availability_zones'] or 'none'}"
        )
    strategy = group["spot_allocation_strategy"]
    if strategy is not None:
        _require_choice(
            strategy, PCS_SPOT_ALLOCATION_STRATEGIES, f"{path}.spot_allocation_strategy"
        )
        if purchase != "SPOT":
            raise PcsConfigError(
                f"{path}.spot_allocation_strategy applies to SPOT node groups only "
                f"(purchase_option is {purchase!r})"
            )

    group["ami"] = _validate_ami(group["ami"], f"{path}.ami")
    _require_bool(group["efa"], f"{path}.efa")
    _require_int(group["efa_interfaces"], (1, 64), f"{path}.efa_interfaces")
    _require_bool(group["placement_group"], f"{path}.placement_group")
    if group["efa"] and len(group["availability_zones"]) != 1:
        raise PcsConfigError(
            f"{path}.efa needs exactly one availability zone in availability_zones (EFA "
            f"OS-bypass traffic does not cross subnets, and the regional VPC has one private "
            f"subnet per zone), got {group['availability_zones'] or 'none'}"
        )
    if group["placement_group"] and len(group["availability_zones"]) != 1:
        raise PcsConfigError(
            f"{path}.placement_group needs exactly one availability zone in "
            f"availability_zones (a cluster placement group lives in one zone), got "
            f"{group['availability_zones'] or 'none'}"
        )
    group["slurm_custom_settings"] = _validate_custom_settings(
        group["slurm_custom_settings"], f"{path}.slurm_custom_settings"
    )
    group["gres_custom_settings"] = _validate_gres_records(
        group["gres_custom_settings"], f"{path}.gres_custom_settings"
    )
    if group["scale_down_idle_seconds"] is not None:
        _require_int(
            group["scale_down_idle_seconds"],
            SCALE_DOWN_IDLE_BOUNDS,
            f"{path}.scale_down_idle_seconds",
        )
    return group


def _validate_node_groups(raw: object) -> list[dict[str, Any]]:
    path = "pcs.managed.node_groups"
    if not isinstance(raw, list):
        raise PcsConfigError(f"{path} must be a list of node groups, got {raw!r}")
    if not raw:
        raise PcsConfigError(f"{path} must declare at least one compute node group")
    groups = [_validate_node_group(item, index) for index, item in enumerate(raw)]
    names = [group["name"] for group in groups]
    if len(set(names)) != len(names):
        raise PcsConfigError(f"{path} names a node group twice")
    if LOGIN_NODE_GROUP_NAME in names:
        raise PcsConfigError(
            f"{path} must not name a group {LOGIN_NODE_GROUP_NAME!r}; the login node group is "
            "managed"
        )
    return groups


def _validate_queue(raw: object, index: int, node_group_names: set[str]) -> dict[str, Any]:
    path = f"pcs.managed.queues[{index}]"
    block = _require_object(raw, path)
    _reject_unknown_keys(block, (*QUEUE_DEFAULTS.keys(), "name"), path)
    name = _require_nonempty_string(block.get("name"), f"{path}.name")
    if not PCS_NAME_PATTERN.match(name):
        raise PcsConfigError(
            f"{path}.name {name!r} is not a PCS queue name: 3-25 characters, a letter followed "
            "by letters, digits and hyphens"
        )
    queue = _deep_merge(QUEUE_DEFAULTS, {k: v for k, v in block.items() if k != "name"})
    queue["name"] = name
    path = f"pcs.managed.queues.{name}"
    groups = _require_string_list(queue["node_groups"], f"{path}.node_groups")
    if not groups:
        raise PcsConfigError(f"{path}.node_groups must name at least one compute node group")
    unknown = sorted(set(groups) - node_group_names)
    if unknown:
        raise PcsConfigError(
            f"{path}.node_groups names undeclared node group(s): {', '.join(unknown)}"
        )
    if len(set(groups)) != len(groups):
        raise PcsConfigError(f"{path}.node_groups lists a node group twice")
    queue["node_groups"] = groups
    _require_bool(queue["default"], f"{path}.default")
    queue["slurm_custom_settings"] = _validate_custom_settings(
        queue["slurm_custom_settings"], f"{path}.slurm_custom_settings"
    )
    if "Default" in queue["slurm_custom_settings"]:
        raise PcsConfigError(
            f"{path}.slurm_custom_settings.Default is managed by the queue's 'default' flag"
        )
    return queue


def _validate_queues(raw: object, node_group_names: set[str]) -> list[dict[str, Any]]:
    path = "pcs.managed.queues"
    if not isinstance(raw, list):
        raise PcsConfigError(f"{path} must be a list of queues, got {raw!r}")
    if not raw:
        raise PcsConfigError(f"{path} must declare at least one queue")
    queues = [_validate_queue(item, index, node_group_names) for index, item in enumerate(raw)]
    names = [queue["name"] for queue in queues]
    if len(set(names)) != len(names):
        raise PcsConfigError(f"{path} names a queue twice")
    defaults = [queue["name"] for queue in queues if queue["default"]]
    if len(defaults) != 1:
        raise PcsConfigError(
            f"{path} must mark exactly one queue default: true (sbatch without --partition "
            f"goes there), found {defaults or 'none'}"
        )
    return queues


def _validate_identity(
    raw: object, *, home_required: bool, home_on_efs: bool = False
) -> dict[str, Any]:
    path = "pcs.identity"
    block = _require_object(raw, path)
    _reject_unknown_keys(block, PCS_DEFAULTS["identity"].keys(), path)
    identity = _deep_merge(PCS_DEFAULTS["identity"], block)
    name = _require_nonempty_string(identity["name"], f"{path}.name")
    if not POSIX_NAME_PATTERN.match(name):
        raise PcsConfigError(f"{path}.name must be a POSIX user name, got {name!r}")
    identity["name"] = name
    identity["uid"] = _require_int(identity["uid"], (0, 2**31 - 1), f"{path}.uid")
    identity["gid"] = _require_int(identity["gid"], (0, 2**31 - 1), f"{path}.gid")
    gids = identity["gids"]
    if not isinstance(gids, list):
        raise PcsConfigError(f"{path}.gids must be a list of group ids, got {gids!r}")
    identity["gids"] = [
        _require_int(gid, (0, 2**31 - 1), f"{path}.gids[{index}]") for index, gid in enumerate(gids)
    ]
    if identity["dir"] is None:
        if home_required:
            raise PcsConfigError(
                f"{path}.dir is required when pcs.managed.storage.home is 'none': without the "
                "shared /home the default /home/<name> cannot be assumed to exist on every node"
            )
        identity["dir"] = f"{PCS_HOME_MOUNT_PATH}/{name}"
    else:
        directory = _require_nonempty_string(identity["dir"], f"{path}.dir")
        if not directory.startswith("/"):
            raise PcsConfigError(f"{path}.dir must be an absolute path, got {directory!r}")
        if home_on_efs and not EFS_HOME_DIR_PATTERN.match(directory):
            raise PcsConfigError(
                f"{path}.dir must be a directory straight under {PCS_HOME_MOUNT_PATH} (the "
                f"shared EFS home the managed cluster mounts, where the node lifecycle action "
                f"creates it), got {directory!r}"
            )
        identity["dir"] = directory
    shell = _require_nonempty_string(identity["shell"], f"{path}.shell")
    if not shell.startswith("/"):
        raise PcsConfigError(f"{path}.shell must be an absolute path, got {shell!r}")
    identity["shell"] = shell
    if identity["gecos"] is not None and not isinstance(identity["gecos"], str):
        raise PcsConfigError(f"{path}.gecos must be a string, got {identity['gecos']!r}")
    return identity


def _validate_managed(raw: object, *, fsx_enabled: bool) -> dict[str, Any]:
    path = "pcs.managed"
    block = _require_object(raw, path)
    _reject_unknown_keys(block, PCS_DEFAULTS["managed"].keys(), path)
    # ``block`` is already merged over the defaults (a null sub-block kept
    # them), so every sub-block is present: an object, or the scalar an
    # operator mistakenly wrote in its place.
    for key in ("accounting", "storage", "login", "metrics", "lifecycle_logs"):
        sub_block = _require_object(block[key], f"{path}.{key}")
        _reject_unknown_keys(sub_block, PCS_DEFAULTS["managed"][key].keys(), f"{path}.{key}")
    managed = _deep_merge(PCS_DEFAULTS["managed"], block)

    if managed["cluster_name"] is not None:
        cluster_name = _require_nonempty_string(managed["cluster_name"], f"{path}.cluster_name")
        if not PCS_CLUSTER_NAME_PATTERN.match(cluster_name):
            raise PcsConfigError(
                f"{path}.cluster_name {cluster_name!r} is not a PCS cluster name: 3-40 "
                "characters, a letter followed by letters, digits and hyphens"
            )
        managed["cluster_name"] = cluster_name
    _require_choice(managed["slurm_version"], PCS_SUPPORTED_SLURM_VERSIONS, f"{path}.slurm_version")
    _require_choice(managed["size"], PCS_CLUSTER_SIZES, f"{path}.size")

    accounting = managed["accounting"]
    _require_bool(accounting["enabled"], f"{path}.accounting.enabled")
    purge = accounting["purge_days"]
    if purge != PURGE_NEVER:
        _require_int(purge, PURGE_DAYS_BOUNDS, f"{path}.accounting.purge_days")
    _require_int(
        managed["scale_down_idle_seconds"],
        SCALE_DOWN_IDLE_BOUNDS,
        f"{path}.scale_down_idle_seconds",
    )

    storage = managed["storage"]
    _require_choice(storage["home"], ("efs", "none"), f"{path}.storage.home")
    _require_choice(storage["scratch"], ("auto", "fsx", "none"), f"{path}.storage.scratch")
    if storage["scratch"] == "fsx" and not fsx_enabled:
        raise PcsConfigError(
            f"{path}.storage.scratch is 'fsx' but fsx_lustre is not enabled for this "
            "deployment; enable fsx_lustre or use 'auto'"
        )

    login = managed["login"]
    _require_bool(login["enabled"], f"{path}.login.enabled")
    instance_type = _require_nonempty_string(login["instance_type"], f"{path}.login.instance_type")
    if not INSTANCE_TYPE_PATTERN.match(instance_type):
        raise PcsConfigError(
            f"{path}.login.instance_type has an invalid EC2 instance type {instance_type!r}"
        )
    login["instance_type"] = instance_type
    login["ami"] = _validate_ami(login["ami"], f"{path}.login.ami")
    login["availability_zones"] = _validate_availability_zones(
        login["availability_zones"], f"{path}.login.availability_zones"
    )
    login["_architecture"] = instance_architecture(instance_type)

    managed["node_groups"] = _validate_node_groups(managed["node_groups"])
    managed["queues"] = _validate_queues(
        managed["queues"], {group["name"] for group in managed["node_groups"]}
    )
    _require_bool(managed["metrics"]["enabled"], f"{path}.metrics.enabled")
    _require_bool(managed["lifecycle_logs"]["enabled"], f"{path}.lifecycle_logs.enabled")
    for key in ("slurm_custom_settings", "slurmdbd_custom_settings", "cgroup_custom_settings"):
        managed[key] = _validate_custom_settings(managed[key], f"{path}.{key}")
    if managed["slurmdbd_custom_settings"] and not accounting["enabled"]:
        raise PcsConfigError(
            f"{path}.slurmdbd_custom_settings needs accounting.enabled: slurmdbd only runs "
            "with accounting on"
        )
    if managed["metrics"]["enabled"]:
        _check_metrics_settings(managed["slurm_custom_settings"], f"{path}.slurm_custom_settings")
    _require_choice(managed["removal_policy"], ("destroy", "retain"), f"{path}.removal_policy")
    return managed


def _check_metrics_settings(settings: Mapping[str, str], path: str) -> None:
    """``metrics.enabled`` needs the three conditions PCS documents for Slurm metrics."""
    if "PrivateData" in settings:
        raise PcsConfigError(
            f"{path}.PrivateData cannot be set with metrics.enabled: PCS serves the Slurm "
            "metrics endpoints only when PrivateData is unset"
        )
    metrics_type = settings.get("MetricsType")
    if metrics_type is not None and metrics_type != PCS_METRICS_SLURM_SETTINGS["MetricsType"]:
        raise PcsConfigError(
            f"{path}.MetricsType is {metrics_type!r} but metrics.enabled needs "
            f"{PCS_METRICS_SLURM_SETTINGS['MetricsType']!r} (the only plugin Slurm ships); "
            "drop the setting"
        )


def managed_slurm_custom_settings(config: Mapping[str, Any]) -> dict[str, str]:
    """The cluster-level ``slurm.conf`` settings the managed cluster is created with.

    The operator's ``managed.slurm_custom_settings`` plus, with
    ``metrics.enabled``, the settings PCS documents for its Slurm metrics:
    ``MetricsType`` set, and ``enable_http`` merged into whatever
    ``CommunicationParameters`` the operator already lists (a comma-separated
    Slurm parameter list), so a block that sets other communication flags
    keeps them.
    """
    managed = config["managed"]
    settings = dict(managed["slurm_custom_settings"])
    if not managed["metrics"]["enabled"]:
        return settings
    settings["MetricsType"] = PCS_METRICS_SLURM_SETTINGS["MetricsType"]
    flags = [
        flag.strip()
        for flag in str(settings.get("CommunicationParameters") or "").split(",")
        if flag.strip()
    ]
    if PCS_METRICS_SLURM_SETTINGS["CommunicationParameters"] not in flags:
        flags.append(PCS_METRICS_SLURM_SETTINGS["CommunicationParameters"])
    settings["CommunicationParameters"] = ",".join(flags)
    return settings


def metrics_enabled(config: Mapping[str, Any]) -> bool:
    """Whether the Region scrapes the managed cluster's Slurm metrics (managed mode only)."""
    return bool(
        pcs_enabled(config) and is_managed(config) and config["managed"]["metrics"]["enabled"]
    )


def validate_pcs_config(raw: object, *, fsx_enabled: bool = False) -> dict[str, Any]:
    """Validate the raw ``pcs`` block and return it with defaults filled.

    ``None`` means "absent" and yields the defaults (PCS off). Unknown keys
    fail by name at every level. ``fsx_enabled`` says whether the region (or,
    for the global block, every region) has FSx for Lustre, which
    ``managed.storage.scratch: fsx`` requires.

    The managed block is validated even when ``mode`` is ``existing`` so a
    later switch of mode never trips over a typo that was silently carried,
    and because the defaults are always well-formed that costs nothing. The
    returned mapping carries one derived, underscore-prefixed value per node
    group and for the login node (``_architecture``) for the AMI lookup.
    """
    if raw is None:
        raw = {}
    block = _require_object(raw, "pcs")
    _reject_unknown_keys(block, PCS_DEFAULTS.keys(), "pcs")
    config = _deep_merge(PCS_DEFAULTS, block)

    _require_bool(config["enabled"], "pcs.enabled")
    _require_choice(config["mode"], PCS_MODES, "pcs.mode")
    cluster_id = config["cluster_id"]
    if cluster_id is not None:
        cluster_id = _require_nonempty_string(cluster_id, "pcs.cluster_id")
        if not PCS_CLUSTER_ID_PATTERN.match(cluster_id):
            raise PcsConfigError(
                f"pcs.cluster_id must be a PCS cluster id (pcs_...; see `aws pcs list-clusters`), "
                f"got {cluster_id!r}"
            )
        config["cluster_id"] = cluster_id
    if config["mode"] == "existing" and cluster_id is None:
        raise PcsConfigError("pcs.cluster_id is required when pcs.mode is 'existing'")
    if config["endpoint_cidr"] is not None:
        cidr = _require_nonempty_string(config["endpoint_cidr"], "pcs.endpoint_cidr")
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise PcsConfigError(f"pcs.endpoint_cidr {cidr!r} is not a CIDR block") from exc
        if network.version != 4:
            raise PcsConfigError(f"pcs.endpoint_cidr {cidr!r} must be IPv4")
        config["endpoint_cidr"] = str(network)

    config["managed"] = _validate_managed(config["managed"], fsx_enabled=fsx_enabled)
    home = config["managed"]["storage"]["home"]
    managed = config["mode"] == "managed"
    config["identity"] = _validate_identity(
        config["identity"],
        home_required=managed and home == "none",
        home_on_efs=managed and home == "efs",
    )
    return config


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


def pcs_enabled(config: Mapping[str, Any]) -> bool:
    """True when the block turns PCS on (after any run-scoped override)."""
    return bool(config.get("enabled"))


def is_managed(config: Mapping[str, Any]) -> bool:
    """True when GCO provisions the cluster (``mode: managed``)."""
    return config.get("mode") == "managed"


def node_group_names(config: Mapping[str, Any]) -> list[str]:
    """The compute node groups in declaration order (without the login group)."""
    return [group["name"] for group in config["managed"]["node_groups"]]


def default_queue(config: Mapping[str, Any]) -> str:
    """The queue ``sbatch`` without ``--partition`` lands in."""
    return str(next(queue["name"] for queue in config["managed"]["queues"] if queue["default"]))


def managed_cluster_name(config: Mapping[str, Any], project_name: str) -> str:
    """The PCS cluster name for managed mode: configured, else ``<project>-pcs``."""
    configured = config["managed"].get("cluster_name")
    if configured:
        return str(configured)
    name = f"{project_name}-pcs"
    if not PCS_CLUSTER_NAME_PATTERN.match(name):
        raise PcsConfigError(
            f"derived PCS cluster name {name!r} is not valid for PCS (3-40 characters, a letter "
            "followed by letters, digits and hyphens); set pcs.managed.cluster_name"
        )
    return name


def scratch_backend(config: Mapping[str, Any], *, fsx_enabled: bool) -> str:
    """``"fsx"`` or ``"none"``: what ``managed.storage.scratch`` resolves to."""
    scratch = str(config["managed"]["storage"]["scratch"])
    if scratch == "auto":
        return "fsx" if fsx_enabled else "none"
    return scratch


def pcs_cluster_arn(*, partition: str, region: str, account: str, cluster_id: str) -> str:
    """The cluster ARN the IAM grants are written against."""
    return f"arn:{partition}:pcs:{region}:{account}:cluster/{cluster_id}"


def enabled_slurm_backends(*, slinky: bool, pcs: bool) -> list[str]:
    """The backends the API pod serves, in canonical order (``GCO_SLURM_BACKENDS``)."""
    backends: list[str] = []
    if slinky:
        backends.append(SLINKY_BACKEND)
    if pcs:
        backends.append(PCS_BACKEND)
    return backends


# ---------------------------------------------------------------------------
# kubectl-applier replacements
# ---------------------------------------------------------------------------

#: Every ``{{TOKEN}}`` the PCS integration puts into the kubectl-applier
#: manifests. Two kinds, as with the Slurm tokens:
#:
#: * Gated: ``{{PCS_ENABLED}}`` and ``{{PCS_RESTAPI_CIDR}}`` appear only in
#:   post-helm-pcs-network.yaml; when PCS is off they are not emitted, the
#:   applier skips the file and ``_FEATURE_RESOURCE_INVENTORY`` prunes the
#:   NetworkPolicy an earlier deploy applied. ``{{PCS_METRICS_ENABLED}}`` and
#:   ``{{PCS_CONTROLLER_ADDRESS}}`` gate post-helm-pcs-observability.yaml the
#:   same way, on ``managed.metrics.enabled`` (managed mode only).
#: * Always emitted: ``{{SLURM_BACKENDS}}``, ``{{PCS_CLUSTER_ID}}``,
#:   ``{{PCS_IDENTITY_JSON}}`` and ``{{PCS_DEFAULT_PARTITION}}`` sit in the
#:   core manifest-processor Deployment (31-manifest-processor.yaml), where a
#:   leftover placeholder would make the applier skip the whole file, exactly
#:   like ``{{SLURM_API_ENABLED}}`` and ``{{TRACING_ENABLED}}``. They are
#:   empty strings when PCS is off. ``{{SLURM_BACKENDS}}`` is the combined
#:   list for both backends, so it is computed from both enablement flags.
PCS_MANIFEST_TOKENS: tuple[str, ...] = (
    "{{PCS_ENABLED}}",
    "{{PCS_RESTAPI_CIDR}}",
    "{{PCS_METRICS_ENABLED}}",
    "{{PCS_CONTROLLER_ADDRESS}}",
    "{{SLURM_BACKENDS}}",
    "{{PCS_CLUSTER_ID}}",
    "{{PCS_IDENTITY_JSON}}",
    "{{PCS_DEFAULT_PARTITION}}",
)
#: The tokens that must be emitted on every deploy (see above).
PCS_ALWAYS_EMITTED_TOKENS: frozenset[str] = frozenset(
    {
        "{{SLURM_BACKENDS}}",
        "{{PCS_CLUSTER_ID}}",
        "{{PCS_IDENTITY_JSON}}",
        "{{PCS_DEFAULT_PARTITION}}",
    }
)


def _yaml_single_quoted_body(text: str) -> str:
    """Escape ``text`` for the inside of a YAML single-quoted scalar.

    The identity JSON carries double quotes, so the manifest wraps the
    placeholder in single quotes; the only escape a single-quoted YAML scalar
    needs is a doubled single quote.
    """
    return text.replace("'", "''")


def compute_pcs_replacements(
    config: Mapping[str, Any],
    *,
    enabled: bool,
    slinky_enabled: bool,
    cluster_id: str,
    vpc_cidr: str,
    controller_address: str = "",
) -> dict[str, str]:
    """Build the kubectl-applier replacements for the PCS integration.

    ``enabled`` is the effective PCS enablement (cdk.json toggle or the
    run-scoped ``feature_enabled_overrides``), ``slinky_enabled`` the
    effective Slinky chart enablement (both feed ``{{SLURM_BACKENDS}}``).
    ``cluster_id`` is the PCS cluster id, in managed mode a CloudFormation
    token for the created cluster's id, so the value is passed through
    untouched. ``vpc_cidr`` is the regional VPC's IPv4 range; ``endpoint_cidr``
    in the block overrides it for a peered cluster. ``controller_address`` is
    the controller's ``ip:6817`` (a deploy-time token from the stack's
    controller lookup) and is only consulted when the managed cluster's
    metrics are on; the observability manifest's tokens are then emitted so
    the ``ScrapeConfig``, dashboard and alerts apply.
    """
    backends = enabled_slurm_backends(slinky=slinky_enabled, pcs=enabled)
    replacements = {
        "{{SLURM_BACKENDS}}": ",".join(backends),
        "{{PCS_CLUSTER_ID}}": "",
        "{{PCS_IDENTITY_JSON}}": "",
        "{{PCS_DEFAULT_PARTITION}}": "",
    }
    if not enabled:
        return replacements
    identity = config["identity"]
    identity_json = json.dumps(
        {
            "name": identity["name"],
            "uid": int(identity["uid"]),
            "gid": int(identity["gid"]),
            "gids": [int(gid) for gid in identity.get("gids") or []],
            "dir": identity["dir"],
            "shell": identity["shell"],
            "gecos": identity.get("gecos"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    replacements.update(
        {
            "{{PCS_ENABLED}}": "true",
            "{{PCS_RESTAPI_CIDR}}": str(config.get("endpoint_cidr") or vpc_cidr),
            "{{PCS_CLUSTER_ID}}": str(cluster_id),
            "{{PCS_IDENTITY_JSON}}": _yaml_single_quoted_body(identity_json),
            "{{PCS_DEFAULT_PARTITION}}": default_queue(config) if is_managed(config) else "",
        }
    )
    if is_managed(config) and config["managed"]["metrics"]["enabled"]:
        if not controller_address:
            raise PcsConfigError(
                "pcs.managed.metrics.enabled needs the controller address the stack's "
                "controller lookup resolves; none was passed"
            )
        replacements["{{PCS_METRICS_ENABLED}}"] = "true"
        replacements["{{PCS_CONTROLLER_ADDRESS}}"] = str(controller_address)
    return replacements


def identity_claims(identity: Mapping[str, Any]) -> dict[str, Any]:
    """The JWT claims PCS's slurmrestd requires for the configured identity.

    PCS reads the username from ``id.name`` (the top-level ``sun``/``username``
    claims are deprecated from Slurm 26.05); ``uid``/``gid`` are the numeric
    identity the job runs as, ``id.gids`` the full supplementary list with
    the primary group first.
    """
    gids = [int(identity["gid"])]
    for gid in identity.get("gids") or []:
        if int(gid) not in gids:
            gids.append(int(gid))
    return {
        "uid": int(identity["uid"]),
        "gid": int(identity["gid"]),
        "id": {
            "name": str(identity["name"]),
            "gecos": str(identity.get("gecos") or identity["name"]),
            "dir": str(identity["dir"]),
            "shell": str(identity["shell"]),
            "gids": gids,
        },
    }
