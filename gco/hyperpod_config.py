"""The ``hyperpod`` cdk.json block: Amazon SageMaker HyperPod as GPU capacity for a Region.

HyperPod here is the EKS-orchestrated flavor: an ``AWS::SageMaker::Cluster``
whose instance groups join this Region's ``gco-<region>`` EKS cluster as
nodes, with HyperPod's deep health checks, health-monitoring agent and
automatic node recovery behind them. GCO's existing schedulers (Kueue,
Volcano, Slinky) place work on those nodes exactly as they place it on
Karpenter capacity; HyperPod adds resilience and, through Training Plans,
reserved GPU capacity for a fixed window.

This module is the single source of truth for the block's shape: defaults,
validation, the run-scoped ``hyperpod_overrides`` context, the Kubernetes
labels and taint HyperPod nodes carry, the per-Region health-monitoring agent
image, and the kubectl-applier replacement tokens. It imports nothing from
``aws_cdk`` so the CLI and the live-validation harness can use it without
the CDK toolchain, exactly like :mod:`gco.pcs_config`.

Block (``hyperpod`` in cdk.json; ``hyperpod_regions.<region>`` overlays it)::

    "hyperpod": {
      "enabled": false,
      "cluster_name": null,            # default <project>-hyperpod
      "node_recovery": "Automatic",    # or "None"
      "availability_zone": null,       # default: the first EKS-supported private subnet's AZ
      "instance_groups": [
        {
          "name": "accelerated",
          "instance_type": "ml.g5.8xlarge",
          "instance_count": 1,
          "threads_per_core": 1,
          "deep_health_checks": [],       # opt in: ["InstanceStress", "InstanceConnectivity"]
          "training_plan_arn": null,
          "root_volume_size_gb": 100
        }
      ],
      "queue": {"max_cpu": "1000", "max_memory": "4Ti", "max_gpu": "64"},
      "removal_policy": "destroy"
    }
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

#: The cdk.json block and its per-Region overlay.
HYPERPOD_FEATURE_KEY = "hyperpod"
HYPERPOD_REGIONS_KEY = "hyperpod_regions"

#: Run-scoped CDK context carrying a JSON object deep-merged over the block
#: for one deploy (``--context hyperpod_overrides='{"enabled": true}'``); the
#: live release harness turns HyperPod on this way.
HYPERPOD_OVERRIDES_CONTEXT_KEY = "hyperpod_overrides"

#: Node labels GCO sets through the instance group's ``KubernetesConfig``.
#: ``CAPACITY_PROVIDER_LABEL`` is the placement key: the Kueue ResourceFlavor's
#: ``nodeLabels``, the inference ``--capacity-type hyperpod`` nodeSelector and
#: the jobs API's placement report all read it. Karpenter nodes carry
#: ``karpenter.sh/capacity-type`` instead, so the two never overlap.
CAPACITY_PROVIDER_LABEL = "gco.aws/capacity-provider"
CAPACITY_PROVIDER_HYPERPOD = "hyperpod"
INSTANCE_GROUP_LABEL = "gco.aws/hyperpod-instance-group"

#: The taint every HyperPod node carries so only work that asked for HyperPod
#: lands there: platform pods stay on the Auto Mode node pools, and a job
#: must tolerate it (the Kueue flavor adds the toleration for admitted
#: workloads).
HYPERPOD_TAINT_KEY = "gco.aws/hyperpod"
HYPERPOD_TAINT_VALUE = "true"
HYPERPOD_TAINT_EFFECT = "NoSchedule"

#: HyperPod's own node labels (resiliency); reported with placement.
NODE_HEALTH_STATUS_LABEL = "sagemaker.amazonaws.com/node-health-status"
DEEP_HEALTH_CHECK_STATUS_LABEL = "sagemaker.amazonaws.com/deep-health-check-status"
NODE_HEALTH_SCHEDULABLE = "Schedulable"

#: Kubernetes objects the HyperPod control plane expects on the EKS cluster
#: (the ``aws-hyperpod`` namespace name is fixed by the service).
HYPERPOD_NAMESPACE = "aws-hyperpod"
HEALTH_MONITORING_AGENT_NAME = "health-monitoring-agent"
DEEP_HEALTH_CHECK_SERVICE_ACCOUNT = "deep-health-check-service-account"
JOB_AUTO_RESTART_NAME = "job-auto-restart"

#: The health-monitoring agent image the HyperPod team publishes per Region
#: (account differs by Region; the tag is one pin). Source: the "Supported
#: Regions and their ECR URIs" table in aws/sagemaker-hyperpod-cli's
#: helm_chart/readme.md. A Region missing here cannot enable HyperPod.
HEALTH_MONITORING_AGENT_TAG = "1.0.2710.0_1.0.501.0"
HEALTH_MONITORING_AGENT_ACCOUNTS: Mapping[str, str] = {
    "us-east-1": "767398015722",
    "us-west-2": "905418368575",
    "us-east-2": "851725546812",
    "us-west-1": "011528288828",
    "eu-central-1": "211125453373",
    "eu-north-1": "654654141839",
    "eu-west-1": "533267293120",
    "eu-west-2": "011528288831",
    "ap-northeast-1": "533267052152",
    "ap-south-1": "011528288864",
    "ap-southeast-1": "905418428165",
    "ap-southeast-2": "851725636348",
    "sa-east-1": "025066253954",
}

#: ``OnStartDeepHealthChecks`` values the API accepts. Off by default (see
#: :data:`INSTANCE_GROUP_DEFAULTS`): SageMaker runs them on every instance it
#: creates or replaces and holds the instance for about two hours while they
#: run, and it accepts ``InstanceConnectivity`` only for instance families
#: with multi-node GPU networking (``CreateCluster`` named G5 and G6 from
#: 8xlarge up, P4, P5, P6 and Trn on 2026-10-31, and refused ``ml.g4dn.xlarge``
#: with that list). The service validates the pairing; GCO does not keep a
#: copy of the family list.
DEEP_HEALTH_CHECK_TYPES: tuple[str, ...] = ("InstanceStress", "InstanceConnectivity")
NODE_RECOVERY_MODES: tuple[str, ...] = ("Automatic", "None")
REMOVAL_POLICIES: tuple[str, ...] = ("destroy", "retain")

#: HyperPod instance types are the ``ml.`` family names. The service validates
#: the exact catalog; this only rejects something that is not an ``ml.`` type.
INSTANCE_TYPE_PATTERN = re.compile(r"^ml\.[a-z0-9]+(-[a-z0-9]+)?\.[a-z0-9]+$")
#: ``ClusterName`` / ``InstanceGroupName`` pattern from the API.
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")
TRAINING_PLAN_ARN_PATTERN = re.compile(
    r"^arn:aws[a-z\-]*:sagemaker:[a-z0-9\-]+:[0-9]{12}:training-plan/.+$"
)
#: Kubernetes quantity, as the Kueue ClusterQueue nominal quotas accept it.
QUANTITY_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?([EPTGMk]i?|m)?$")
#: An Availability Zone name (``us-east-1a``), the only zone form a cluster takes.
AVAILABILITY_ZONE_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d[a-z]$")

#: Kueue objects the gated manifest ships when HyperPod and Kueue are both on.
HYPERPOD_RESOURCE_FLAVOR = "gco-hyperpod-flavor"
HYPERPOD_CLUSTER_QUEUE = "gco-hyperpod-queue"
HYPERPOD_LOCAL_QUEUE = "gco-hyperpod"

#: Regional-stack CfnOutput keys (the CLI reads them by exact key).
HYPERPOD_OUTPUT_CLUSTER_ARN = "HyperpodClusterArn"
HYPERPOD_OUTPUT_CLUSTER_NAME = "HyperpodClusterName"
HYPERPOD_OUTPUT_EXECUTION_ROLE_ARN = "HyperpodExecutionRoleArn"
HYPERPOD_OUTPUT_SECURITY_GROUP_ID = "HyperpodSecurityGroupId"
HYPERPOD_OUTPUT_INSTANCE_GROUPS = "HyperpodInstanceGroups"

INSTANCE_GROUP_DEFAULTS: dict[str, Any] = {
    "name": "accelerated",
    "instance_type": "ml.g5.8xlarge",
    "instance_count": 1,
    "threads_per_core": 1,
    "deep_health_checks": [],
    "training_plan_arn": None,
    "root_volume_size_gb": 100,
}

HYPERPOD_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "cluster_name": None,
    "node_recovery": "Automatic",
    "availability_zone": None,
    "instance_groups": [copy.deepcopy(INSTANCE_GROUP_DEFAULTS)],
    # Nominal quotas of the Kueue ClusterQueue that fronts the HyperPod nodes.
    # Admission caps only: the instance groups are the real limit, so the
    # defaults are generous rather than derived from the instance types.
    "queue": {"max_cpu": "1000", "max_memory": "4Ti", "max_gpu": "64"},
    "removal_policy": "destroy",
}

_INSTANCE_GROUP_KEYS = tuple(INSTANCE_GROUP_DEFAULTS)
_QUEUE_KEYS = ("max_cpu", "max_memory", "max_gpu")
_HYPERPOD_KEYS = tuple(HYPERPOD_DEFAULTS)


class HyperpodConfigError(ValueError):
    """Raised when the ``hyperpod`` block is malformed."""


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
        raise HyperpodConfigError(
            f"{path} contains unknown key(s): {', '.join(unknown)}; "
            f"allowed keys: {', '.join(sorted(allowed_set))}"
        )


def _require_object(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HyperpodConfigError(f"{path} must be an object, got {value!r}")
    return value


def _require_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise HyperpodConfigError(f"{path} must be a boolean, got {value!r}")
    return value


def _require_int(value: object, bounds: tuple[int, int], path: str) -> int:
    """An exact integer (``bool`` rejected) inside the inclusive ``bounds``."""
    low, high = bounds
    if type(value) is not int or not low <= value <= high:
        raise HyperpodConfigError(
            f"{path} must be an integer between {low} and {high}, got {value!r}"
        )
    return value


def _require_choice(value: object, choices: Iterable[str], path: str) -> str:
    options = tuple(choices)
    if not isinstance(value, str) or value not in options:
        raise HyperpodConfigError(f"{path} must be one of {', '.join(options)}, got {value!r}")
    return value


def _require_name(value: object, path: str) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.match(value):
        raise HyperpodConfigError(
            f"{path} must be 1-63 letters, digits and hyphens starting and ending with a "
            f"letter or digit, got {value!r}"
        )
    return value


def _require_quantity(value: object, path: str) -> str:
    if not isinstance(value, str) or not QUANTITY_PATTERN.match(value.strip()):
        raise HyperpodConfigError(
            f"{path} must be a Kubernetes quantity string (for example 1000, 4Ti), got {value!r}"
        )
    return value.strip()


def _validate_instance_group(raw: object, path: str) -> dict[str, Any]:
    block = _require_object(raw, path)
    _reject_unknown_keys(block, _INSTANCE_GROUP_KEYS, path)
    group = _deep_merge(INSTANCE_GROUP_DEFAULTS, block)
    group["name"] = _require_name(group["name"], f"{path}.name")
    instance_type = group["instance_type"]
    if not isinstance(instance_type, str) or not INSTANCE_TYPE_PATTERN.match(instance_type):
        raise HyperpodConfigError(
            f"{path}.instance_type must be a SageMaker HyperPod instance type such as "
            f"ml.g5.8xlarge, got {instance_type!r}"
        )
    group["instance_count"] = _require_int(
        group["instance_count"], (0, 10000), f"{path}.instance_count"
    )
    group["threads_per_core"] = _require_int(
        group["threads_per_core"], (1, 2), f"{path}.threads_per_core"
    )
    checks = group["deep_health_checks"]
    if not isinstance(checks, list) or any(
        not isinstance(check, str) or check not in DEEP_HEALTH_CHECK_TYPES for check in checks
    ):
        raise HyperpodConfigError(
            f"{path}.deep_health_checks must be a list drawn from "
            f"{', '.join(DEEP_HEALTH_CHECK_TYPES)}, got {checks!r}"
        )
    group["deep_health_checks"] = list(dict.fromkeys(checks))
    arn = group["training_plan_arn"]
    if arn is not None and (not isinstance(arn, str) or not TRAINING_PLAN_ARN_PATTERN.match(arn)):
        raise HyperpodConfigError(
            f"{path}.training_plan_arn must be a SageMaker training-plan ARN or null, got {arn!r}"
        )
    group["root_volume_size_gb"] = _require_int(
        group["root_volume_size_gb"], (1, 16384), f"{path}.root_volume_size_gb"
    )
    return group


def _validate_queue(raw: object, path: str) -> dict[str, str]:
    block = _require_object(raw, path)
    _reject_unknown_keys(block, _QUEUE_KEYS, path)
    merged = _deep_merge(HYPERPOD_DEFAULTS["queue"], block)
    return {key: _require_quantity(merged[key], f"{path}.{key}") for key in _QUEUE_KEYS}


# ---------------------------------------------------------------------------
# Public validation
# ---------------------------------------------------------------------------


def validate_hyperpod_config(raw: object) -> dict[str, Any]:
    """Return the ``hyperpod`` block merged over the defaults, or raise.

    ``None`` (no block) is the defaults: HyperPod off. Unknown keys fail by
    name. Instance groups need unique names; an enabled block needs at least
    one instance group.
    """
    if raw is None:
        return copy.deepcopy(HYPERPOD_DEFAULTS)
    block = _require_object(raw, HYPERPOD_FEATURE_KEY)
    _reject_unknown_keys(block, _HYPERPOD_KEYS, HYPERPOD_FEATURE_KEY)
    config = _deep_merge(HYPERPOD_DEFAULTS, block)
    config["enabled"] = _require_bool(config["enabled"], "hyperpod.enabled")
    if config["cluster_name"] is not None:
        config["cluster_name"] = _require_name(config["cluster_name"], "hyperpod.cluster_name")
    config["node_recovery"] = _require_choice(
        config["node_recovery"], NODE_RECOVERY_MODES, "hyperpod.node_recovery"
    )
    zone = config["availability_zone"]
    if zone is not None and (
        not isinstance(zone, str) or not AVAILABILITY_ZONE_PATTERN.match(zone)
    ):
        raise HyperpodConfigError(
            f"hyperpod.availability_zone must be an Availability Zone name such as us-east-1a "
            f"or null, got {zone!r}"
        )
    groups_raw = config["instance_groups"]
    if not isinstance(groups_raw, list):
        raise HyperpodConfigError(
            f"hyperpod.instance_groups must be a list of objects, got {groups_raw!r}"
        )
    groups = [
        _validate_instance_group(group, f"hyperpod.instance_groups[{index}]")
        for index, group in enumerate(groups_raw)
    ]
    names = [group["name"] for group in groups]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise HyperpodConfigError(
            f"hyperpod.instance_groups names must be unique; duplicated: {', '.join(duplicates)}"
        )
    if config["enabled"] and not groups:
        raise HyperpodConfigError("hyperpod.instance_groups must name at least one instance group")
    config["instance_groups"] = groups
    config["queue"] = _validate_queue(config["queue"], "hyperpod.queue")
    config["removal_policy"] = _require_choice(
        config["removal_policy"], REMOVAL_POLICIES, "hyperpod.removal_policy"
    )
    return config


# ---------------------------------------------------------------------------
# Run-scoped overrides
# ---------------------------------------------------------------------------


def parse_hyperpod_overrides(raw: object) -> dict[str, Any]:
    """Parse the ``hyperpod_overrides`` context value into a mapping.

    Accepts a JSON object string (the only shape ``cdk --context`` can carry)
    or a mapping; ``None`` and blank strings mean no overrides. Only the shape
    is checked here: the merged block goes through
    :func:`validate_hyperpod_config` as a whole.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return copy.deepcopy(dict(raw))
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HyperpodConfigError(
                f"{HYPERPOD_OVERRIDES_CONTEXT_KEY} must be a JSON object: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise HyperpodConfigError(
                f"{HYPERPOD_OVERRIDES_CONTEXT_KEY} must be a JSON object, "
                f"got {type(parsed).__name__}"
            )
        return parsed
    raise HyperpodConfigError(
        f"{HYPERPOD_OVERRIDES_CONTEXT_KEY} must be a JSON object string or an object, "
        f"got {type(raw).__name__}"
    )


def apply_hyperpod_overrides(raw: object, overrides: object) -> object:
    """``raw`` (the block as cdk.json has it, possibly ``None``) with the overrides merged in."""
    parsed = parse_hyperpod_overrides(overrides)
    if not parsed or (raw is not None and not isinstance(raw, dict)):
        return raw
    return _deep_merge(raw or {}, parsed)


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------


def hyperpod_enabled(config: Mapping[str, Any]) -> bool:
    """True when the block turns HyperPod on (after any run-scoped override)."""
    return bool(config.get("enabled"))


def cluster_name(config: Mapping[str, Any], project_name: str) -> str:
    """The HyperPod cluster name: the configured one or ``<project>-hyperpod``."""
    configured = config.get("cluster_name")
    return str(configured) if configured else f"{project_name}-hyperpod"


def instance_group_names(config: Mapping[str, Any]) -> list[str]:
    """The instance groups in declaration order."""
    return [str(group["name"]) for group in config["instance_groups"]]


def health_monitoring_agent_image(region: str) -> str:
    """The health-monitoring agent image for ``region``, or raise if HyperPod has none there."""
    account = HEALTH_MONITORING_AGENT_ACCOUNTS.get(region)
    if account is None:
        supported = ", ".join(sorted(HEALTH_MONITORING_AGENT_ACCOUNTS))
        raise HyperpodConfigError(
            f"SageMaker HyperPod publishes no health-monitoring agent for {region}; "
            f"supported Regions: {supported}"
        )
    return (
        f"{account}.dkr.ecr.{region}.amazonaws.com/hyperpod-health-monitoring-agent:"
        f"{HEALTH_MONITORING_AGENT_TAG}"
    )


def node_labels(group_name: str) -> dict[str, str]:
    """The labels an instance group's nodes carry."""
    return {
        CAPACITY_PROVIDER_LABEL: CAPACITY_PROVIDER_HYPERPOD,
        INSTANCE_GROUP_LABEL: group_name,
    }


def node_taints() -> list[dict[str, str]]:
    """The taint every HyperPod node carries (``KubernetesConfig.Taints`` shape)."""
    return [
        {"key": HYPERPOD_TAINT_KEY, "value": HYPERPOD_TAINT_VALUE, "effect": HYPERPOD_TAINT_EFFECT}
    ]


def hyperpod_toleration() -> dict[str, str]:
    """The pod toleration that admits work onto HyperPod nodes."""
    return {
        "key": HYPERPOD_TAINT_KEY,
        "operator": "Equal",
        "value": HYPERPOD_TAINT_VALUE,
        "effect": HYPERPOD_TAINT_EFFECT,
    }


# ---------------------------------------------------------------------------
# kubectl-applier replacement tokens
# ---------------------------------------------------------------------------

#: Emitted only when HyperPod is on; their absence gates the HyperPod
#: manifests out and prunes what they created.
HYPERPOD_GATED_TOKENS: tuple[str, ...] = (
    "{{HYPERPOD_ENABLED}}",
    "{{HYPERPOD_HMA_IMAGE}}",
    "{{HYPERPOD_QUEUE_MAX_CPU}}",
    "{{HYPERPOD_QUEUE_MAX_MEMORY}}",
    "{{HYPERPOD_QUEUE_MAX_GPU}}",
)


def compute_hyperpod_replacements(
    config: Mapping[str, Any], *, enabled: bool, region: str
) -> dict[str, str]:
    """The kubectl-applier replacements for the HyperPod manifests.

    Nothing is emitted when HyperPod is off, so every ``{{HYPERPOD_*}}`` token
    stays unreplaced, the applier skips the gated files, and
    ``_FEATURE_RESOURCE_INVENTORY`` prunes what an earlier deploy created.
    """
    if not enabled:
        return {}
    queue = config["queue"]
    return {
        "{{HYPERPOD_ENABLED}}": "true",
        "{{HYPERPOD_HMA_IMAGE}}": health_monitoring_agent_image(region),
        "{{HYPERPOD_QUEUE_MAX_CPU}}": str(queue["max_cpu"]),
        "{{HYPERPOD_QUEUE_MAX_MEMORY}}": str(queue["max_memory"]),
        "{{HYPERPOD_QUEUE_MAX_GPU}}": str(queue["max_gpu"]),
    }
