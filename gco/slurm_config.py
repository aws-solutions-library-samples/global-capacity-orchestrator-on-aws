"""Schema, defaults and rendering for Slurm on GCO (``helm.slurm``).

GCO runs Slurm on each regional EKS cluster through the Slinky slurm-operator
when ``cdk.json`` ``helm.slurm.enabled`` is true (off by default;
``helm_enabled_overrides=slurm`` forces it on for one deploy). The block is
the whole description of the cluster an operator gets, the Slurm equivalent of
an AWS PCS cluster definition:

- ``partitions`` are the compute node groups. Each one becomes a Slinky
  ``NodeSet`` (a pool of identically sized ``slurmd`` pods scheduled onto one
  of GCO's Karpenter NodePools) and the Slurm partition that spans it. Pod
  size is node size: Slinky sizes the Slurm node from the pod's resources and
  Karpenter picks an EC2 instance that fits it, so ``cpu``/``memory``/``gpus``
  stand in for PCS's instance type.
- ``min_nodes``/``max_nodes`` are the scaling bounds. A partition whose bounds
  differ is autoscaled on Slurm's own queue metrics by a KEDA ``ScaledObject``
  the chart renders through ``extraObjects`` (:func:`slurm_chart_values`),
  scaling the NodeSet to zero when idle; Karpenter then consolidates the
  nodes.
- ``accounting`` is slurmdbd with an in-cluster MariaDB (or an external
  database the operator already runs), ``metrics`` is slurmctld's native
  OpenMetrics endpoint, ``login`` is the sackd login pod ``gco slurm shell``
  execs into, and ``slurm_conf``/``slurmdbd_conf`` pass extra configuration
  through to the daemons.

This module owns the block's shape so the config loader, the regional stack,
the kubectl-applier manifests, the ``gco slurm`` CLI and the live validation
harness agree on names and defaults. Like :mod:`gco.argocd_config` it imports
nothing from ``aws_cdk``: the CLI runs without the CDK toolchain installed. It
raises :class:`SlurmConfigError` (a ``ValueError``); the loader re-raises with
its own exception type.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any

from gco.resource_governance import parse_k8s_quantity

# ---------------------------------------------------------------------------
# Fixed names shared by the chart values, the manifests, the API and the CLI
# ---------------------------------------------------------------------------

#: ``cdk.json`` ``helm`` block key (and ``helm_enabled_overrides`` name).
SLURM_HELM_KEY = "slurm"

#: charts.yaml entries (the Helm release names).
SLURM_OPERATOR_CHART_NAME = "slinky-slurm-operator"
SLURM_CLUSTER_CHART_NAME = "slinky-slurm"

#: The Slurm cluster lives with the user workloads; the operator has its own
#: namespace (charts.yaml ``slinky-slurm-operator.namespace``).
SLURM_NAMESPACE = "gco-jobs"
SLURM_OPERATOR_NAMESPACE = "slurm-operator"

#: Objects the ``slurm`` chart and the operator derive from the release name
#: ``slinky-slurm``: the Controller CR is the release name, its StatefulSet and
#: Service are ``<release>-controller``, the RestApi Deployment and Service are
#: ``<release>-restapi``, NodeSets are ``<release>-worker-<key>`` and the
#: LoginSet Deployment/Service is ``<release>-login-<key>``. The two auth
#: Secrets hold the cluster key (``slurm.key``) and the JWT signing key
#: (``jwt.key``).
SLURM_CONTROLLER_NAME = "slinky-slurm-controller"
SLURM_RESTAPI_NAME = "slinky-slurm-restapi"
SLURM_ACCOUNTING_NAME = "slinky-slurm-accounting"
SLURM_LOGIN_SET_KEY = "gco"
SLURM_LOGIN_NAME = f"{SLURM_CLUSTER_CHART_NAME}-login-{SLURM_LOGIN_SET_KEY}"
SLURM_JWT_SECRET = "slinky-slurm-auth-jwt"  # nosec B105  # Kubernetes Secret name, not its contents
SLURM_KEY_SECRET = "slinky-slurm-auth-slurm"  # nosec B105  # Kubernetes Secret name, not its contents

#: Daemon ports (Slurm defaults; the operator does not change them). slurmctld
#: also serves the OpenMetrics endpoints on its own port.
SLURM_CONTROLLER_PORT = 6817
SLURM_SLURMD_PORT = 6818
SLURM_SLURMDBD_PORT = 6819
SLURM_RESTAPI_PORT = 6820

#: Pod labels the operator stamps (``app.kubernetes.io/name`` per component;
#: every Slurm pod also carries the chart's ``app.kubernetes.io/part-of: slurm``).
SLURM_COMPONENT_LABEL = "app.kubernetes.io/name"
SLURM_CONTROLLER_COMPONENT = "slurmctld"
SLURM_RESTAPI_COMPONENT = "slurmrestd"
SLURM_WORKER_COMPONENT = "slurmd"
SLURM_ACCOUNTING_COMPONENT = "slurmdbd"
SLURM_LOGIN_COMPONENT = "login"
SLURM_PART_OF_LABEL = "app.kubernetes.io/part-of"
SLURM_PART_OF_VALUE = "slurm"

#: The in-cluster accounting database (29-slurm-accounting-db.yaml):
#: one StatefulSet, Service and generated-password Secret of this name, and
#: the slurmdbd connection constants the chart values point at it with.
SLURM_ACCOUNTING_DB_NAME = "slinky-slurm-mariadb"
SLURM_ACCOUNTING_DB_SECRET = SLURM_ACCOUNTING_DB_NAME  # nosec B105  # Kubernetes Secret name
SLURM_ACCOUNTING_DB_PASSWORD_KEY = "password"  # nosec B105  # Secret data key, not a password
SLURM_ACCOUNTING_DB_ROOT_PASSWORD_KEY = "root-password"  # nosec B105  # Secret data key
SLURM_ACCOUNTING_DB_PORT = 3306
SLURM_ACCOUNTING_DATABASE = "slurm_acct_db"
SLURM_ACCOUNTING_USER = "slurm"

#: Where the accounting backup CronJob writes its dumps in the regional shared
#: bucket (``<prefix><cluster name>/<UTC timestamp>.sql.gz``); the regional
#: stack's lifecycle rule expires objects under this prefix.
SLURM_ACCOUNTING_BACKUP_PREFIX = "slurm/accounting-backups/"

#: Block storage for slurmctld's state directory and the accounting database:
#: a gp3 StorageClass shipped by the gated base manifest 28-storage-slurm-gp3.yaml.
SLURM_STORAGE_CLASS = "gco-slurm-gp3"

#: Shared filesystems mounted into worker and login pods. The EFS claim is the
#: always-present ``gco-jobs`` claim; ``/home`` is a subdirectory of it so
#: Slurm home directories never mix with job output written to the claim root.
#: The FSx claim exists only when ``fsx_lustre`` is enabled.
SLURM_EFS_CLAIM = "gco-shared-storage"
SLURM_HOME_SUBPATH = "slurm/home"
SLURM_HOME_MOUNT_PATH = "/home"
SLURM_FSX_CLAIM = "gco-fsx-storage"
SLURM_SCRATCH_MOUNT_PATH = "/scratch"

#: The control-plane pods (slurmctld, slurmrestd, slurmdbd) run at the
#: platform priority (05-priority-classes.yaml) so they preempt user pods
#: under pressure instead of being starved; workers deliberately do not.
SLURM_PLATFORM_PRIORITY_CLASS = "gco-platform-critical"

#: Where the KEDA Prometheus scaler reads Slurm's metrics (kube-prometheus-stack).
SLURM_PROMETHEUS_URL = "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"

#: The Karpenter NodePools a partition may target (lambda/kubectl-applier-simple/
#: manifests/4x-nodepool-*.yaml): the node label that selects the pool, the
#: taints its nodes carry (the slurmd pods tolerate exactly those), and what
#: accelerators its instances have. ``neuron`` is deliberately absent: Slurm's
#: gres plugin has no Trainium support.
NODE_POOLS: dict[str, dict[str, Any]] = {
    "cpu": {"selector": {"workload-type": "cpu"}, "taints": (), "gpu": False, "efa": False},
    "gpu": {
        "selector": {"workload-type": "gpu"},
        "taints": ("nvidia.com/gpu",),
        "gpu": True,
        "efa": False,
    },
    "gpu-efa": {
        "selector": {"workload-type": "gpu-efa"},
        "taints": ("nvidia.com/gpu", "vpc.amazonaws.com/efa"),
        "gpu": True,
        "efa": True,
    },
}

#: ``karpenter.sh/capacity-type`` choices; ``any`` leaves the pool's own mix.
CAPACITY_TYPES: tuple[str, ...] = ("any", "spot", "on-demand")
CAPACITY_TYPE_LABEL = "karpenter.sh/capacity-type"

#: Slurm's ``MaxNodeCount`` as the operator configures it; a NodeSet cannot
#: usefully grow past it.
MAX_NODES_LIMIT = 1024

#: Partition names double as NodeSet keys: the operator names pods
#: ``<name>-<ordinal>`` and the NodeSet ``slinky-slurm-worker-<name>``, so the
#: name must be a DNS label short enough for both.
PARTITION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$|^[a-z]$")
#: The chart's own ``partitions.all`` key (GCO disables it, see values).
RESERVED_PARTITION_NAMES: frozenset[str] = frozenset({"all"})

#: Slurm time limits: minutes, min:sec, hours:min:sec, days-hours,
#: days-hours:min, days-hours:min:sec, or unlimited.
SLURM_TIME_PATTERN = re.compile(
    r"^(UNLIMITED|INFINITE|\d+|\d+:\d{2}|\d+:\d{2}:\d{2}|\d+-\d{1,2}|\d+-\d{1,2}:\d{2}|\d+-\d{1,2}:\d{2}:\d{2})$"
)

#: Kubernetes label values: ``features`` become Slurm node Features and are
#: also the only free-form text that reaches slurm.conf from a partition.
FEATURE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

#: Slurm ``ClusterName``: letters, digits, ``_`` and ``-``; slurmdbd uses it
#: as a table-name prefix, hence the conservative character set and length.
CLUSTER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
#: Keys an operator may set in ``slurm_conf`` / ``slurmdbd_conf``.
CONF_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")

#: slurm.conf keys the operator writes itself; overriding them through
#: ``extraConf`` either breaks the operator's reconciliation (auth, ports,
#: hosts) or duplicates a key Slurm rejects. The webhook rejects whole files
#: named slurm.conf/slurmdbd.conf; this is the per-key equivalent.
OPERATOR_OWNED_SLURM_CONF_KEYS: frozenset[str] = frozenset(
    {
        "ClusterName",
        "SlurmUser",
        "SlurmdUser",
        "SlurmctldHost",
        "SlurmctldPort",
        "SlurmdPort",
        "SlurmdSpoolDir",
        "StateSaveLocation",
        "SlurmctldLogFile",
        "SlurmSchedLogFile",
        "SlurmdLogFile",
        "AuthType",
        "CredType",
        "AuthAltTypes",
        "MetricsType",
        "AccountingStorageType",
        "AccountingStorageHost",
        "AccountingStoragePort",
        "Include",
        "NodeName",
        "NodeSet",
        "PartitionName",
        "Prolog",
        "Epilog",
        "PrologSlurmctld",
        "EpilogSlurmctld",
    }
)
OPERATOR_OWNED_SLURMDBD_CONF_KEYS: frozenset[str] = frozenset(
    {
        "DbdHost",
        "DbdPort",
        "SlurmUser",
        "StorageType",
        "StorageHost",
        "StoragePort",
        "StorageLoc",
        "StorageUser",
        "StoragePass",
        "AuthType",
        "AuthAltTypes",
        "AuthAltParameters",
        "LogFile",
        "PidFile",
    }
)

#: Per-partition defaults for keys an operator leaves out.
PARTITION_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "node_pool": "cpu",
    "cpu": "4",
    "memory": "16Gi",
    "gpus": 0,
    "efa_interfaces": 0,
    "capacity_type": "any",
    "min_nodes": 0,
    "max_nodes": 4,
    "default": False,
    "max_time": "UNLIMITED",
    "exclusive": False,
    "features": [],
}

#: The block with every knob at its default. Slurm is off; when it is on, the
#: shipped partitions give a CPU partition and a single-GPU partition that
#: both scale from zero, plus a disabled template for multi-node EFA training
#: (p5-class: eight GPUs, 32 EFA interfaces, whole-node exclusive jobs).
SLURM_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    # Slurm's ClusterName; also the accounting cluster record.
    "cluster_name": "gco-slurm",
    "controller": {
        # slurmctld StateSaveLocation: a dedicated gp3 volume (the chart
        # creates the claim on SLURM_STORAGE_CLASS).
        "state_storage_size": "20Gi",
    },
    "restapi": {
        # Two slurmrestd replicas behind one Service, with a PDB keeping one
        # up through node drains: the GCO API's path into Slurm.
        "replicas": 2,
    },
    "accounting": {
        "enabled": True,
        # In-cluster MariaDB (29-slurm-accounting-db.yaml).
        "storage_size": "20Gi",
        "backup": {
            # Nightly mariadb-dump of slurm_acct_db to the regional shared
            # bucket under slurm/accounting-backups/<cluster>/.
            "enabled": True,
            "schedule": "0 3 * * *",
            "retention_days": 30,
        },
        # Point slurmdbd at a database the operator already runs (RDS for
        # MariaDB, Aurora MySQL, ...) instead of the in-cluster one. host set
        # = external; password_secret names a Secret in gco-jobs carrying the
        # password under the given key.
        "external": {
            "host": "",
            "port": SLURM_ACCOUNTING_DB_PORT,
            "database": SLURM_ACCOUNTING_DATABASE,
            "username": SLURM_ACCOUNTING_USER,
            "password_secret": "",
            "password_key": SLURM_ACCOUNTING_DB_PASSWORD_KEY,
        },
    },
    "metrics": {
        # slurmctld OpenMetrics (MetricsType=metrics/openmetrics); scraped by
        # the gco-slurm-controller ServiceMonitor when observability is on and
        # read by the autoscaler. Only turn off for a cluster that has no
        # autoscaled partition.
        "enabled": True,
    },
    "login": {
        # One sackd login pod (ClusterIP, no load balancer): the target of
        # `gco slurm shell`, with /home mounted like the workers.
        "enabled": True,
        "replicas": 1,
    },
    "autoscaling": {
        # Pending jobs per additional node (KEDA threshold for the pending-jobs
        # trigger; 1 = one node per pending job).
        "pending_jobs_threshold": 1,
        # Idle time before an autoscaled partition is scaled to min_nodes (and
        # to zero when min_nodes is 0).
        "cooldown_seconds": 300,
        "polling_interval_seconds": 30,
    },
    "partitions": {
        "cpu": {
            "node_pool": "cpu",
            "cpu": "8",
            "memory": "32Gi",
            "gpus": 0,
            "capacity_type": "any",
            "min_nodes": 0,
            "max_nodes": 8,
            "default": True,
            "max_time": "UNLIMITED",
            "exclusive": False,
        },
        "gpu": {
            "node_pool": "gpu",
            "cpu": "8",
            "memory": "32Gi",
            "gpus": 1,
            "capacity_type": "any",
            "min_nodes": 0,
            "max_nodes": 4,
            "default": False,
            "max_time": "2-00:00:00",
            "exclusive": True,
        },
        "gpu-efa": {
            "enabled": False,
            "node_pool": "gpu-efa",
            "cpu": "180",
            "memory": "1800Gi",
            "gpus": 8,
            "efa_interfaces": 32,
            "capacity_type": "on-demand",
            "min_nodes": 0,
            "max_nodes": 2,
            "default": False,
            "max_time": "7-00:00:00",
            "exclusive": True,
        },
    },
    "storage": {
        # /home for workers and login pods: "efs" (the shared claim) or "none".
        "home": "efs",
        # /scratch: "auto" (FSx Lustre when fsx_lustre is enabled), "fsx"
        # (require it) or "none".
        "scratch": "auto",
    },
    # Extra slurm.conf / slurmdbd.conf lines (key -> value or list of values).
    "slurm_conf": {},
    "slurmdbd_conf": {},
}

#: Image pins GCO keeps in lockstep with the chart version in charts.yaml.
#: The Slinky sidecars default to ``alpine:latest`` upstream; GCO pins them.
SLURM_IMAGE_TAG = "26.05.5-ubuntu24.04"
SLURM_SIDECAR_IMAGE = {"repository": "public.ecr.aws/docker/library/alpine", "tag": "3.24.3"}


class SlurmConfigError(ValueError):
    """Raised when the ``helm.slurm`` block is malformed."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _deep_merge(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(defaults))
    for key, value in overrides.items():
        if value is None and isinstance(merged.get(key), Mapping):
            # JSON null for a nested block reads as "absent": keep its defaults.
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
        raise SlurmConfigError(
            f"{path} contains unknown key(s): {', '.join(unknown)}; "
            f"allowed keys: {', '.join(sorted(allowed_set))}"
        )


def _require_object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SlurmConfigError(f"{path} must be an object, got {value!r}")
    return value


def _require_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise SlurmConfigError(f"{path} must be a boolean, got {value!r}")
    return value


def _require_int(value: object, bounds: tuple[int, int], path: str) -> int:
    """An exact integer (``bool`` rejected) inside the inclusive ``bounds``."""
    low, high = bounds
    if type(value) is not int or not low <= value <= high:
        raise SlurmConfigError(f"{path} must be an integer between {low} and {high}, got {value!r}")
    return value


def _require_quantity(value: object, path: str) -> float:
    """A positive Kubernetes quantity (``"8"``, ``"500m"``, ``"32Gi"``, ``8``)."""
    if type(value) is bool or not isinstance(value, str | int | float):
        raise SlurmConfigError(f"{path} must be a Kubernetes quantity, got {value!r}")
    try:
        parsed = parse_k8s_quantity(str(value))
    except ValueError as exc:
        raise SlurmConfigError(f"{path}={value!r} is not a valid Kubernetes quantity") from exc
    if parsed <= 0:
        raise SlurmConfigError(f"{path} must be greater than zero, got {value!r}")
    return parsed


def _require_nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SlurmConfigError(f"{path} must be a non-empty string, got {value!r}")
    return value.strip()


def _validate_conf_map(raw: object, path: str, owned: frozenset[str]) -> dict[str, list[str]]:
    """Validate an ``extraConfMap``-shaped block: key -> string or list of strings."""
    block = _require_object(raw, path)
    normalized: dict[str, list[str]] = {}
    for key, value in block.items():
        key_text = str(key)
        if not CONF_KEY_PATTERN.match(key_text):
            raise SlurmConfigError(
                f"{path} key {key_text!r} is not a Slurm configuration parameter name"
            )
        if key_text in owned:
            raise SlurmConfigError(
                f"{path}.{key_text} is managed by the slurm-operator and cannot be overridden"
            )
        values = value if isinstance(value, list) else [value]
        rendered: list[str] = []
        for item in values:
            if type(item) is bool or not isinstance(item, str | int | float):
                raise SlurmConfigError(
                    f"{path}.{key_text} must be a string, a number or a list of them, got {value!r}"
                )
            text = str(item).strip()
            if not text or "\n" in text:
                raise SlurmConfigError(
                    f"{path}.{key_text} values must be non-empty single-line strings, got {item!r}"
                )
            rendered.append(text)
        if not rendered:
            raise SlurmConfigError(f"{path}.{key_text} must not be an empty list")
        normalized[key_text] = rendered
    return normalized


def _validate_partition(name: str, raw: object) -> dict[str, Any]:
    path = f"helm.slurm.partitions.{name}"
    if not PARTITION_NAME_PATTERN.match(name):
        raise SlurmConfigError(
            f"helm.slurm.partitions has an invalid partition name {name!r}: use a lowercase "
            "DNS label of at most 32 characters (it names the NodeSet and its pods)"
        )
    if name in RESERVED_PARTITION_NAMES:
        raise SlurmConfigError(
            f"helm.slurm.partitions.{name} is reserved by the Slinky chart; choose another name"
        )
    block = _require_object(raw, path)
    _reject_unknown_keys(block, PARTITION_DEFAULTS.keys(), path)
    partition = _deep_merge(PARTITION_DEFAULTS, block)

    _require_bool(partition["enabled"], f"{path}.enabled")
    _require_bool(partition["default"], f"{path}.default")
    _require_bool(partition["exclusive"], f"{path}.exclusive")
    pool_name = partition["node_pool"]
    if pool_name not in NODE_POOLS:
        raise SlurmConfigError(
            f"{path}.node_pool must be one of {', '.join(sorted(NODE_POOLS))}, got {pool_name!r}"
        )
    pool = NODE_POOLS[pool_name]
    cpu = _require_quantity(partition["cpu"], f"{path}.cpu")
    if cpu < 1 or cpu != int(cpu):
        raise SlurmConfigError(
            f"{path}.cpu must be a whole number of cores (a Slurm node advertises whole CPUs), "
            f"got {partition['cpu']!r}"
        )
    memory = _require_quantity(partition["memory"], f"{path}.memory")
    if memory < 1024 * 1024 * 1024:
        raise SlurmConfigError(
            f"{path}.memory must be at least 1Gi (slurmd and the job need room inside the "
            f"pod's limit), got {partition['memory']!r}"
        )
    gpus = _require_int(partition["gpus"], (0, 64), f"{path}.gpus")
    efa = _require_int(partition["efa_interfaces"], (0, 64), f"{path}.efa_interfaces")
    if gpus and not pool["gpu"]:
        raise SlurmConfigError(
            f"{path}.gpus is {gpus} but node_pool {pool_name!r} has no GPUs; use gpu or gpu-efa"
        )
    if pool["gpu"] and not gpus:
        raise SlurmConfigError(
            f"{path}.node_pool {pool_name!r} schedules onto GPU nodes but gpus is 0; request at "
            "least one GPU or use the cpu pool"
        )
    if efa and not pool["efa"]:
        raise SlurmConfigError(
            f"{path}.efa_interfaces is {efa} but node_pool {pool_name!r} has no EFA; use gpu-efa"
        )
    if pool["efa"] and not efa:
        raise SlurmConfigError(
            f"{path}.node_pool {pool_name!r} is the EFA pool but efa_interfaces is 0; request the "
            "instance's EFA interfaces (32 on p5-class instances) or use the gpu pool"
        )
    if partition["capacity_type"] not in CAPACITY_TYPES:
        raise SlurmConfigError(
            f"{path}.capacity_type must be one of {', '.join(CAPACITY_TYPES)}, "
            f"got {partition['capacity_type']!r}"
        )
    min_nodes = _require_int(partition["min_nodes"], (0, MAX_NODES_LIMIT), f"{path}.min_nodes")
    max_nodes = _require_int(partition["max_nodes"], (1, MAX_NODES_LIMIT), f"{path}.max_nodes")
    if min_nodes > max_nodes:
        raise SlurmConfigError(
            f"{path}.min_nodes must not exceed max_nodes, got {min_nodes} > {max_nodes}"
        )
    max_time = partition["max_time"]
    if not isinstance(max_time, str) or not SLURM_TIME_PATTERN.match(max_time.strip()):
        raise SlurmConfigError(
            f"{path}.max_time must be a Slurm time limit (minutes, HH:MM:SS, D-HH:MM:SS or "
            f"UNLIMITED), got {max_time!r}"
        )
    partition["max_time"] = max_time.strip()
    features = partition["features"]
    if not isinstance(features, list) or not all(
        isinstance(item, str) and FEATURE_PATTERN.match(item) for item in features
    ):
        raise SlurmConfigError(
            f"{path}.features must be a list of Slurm feature names (letters, digits, _ . -), "
            f"got {features!r}"
        )
    if len(set(features)) != len(features):
        raise SlurmConfigError(f"{path}.features lists a feature twice")
    partition["_cpu_cores"] = cpu
    partition["_memory_bytes"] = memory
    return partition


def _validate_partitions(raw: object) -> dict[str, dict[str, Any]]:
    block = _require_object(raw, "helm.slurm.partitions")
    partitions = {str(name): _validate_partition(str(name), spec) for name, spec in block.items()}
    enabled = {name: spec for name, spec in partitions.items() if spec["enabled"]}
    if not enabled:
        raise SlurmConfigError(
            "helm.slurm.partitions must declare at least one enabled partition when Slurm is on"
        )
    defaults = sorted(name for name, spec in enabled.items() if spec["default"])
    if len(defaults) != 1:
        raise SlurmConfigError(
            "helm.slurm.partitions must mark exactly one enabled partition default: true "
            f"(sbatch without --partition goes there), found {defaults or 'none'}"
        )
    return partitions


def _quota_quantity(resource_quota: Mapping[str, Any], key: str) -> float | None:
    """A quota ceiling as a float, ``None`` when absent or unparseable.

    The regional stack validates ``resource_quota`` itself and reports a bad
    value with its own message; this module only compares against values it
    can read, so a malformed quota never produces a second, Slurm-flavored
    error for the same typo.
    """
    value = resource_quota.get(key)
    if value is None:
        return None
    try:
        return parse_k8s_quantity(str(value))
    except ValueError:
        return None


def _validate_quota_fit(
    config: Mapping[str, Any], resource_quota: Mapping[str, Any] | None
) -> None:
    """Fail when the declared partitions can never fit the gco-jobs quota.

    Slurm worker pods live in ``gco-jobs`` and count against its
    ``ResourceQuota`` and ``LimitRange`` (04-resource-quotas.yaml). A partition
    whose node is larger than the per-container ceiling, or whose
    ``max_nodes`` worth of nodes exceeds the namespace ceiling on its own,
    would deploy fine and then sit PENDING forever with the reason visible only
    in namespace events. Check the certain failures at synth; the quota is
    shared with other job pods, so fitting here is necessary, not sufficient.
    """
    if not resource_quota:
        return
    enabled = {n: p for n, p in config["partitions"].items() if p["enabled"]}
    container_cpu = _quota_quantity(resource_quota, "container_max_cpu")
    container_memory = _quota_quantity(resource_quota, "container_max_memory")
    container_gpu = _quota_quantity(resource_quota, "container_max_gpu")
    for name, spec in enabled.items():
        path = f"helm.slurm.partitions.{name}"
        if container_cpu is not None and spec["_cpu_cores"] > container_cpu:
            raise SlurmConfigError(
                f"{path}.cpu={spec['cpu']!r} exceeds resource_quota.container_max_cpu="
                f"{resource_quota['container_max_cpu']!r}: the LimitRange would reject every "
                "worker pod"
            )
        if container_memory is not None and spec["_memory_bytes"] > container_memory:
            raise SlurmConfigError(
                f"{path}.memory={spec['memory']!r} exceeds resource_quota.container_max_memory="
                f"{resource_quota['container_max_memory']!r}: the LimitRange would reject every "
                "worker pod"
            )
        if container_gpu is not None and spec["gpus"] > container_gpu:
            raise SlurmConfigError(
                f"{path}.gpus={spec['gpus']} exceeds resource_quota.container_max_gpu="
                f"{resource_quota['container_max_gpu']!r}: the LimitRange would reject every "
                "worker pod"
            )
    totals = {
        "max_cpu": sum(p["_cpu_cores"] * p["max_nodes"] for p in enabled.values()),
        "max_memory": sum(p["_memory_bytes"] * p["max_nodes"] for p in enabled.values()),
        "max_gpu": float(sum(p["gpus"] * p["max_nodes"] for p in enabled.values())),
        "max_pods": float(
            sum(p["max_nodes"] for p in enabled.values()) + control_plane_pods(config)
        ),
    }
    for key, needed in totals.items():
        ceiling = _quota_quantity(resource_quota, key)
        if ceiling is not None and needed > ceiling:
            raise SlurmConfigError(
                f"helm.slurm.partitions at max_nodes need {needed:g} of resource_quota.{key} "
                f"but the gco-jobs ceiling is {resource_quota[key]!r}; lower max_nodes or raise "
                "the quota (workers share it with every other job pod)"
            )


def control_plane_pods(config: Mapping[str, Any]) -> int:
    """Pods the cluster runs besides workers: slurmctld, slurmrestd, slurmdbd, MariaDB, login."""
    count = 1 + int(config["restapi"]["replicas"])
    if config["accounting"]["enabled"]:
        count += 1
        if accounting_in_cluster(config):
            count += 1
    if config["login"]["enabled"]:
        count += int(config["login"]["replicas"])
    return count


def validate_slurm_config(
    raw: object,
    *,
    resource_quota: Mapping[str, Any] | None = None,
    observability_enabled: bool = True,
    fsx_enabled: bool = False,
) -> dict[str, Any]:
    """Validate the raw ``helm.slurm`` block and return it with defaults filled.

    ``None`` means "absent" and yields the defaults (Slurm off). Unknown keys
    fail by name at every level. The deployment-wide facts that decide whether
    the block can work are passed in: the effective ``resource_quota`` (worker
    pods must fit the gco-jobs ceilings), whether ``cluster_observability`` is
    on (autoscaling reads Slurm's metrics through Prometheus) and whether
    ``fsx_lustre`` is on (``storage.scratch: fsx``).

    The returned mapping carries two derived, underscore-prefixed numbers per
    partition (``_cpu_cores``, ``_memory_bytes``) for the renderer; everything
    else is the operator's block with defaults merged.
    """
    if raw is None:
        config = copy.deepcopy(SLURM_DEFAULTS)
        config["partitions"] = _validate_partitions(config["partitions"])
        return config
    block = _require_object(raw, "helm.slurm")
    _reject_unknown_keys(block, SLURM_DEFAULTS.keys(), "helm.slurm")
    for key in (
        "controller",
        "restapi",
        "accounting",
        "metrics",
        "login",
        "autoscaling",
        "storage",
    ):
        sub = block.get(key)
        if sub is None:
            continue
        sub_block = _require_object(sub, f"helm.slurm.{key}")
        _reject_unknown_keys(sub_block, SLURM_DEFAULTS[key].keys(), f"helm.slurm.{key}")
        if key == "accounting":
            for nested in ("backup", "external"):
                nested_raw = sub_block.get(nested)
                if nested_raw is None:
                    continue
                nested_block = _require_object(nested_raw, f"helm.slurm.accounting.{nested}")
                _reject_unknown_keys(
                    nested_block,
                    SLURM_DEFAULTS["accounting"][nested].keys(),
                    f"helm.slurm.accounting.{nested}",
                )
    # Partitions merge like every other block: the shipped cpu/gpu/gpu-efa
    # definitions are always present, an entry of the same name deep-merges
    # over the shipped one (``gpu: {max_nodes: 8}`` keeps the GPU pool), a new
    # name starts from PARTITION_DEFAULTS, and ``enabled: false`` removes one.
    config = _deep_merge(SLURM_DEFAULTS, block)

    _require_bool(config["enabled"], "helm.slurm.enabled")
    cluster_name = _require_nonempty_string(config["cluster_name"], "helm.slurm.cluster_name")
    if not CLUSTER_NAME_PATTERN.match(cluster_name):
        raise SlurmConfigError(
            f"helm.slurm.cluster_name must be a Slurm ClusterName (letters, digits, _ -), "
            f"got {cluster_name!r}"
        )
    config["cluster_name"] = cluster_name
    _require_quantity(
        config["controller"]["state_storage_size"], "helm.slurm.controller.state_storage_size"
    )
    _require_int(config["restapi"]["replicas"], (1, 10), "helm.slurm.restapi.replicas")

    accounting = config["accounting"]
    _require_bool(accounting["enabled"], "helm.slurm.accounting.enabled")
    _require_quantity(accounting["storage_size"], "helm.slurm.accounting.storage_size")
    backup = accounting["backup"]
    _require_bool(backup["enabled"], "helm.slurm.accounting.backup.enabled")
    _validate_cron(backup["schedule"], "helm.slurm.accounting.backup.schedule")
    _require_int(backup["retention_days"], (1, 3650), "helm.slurm.accounting.backup.retention_days")
    external = accounting["external"]
    for key in ("host", "database", "username", "password_secret", "password_key"):
        if not isinstance(external[key], str):
            raise SlurmConfigError(
                f"helm.slurm.accounting.external.{key} must be a string, got {external[key]!r}"
            )
        external[key] = external[key].strip()
    _require_int(external["port"], (1, 65535), "helm.slurm.accounting.external.port")
    if external["host"]:
        for key in ("database", "username", "password_secret", "password_key"):
            if not external[key]:
                raise SlurmConfigError(
                    f"helm.slurm.accounting.external.{key} is required when external.host is set"
                )
    elif external["password_secret"]:
        raise SlurmConfigError(
            "helm.slurm.accounting.external.password_secret is set but external.host is empty; "
            "set the host to use an external database"
        )

    _require_bool(config["metrics"]["enabled"], "helm.slurm.metrics.enabled")
    _require_bool(config["login"]["enabled"], "helm.slurm.login.enabled")
    _require_int(config["login"]["replicas"], (1, 10), "helm.slurm.login.replicas")
    autoscaling = config["autoscaling"]
    _require_int(
        autoscaling["pending_jobs_threshold"],
        (1, 1000),
        "helm.slurm.autoscaling.pending_jobs_threshold",
    )
    _require_int(
        autoscaling["cooldown_seconds"], (60, 86400), "helm.slurm.autoscaling.cooldown_seconds"
    )
    _require_int(
        autoscaling["polling_interval_seconds"],
        (5, 3600),
        "helm.slurm.autoscaling.polling_interval_seconds",
    )

    storage = config["storage"]
    if storage["home"] not in ("efs", "none"):
        raise SlurmConfigError(
            f"helm.slurm.storage.home must be 'efs' or 'none', got {storage['home']!r}"
        )
    if storage["scratch"] not in ("auto", "fsx", "none"):
        raise SlurmConfigError(
            f"helm.slurm.storage.scratch must be 'auto', 'fsx' or 'none', got {storage['scratch']!r}"
        )
    if storage["scratch"] == "fsx" and not fsx_enabled:
        raise SlurmConfigError(
            "helm.slurm.storage.scratch is 'fsx' but fsx_lustre is not enabled for this "
            "deployment; enable fsx_lustre or use 'auto'"
        )

    config["slurm_conf"] = _validate_conf_map(
        config["slurm_conf"], "helm.slurm.slurm_conf", OPERATOR_OWNED_SLURM_CONF_KEYS
    )
    config["slurmdbd_conf"] = _validate_conf_map(
        config["slurmdbd_conf"], "helm.slurm.slurmdbd_conf", OPERATOR_OWNED_SLURMDBD_CONF_KEYS
    )

    config["partitions"] = _validate_partitions(config["partitions"])
    if config["enabled"]:
        if autoscaled_partitions(config) and not observability_enabled:
            names = ", ".join(autoscaled_partitions(config))
            raise SlurmConfigError(
                f"helm.slurm.partitions {names} autoscale (min_nodes < max_nodes), which reads "
                "Slurm's queue metrics through Prometheus, but cluster_observability is disabled; "
                "enable it or set min_nodes equal to max_nodes for a fixed-size partition"
            )
        if autoscaled_partitions(config) and not config["metrics"]["enabled"]:
            raise SlurmConfigError(
                "helm.slurm.metrics.enabled is false but a partition autoscales; the autoscaler "
                "reads slurmctld's metrics"
            )
        _validate_quota_fit(config, resource_quota)
    return config


_CRON_FIELD = r"(\*|\d+|\*/\d+|\d+-\d+|\d+(,\d+)+)"
_CRON_PATTERN = re.compile(rf"^{_CRON_FIELD}(\s+{_CRON_FIELD}){{4}}$")


def _validate_cron(value: object, path: str) -> None:
    if not isinstance(value, str) or not _CRON_PATTERN.match(value.strip()):
        raise SlurmConfigError(
            f"{path} must be a five-field cron expression (e.g. '0 3 * * *'), got {value!r}"
        )


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


def slurm_enabled(config: Mapping[str, Any]) -> bool:
    """True when the block turns Slurm on (before any run-scoped override)."""
    return bool(config.get("enabled"))


def enabled_partitions(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The enabled partitions in declaration order."""
    return {n: p for n, p in config["partitions"].items() if p["enabled"]}


def autoscaled_partitions(config: Mapping[str, Any]) -> list[str]:
    """Enabled partitions whose bounds differ (a KEDA ScaledObject each)."""
    return [n for n, p in enabled_partitions(config).items() if p["min_nodes"] < p["max_nodes"]]


def accounting_in_cluster(config: Mapping[str, Any]) -> bool:
    """True when accounting is on and backed by the in-cluster MariaDB."""
    accounting = config["accounting"]
    return bool(accounting["enabled"]) and not accounting["external"]["host"]


def scratch_backend(config: Mapping[str, Any], *, fsx_enabled: bool) -> str:
    """``"fsx"`` or ``"none"``: what ``storage.scratch`` resolves to."""
    scratch = str(config["storage"]["scratch"])
    if scratch == "auto":
        return "fsx" if fsx_enabled else "none"
    return scratch


def nodeset_name(partition: str) -> str:
    """The NodeSet (and its pods' StatefulSet) for a partition."""
    return f"{SLURM_CLUSTER_CHART_NAME}-worker-{partition}"


def scaled_object_name(partition: str) -> str:
    """The KEDA ScaledObject that autoscales a partition."""
    return f"{SLURM_CLUSTER_CHART_NAME}-autoscale-{partition}"


# ---------------------------------------------------------------------------
# Chart values
# ---------------------------------------------------------------------------

#: The slurm.conf parameters GCO sets for every cluster, before the operator's
#: block. select/cons_tres with CR_Core_Memory schedules cores and memory
#: inside a worker pod; gres/gpu is tracked in accounting; ReturnToService=2
#: brings a worker pod back into service as soon as it re-registers after a
#: restart; the cgroup task plugin enforces the allocation; MinJobAge keeps
#: finished jobs visible to the REST API long enough for the GCO API to read
#: their final state; AccountingStoreFlags=job_comment keeps each job's
#: comment in slurmdbd, which is where the GCO API records the submitting
#: caller (``gco:<caller>``) for ``sacct`` attribution (verified live: without
#: it the accounting record's comment is empty).
BASE_SLURM_CONF: dict[str, list[str]] = {
    "SelectType": ["select/cons_tres"],
    "SelectTypeParameters": ["CR_Core_Memory"],
    "ProctrackType": ["proctrack/cgroup"],
    "TaskPlugin": ["task/cgroup", "task/affinity"],
    "JobAcctGatherType": ["jobacct_gather/cgroup"],
    "ReturnToService": ["2"],
    "MinJobAge": ["600"],
    "AccountingStoreFlags": ["job_comment"],
    "PrologFlags": ["Contain"],
    "SchedulerParameters": ["bf_continue", "bf_max_job_test=1000"],
    "PriorityType": ["priority/multifactor"],
    "PriorityWeightAge": ["1000"],
    "PriorityWeightFairshare": ["10000"],
    "PriorityWeightJobSize": ["1000"],
    "PriorityWeightPartition": ["1000"],
}

#: Added when any enabled partition has GPUs.
GPU_SLURM_CONF: dict[str, list[str]] = {
    "GresTypes": ["gpu"],
    "AccountingStorageTRES": ["gres/gpu"],
}

#: cgroup.conf: cgroup v2 (the operator's own two lines) plus constraints so a
#: job sees only the cores, memory and GPU devices Slurm allocated to it.
#: slurmd runs privileged in Slinky, which is what makes device constraints
#: work inside a pod.
CGROUP_CONF = (
    "CgroupPlugin=cgroup/v2\n"
    "IgnoreSystemd=yes\n"
    "ConstrainCores=yes\n"
    "ConstrainRAMSpace=yes\n"
    "ConstrainDevices=yes\n"
)

#: gres.conf: discover the GPUs the NVIDIA runtime exposed to the worker pod
#: from the driver's /proc and /sys files (Slurm's gpu/nvidia plugin), so no
#: NVML library and no per-node device list is needed.
GRES_CONF = "AutoDetect=nvidia\n"

#: Prometheus queries the autoscaler scales on, per partition (``{partition}``
#: is substituted). Both add the partition's busy nodes so a scale decision
#: never drops below what is running; ``or vector(0)`` keeps a missing series
#: from reading as "idle". The first grows one node per pending job (at the
#: configured threshold); the second grows to the largest pending job's node
#: count, which is what a multi-node job needs before it can start. KEDA's HPA
#: takes the larger of the two.
#:
#: Busy is ALLOCATED *or* MIXED: Slurm reports a node whose CPUs are only
#: partly allocated as MIXED, and ``slurm_partition_nodes_alloc`` counts the
#: fully allocated ones alone. Counting only those, a worker running one
#: 1-CPU job read as idle, KEDA scaled the NodeSet to zero after the cooldown,
#: and the operator drained the busy node while its job ran (observed live,
#: 2026-10-28).
_BUSY_NODES = (
    '(sum(slurm_partition_nodes_alloc{{partition="{partition}"}}) or vector(0)) + '
    '(sum(slurm_partition_nodes_mixed{{partition="{partition}"}}) or vector(0))'
)
PENDING_JOBS_QUERY = (
    '(sum(slurm_partition_jobs_pending{{partition="{partition}"}}) or vector(0)) + ' + _BUSY_NODES
)
LARGEST_PENDING_JOB_QUERY = (
    '(max(slurm_partition_jobs_max_job_nodes_nohold{{partition="{partition}"}}) or vector(0)) + '
    + _BUSY_NODES
)


def _conf_map(*layers: Mapping[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for layer in layers:
        for key, values in layer.items():
            merged[key] = list(values)
    return merged


#: Placement shared by every Slurm pod that is not a worker: slurmctld,
#: slurmdbd, slurmrestd, the login pod (chart values) and the MariaDB /
#: backup pods (post-Helm manifests mirror it verbatim). On-demand only (a
#: Spot reclaim of the controller is a cluster outage), amd64 (the Slinky
#: images are built for it), and never on an accelerator pool: the gco-jobs
#: LimitRange defaults every container to ``nvidia.com/gpu: 0``, which the
#: ExtendedResourceToleration admission plugin turns into a toleration of the
#: ``nvidia.com/gpu`` taint, and Karpenter then provisioned a g4dn for the
#: controller (observed live, 2026-10-27) and ``do-not-disrupt`` pinned it
#: there. Every GCO accelerator and job pool labels its nodes with
#: ``workload-type``; the Auto Mode general-purpose pool does not.
PLATFORM_POD_NODE_SELECTOR: dict[str, str] = {
    "kubernetes.io/os": "linux",
    "kubernetes.io/arch": "amd64",
    CAPACITY_TYPE_LABEL: "on-demand",
}
PLATFORM_POD_AFFINITY: dict[str, Any] = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {"matchExpressions": [{"key": "workload-type", "operator": "DoesNotExist"}]}
            ]
        }
    }
}


def _platform_pod_placement() -> dict[str, Any]:
    """Fresh copies of the shared non-worker placement (selector + affinity)."""
    return {
        "nodeSelector": dict(PLATFORM_POD_NODE_SELECTOR),
        "affinity": copy.deepcopy(PLATFORM_POD_AFFINITY),
    }


def _control_plane_pod_spec() -> dict[str, Any]:
    """Pod spec fields shared by slurmctld, slurmrestd and slurmdbd.

    The shared placement plus the platform priority class, which keeps them
    scheduled ahead of user pods.
    """
    return {"priorityClassName": SLURM_PLATFORM_PRIORITY_CLASS, **_platform_pod_placement()}


#: Where API-submitted jobs run by default and where their ``slurm-<id>.out``
#: lands: the ``slurm`` user's home (``/home/slurm`` in the Slinky images),
#: which is on the shared EFS claim when ``storage.home`` is ``efs`` so the
#: login pod can read the output (``gco slurm output``). The login pod's init
#: container below creates it: the claim starts empty and the EFS access
#: point squashes every writer to one POSIX identity, so a plain mkdir there
#: is all the directory needs.
SLURM_DEFAULT_WORKING_DIRECTORY = "/home/slurm"


def _home_init_containers(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The login pod's init container that creates the shared ``/home/slurm``."""
    if config["storage"]["home"] != "efs":
        return []
    return [
        {
            "name": "home-init",
            "image": f"{SLURM_SIDECAR_IMAGE['repository']}:{SLURM_SIDECAR_IMAGE['tag']}",
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "sh",
                "-c",
                f"mkdir -p {SLURM_DEFAULT_WORKING_DIRECTORY} && chmod 2775 "
                f"{SLURM_DEFAULT_WORKING_DIRECTORY}",
            ],
            "resources": {
                "requests": {"cpu": "50m", "memory": "32Mi"},
                "limits": {"cpu": "100m", "memory": "64Mi"},
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumeMounts": [
                {
                    "name": "home",
                    "mountPath": SLURM_HOME_MOUNT_PATH,
                    "subPath": SLURM_HOME_SUBPATH,
                }
            ],
        }
    ]


def _shared_volumes(
    config: Mapping[str, Any], *, fsx_enabled: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(volumes, volumeMounts)`` for /home and /scratch on worker and login pods."""
    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    if config["storage"]["home"] == "efs":
        volumes.append({"name": "home", "persistentVolumeClaim": {"claimName": SLURM_EFS_CLAIM}})
        mounts.append(
            {"name": "home", "mountPath": SLURM_HOME_MOUNT_PATH, "subPath": SLURM_HOME_SUBPATH}
        )
    if scratch_backend(config, fsx_enabled=fsx_enabled) == "fsx":
        volumes.append({"name": "scratch", "persistentVolumeClaim": {"claimName": SLURM_FSX_CLAIM}})
        mounts.append({"name": "scratch", "mountPath": SLURM_SCRATCH_MOUNT_PATH})
    return volumes, mounts


def slurm_real_memory_mib(memory_bytes: int) -> int:
    """``RealMemory`` for a worker whose pod limit is ``memory_bytes``.

    Slurm may hand a job everything RealMemory advertises; the pod's cgroup
    limit also has to hold slurmd and the step's overhead, so the larger of
    1 GiB and 5% is kept back (a 32Gi worker advertises 31130 MiB).
    """
    limit_mib = memory_bytes // (1024 * 1024)
    return max(limit_mib - max(1024, limit_mib // 20), 1)


def _nodeset_values(
    name: str,
    partition: Mapping[str, Any],
    *,
    volumes: list[dict[str, Any]],
    mounts: list[dict[str, Any]],
) -> dict[str, Any]:
    pool = NODE_POOLS[partition["node_pool"]]
    resources: dict[str, str] = {"cpu": str(partition["cpu"]), "memory": str(partition["memory"])}
    if partition["gpus"]:
        resources["nvidia.com/gpu"] = str(partition["gpus"])
    if partition["efa_interfaces"]:
        resources["vpc.amazonaws.com/efa"] = str(partition["efa_interfaces"])
    node_selector: dict[str, str] = {"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64"}
    node_selector.update(pool["selector"])
    if partition["capacity_type"] != "any":
        node_selector[CAPACITY_TYPE_LABEL] = partition["capacity_type"]
    tolerations = [
        {"key": taint, "operator": "Equal", "value": "true", "effect": "NoSchedule"}
        for taint in pool["taints"]
    ]
    # The node's Slurm definition, fixed from the pod's resources rather than
    # detected from the host. slurmd sees the whole instance (36 CPUs on a
    # c4.8xlarge, not the pod's 8), and a dynamic node keeps its first
    # registration's record across scale-to-zero cycles: when KEDA brought a
    # scaled-in worker back on a different instance type, slurmctld marked it
    # INVALID ("Low socket*core*thread count, CoreSpec differ") and the
    # partition was dead until an operator intervened (observed live,
    # 2026-10-28). Advertising exactly the pod's CPUs and memory (minus
    # headroom for slurmd itself) also stops Slurm from scheduling more onto
    # the pod than its cgroup allows. A host always has at least this much,
    # so re-registration on any instance type matches the stored record.
    cpus = int(partition["_cpu_cores"])
    real_memory = slurm_real_memory_mib(int(partition["_memory_bytes"]))
    extra_conf: dict[str, list[str]] = {
        "CPUs": [str(cpus)],
        "Boards": ["1"],
        "SocketsPerBoard": ["1"],
        "CoresPerSocket": [str(cpus)],
        "ThreadsPerCore": ["1"],
        "RealMemory": [str(real_memory)],
    }
    if partition["gpus"]:
        extra_conf["Gres"] = [f"gpu:{partition['gpus']}"]
    if partition["features"]:
        extra_conf["Features"] = list(partition["features"])
    partition_conf: dict[str, str] = {
        "Default": "YES" if partition["default"] else "NO",
        "State": "UP",
        "MaxTime": partition["max_time"],
        # A job that names no --mem gets its share of the node per CPU rather
        # than all of it (Slurm's DefMemPerNode default), so eight one-core
        # jobs share an eight-core worker instead of serialising on it.
        "DefMemPerCPU": str(max(real_memory // cpus, 1)),
    }
    if partition["exclusive"]:
        partition_conf["OverSubscribe"] = "EXCLUSIVE"
    return {
        "enabled": True,
        # Autoscaled partitions start at min_nodes; KEDA owns the count after
        # install through the NodeSet scale subresource.
        "replicas": int(partition["min_nodes"]),
        "slurmd": {
            # Requests equal limits: the pod is the Slurm node, and Slinky
            # sizes the node from the pod's resources, so a burstable pod
            # would advertise capacity it may not get.
            "resources": {"requests": dict(resources), "limits": dict(resources)},
            "volumeMounts": copy.deepcopy(mounts),
        },
        "extraConfMap": extra_conf,
        "partition": {"enabled": True, "configMap": partition_conf},
        "podSpec": {
            "nodeSelector": node_selector,
            "tolerations": tolerations,
            "volumes": copy.deepcopy(volumes),
        },
    }


def _scaled_object(
    name: str, partition: Mapping[str, Any], autoscaling: Mapping[str, Any]
) -> dict[str, Any]:
    min_nodes = int(partition["min_nodes"])
    spec: dict[str, Any] = {
        "scaleTargetRef": {
            "apiVersion": "slinky.slurm.net/v1beta1",
            "kind": "NodeSet",
            "name": nodeset_name(name),
        },
        "pollingInterval": int(autoscaling["polling_interval_seconds"]),
        "cooldownPeriod": int(autoscaling["cooldown_seconds"]),
        # KEDA only supports idleReplicaCount 0; with a floor of one or more
        # the HPA's minReplicas is the floor instead.
        "minReplicaCount": max(min_nodes, 1),
        "maxReplicaCount": int(partition["max_nodes"]),
        "advanced": {
            "horizontalPodAutoscalerConfig": {
                "behavior": {
                    # Grow fast (jobs are waiting), shrink one node every two
                    # minutes after ten quiet ones: the operator drains a
                    # worker before removing it, so a hasty scale-in only
                    # churns nodes.
                    "scaleUp": {
                        "stabilizationWindowSeconds": 0,
                        "selectPolicy": "Max",
                        "policies": [{"type": "Pods", "value": 4, "periodSeconds": 60}],
                    },
                    "scaleDown": {
                        "stabilizationWindowSeconds": 600,
                        "selectPolicy": "Min",
                        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 120}],
                    },
                }
            }
        },
        "triggers": [
            {
                "type": "prometheus",
                "metricType": "Value",
                "metadata": {
                    "serverAddress": SLURM_PROMETHEUS_URL,
                    "query": PENDING_JOBS_QUERY.format(partition=name),
                    "threshold": str(int(autoscaling["pending_jobs_threshold"])),
                },
            },
            {
                "type": "prometheus",
                "metricType": "Value",
                "metadata": {
                    "serverAddress": SLURM_PROMETHEUS_URL,
                    "query": LARGEST_PENDING_JOB_QUERY.format(partition=name),
                    "threshold": "1",
                },
            },
        ],
    }
    if min_nodes == 0:
        spec["idleReplicaCount"] = 0
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {
            "name": scaled_object_name(name),
            "namespace": SLURM_NAMESPACE,
            "labels": {
                "project": "gco",
                SLURM_PART_OF_LABEL: SLURM_PART_OF_VALUE,
                "gco.io/slurm-partition": name,
            },
        },
        "spec": spec,
    }


def slurm_chart_values(config: Mapping[str, Any], *, fsx_enabled: bool) -> dict[str, Any]:
    """The ``slurm`` chart values GCO derives from a validated ``helm.slurm`` block.

    The regional stack hands them to the helm installer, which deep-merges
    them over the static hardening values in ``charts.yaml`` (image pins,
    resource requests, the disabled chart partition). Everything that varies
    per deployment is here: the cluster name, the controller's state volume,
    slurmrestd replicas and PDB, accounting and its database connection, the
    metrics switch, the login pod, every NodeSet with its partition line, the
    cgroup/gres config files, the extra slurm.conf lines, the shared-storage
    mounts, and one KEDA ScaledObject per autoscaled partition rendered
    through the chart's ``extraObjects``. A list value in the result replaces
    the list in charts.yaml (the installer's merge replaces scalars and lists
    and recurses into maps).
    """
    partitions = enabled_partitions(config)
    any_gpu = any(p["gpus"] for p in partitions.values())
    volumes, mounts = _shared_volumes(config, fsx_enabled=fsx_enabled)
    control_plane = _control_plane_pod_spec()

    slurm_conf = _conf_map(BASE_SLURM_CONF, GPU_SLURM_CONF if any_gpu else {}, config["slurm_conf"])
    config_files: dict[str, str] = {"cgroup.conf": CGROUP_CONF}
    if any_gpu:
        config_files["gres.conf"] = GRES_CONF

    values: dict[str, Any] = {
        "clusterName": config["cluster_name"],
        "configFiles": config_files,
        "controller": {
            "metadata": {"annotations": {"karpenter.sh/do-not-disrupt": "true"}},
            "persistence": {
                "enabled": True,
                "existingClaim": "",
                "storageClassName": SLURM_STORAGE_CLASS,
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {"storage": str(config["controller"]["state_storage_size"])}
                },
            },
            "extraConfMap": slurm_conf,
            "metrics": {
                "enabled": bool(config["metrics"]["enabled"]),
                # GCO ships the ServiceMonitor itself (gated on observability,
                # post-helm-monitoring-servicemonitors.yaml) so the operator
                # never depends on a CRD that may not be installed.
                "serviceMonitor": {"enabled": False},
            },
            "podSpec": copy.deepcopy(control_plane),
        },
        "restapi": {
            "replicas": int(config["restapi"]["replicas"]),
            "pdb": {"enabled": int(config["restapi"]["replicas"]) > 1, "minAvailable": 1},
            "metadata": {"annotations": {"karpenter.sh/do-not-disrupt": "true"}},
            "podSpec": copy.deepcopy(control_plane),
        },
        "accounting": _accounting_values(config, control_plane),
        "loginsets": {
            SLURM_LOGIN_SET_KEY: {
                "enabled": bool(config["login"]["enabled"]),
                "replicas": int(config["login"]["replicas"]),
                "login": {"volumeMounts": copy.deepcopy(mounts)},
                # ClusterIP: reached with `gco slurm shell` over the cluster
                # tunnel, never a load balancer.
                "service": {"spec": {"type": "ClusterIP"}},
                "podSpec": {
                    **_platform_pod_placement(),
                    "initContainers": _home_init_containers(config),
                    "volumes": copy.deepcopy(volumes),
                },
            }
        },
        "nodesets": {
            name: _nodeset_values(name, spec, volumes=volumes, mounts=mounts)
            for name, spec in partitions.items()
        },
        # Partitions are one per NodeSet (above); the chart's catch-all is off
        # so exactly one declared partition is Slurm's default.
        "partitions": {"all": {"enabled": False}},
        "extraObjects": [
            _scaled_object(name, partitions[name], config["autoscaling"])
            for name in autoscaled_partitions(config)
        ],
    }
    return values


def _accounting_values(config: Mapping[str, Any], control_plane: dict[str, Any]) -> dict[str, Any]:
    accounting = config["accounting"]
    if not accounting["enabled"]:
        return {"enabled": False}
    external = accounting["external"]
    if external["host"]:
        storage = {
            "host": external["host"],
            "port": int(external["port"]),
            "database": external["database"],
            "username": external["username"],
            "passwordKeyRef": {
                "name": external["password_secret"],
                "key": external["password_key"],
            },
        }
    else:
        storage = {
            "host": SLURM_ACCOUNTING_DB_NAME,
            "port": SLURM_ACCOUNTING_DB_PORT,
            "database": SLURM_ACCOUNTING_DATABASE,
            "username": SLURM_ACCOUNTING_USER,
            "passwordKeyRef": {
                "name": SLURM_ACCOUNTING_DB_SECRET,
                "key": SLURM_ACCOUNTING_DB_PASSWORD_KEY,
            },
        }
    return {
        "enabled": True,
        "storageConfig": storage,
        "extraConfMap": _conf_map(config["slurmdbd_conf"]),
        "metadata": {"annotations": {"karpenter.sh/do-not-disrupt": "true"}},
        "podSpec": copy.deepcopy(control_plane),
    }


# ---------------------------------------------------------------------------
# kubectl-applier replacements
# ---------------------------------------------------------------------------

#: Tokens the Slurm manifests carry. ``{{SLURM_ENABLED}}`` gates the network
#: policies, the gp3 StorageClass, the API's Token and the observability
#: objects; ``{{SLURM_ACCOUNTING_DB_ENABLED}}`` gates the in-cluster MariaDB
#: (with its volume size) and ``{{SLURM_ACCOUNTING_BACKUP_ENABLED}}`` the
#: backup CronJob (with its schedule); retention is an S3 lifecycle rule the
#: regional stack adds from the same block, not a manifest token.
#: ``{{SLURM_API_ENABLED}}`` is different in kind: it is always emitted
#: ("true"/"false") because it sits in the core manifest-processor
#: Deployment (``GCO_SLURM_ENABLED``), where a leftover placeholder would make
#: the applier skip the whole file, exactly like ``{{TRACING_ENABLED}}``.
SLURM_MANIFEST_TOKENS: tuple[str, ...] = (
    "{{SLURM_ENABLED}}",
    "{{SLURM_API_ENABLED}}",
    "{{SLURM_ACCOUNTING_DB_ENABLED}}",
    "{{SLURM_ACCOUNTING_DB_STORAGE_SIZE}}",
    "{{SLURM_ACCOUNTING_BACKUP_ENABLED}}",
    "{{SLURM_ACCOUNTING_BACKUP_SCHEDULE}}",
    "{{SLURM_CLUSTER_NAME}}",
)


def compute_slurm_replacements(config: Mapping[str, Any], *, enabled: bool) -> dict[str, str]:
    """Build the kubectl-applier replacements for the Slurm manifests.

    ``enabled`` is the effective chart enablement (cdk.json toggle or a
    run-scoped ``helm_enabled_overrides``). When false only the always-present
    ``{{SLURM_API_ENABLED}}`` is emitted, as "false": the applier skips every
    Slurm manifest, prunes what an earlier deploy applied, and the API pod
    answers ``/api/v1/slurm/*`` with the enablement remedy. ``{{SLURM_ENABLED}}``
    itself stays with the scheduler gates in the regional stack; this adds the
    accounting and identity tokens.
    """
    if not enabled:
        return {"{{SLURM_API_ENABLED}}": "false"}
    replacements = {
        "{{SLURM_API_ENABLED}}": "true",
        "{{SLURM_CLUSTER_NAME}}": str(config["cluster_name"]),
    }
    if accounting_in_cluster(config):
        accounting = config["accounting"]
        replacements["{{SLURM_ACCOUNTING_DB_ENABLED}}"] = "true"
        replacements["{{SLURM_ACCOUNTING_DB_STORAGE_SIZE}}"] = str(accounting["storage_size"])
        if accounting["backup"]["enabled"]:
            replacements["{{SLURM_ACCOUNTING_BACKUP_ENABLED}}"] = "true"
            replacements["{{SLURM_ACCOUNTING_BACKUP_SCHEDULE}}"] = str(
                accounting["backup"]["schedule"]
            ).strip()
    return replacements
