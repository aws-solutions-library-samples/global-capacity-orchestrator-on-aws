"""
Configuration loader for GCO (Global Capacity Orchestrator on AWS).

This module loads and validates configuration from CDK context (cdk.json).
It provides type-safe access to all configuration values with sensible defaults
and comprehensive validation.

Configuration Sections:
- project_name: Unique identifier for the deployment
- regions: List of AWS regions to deploy to
- kubernetes_version: EKS Kubernetes version
- resource_thresholds: CPU/memory/GPU utilization thresholds
- global_accelerator: Global Accelerator settings
- gateway_health_check: Target-group health check of the platform ALB
- inference_proxy: Shared inference TLS proxy CPU request and HPA target
- manifest_processor: Manifest validation and resource limits
- api_gateway: Throttling and logging configuration
- tracing: OpenTelemetry -> AWS X-Ray tracing for the API services
- tags: Common tags applied to all resources

Usage:
    config = ConfigLoader(app)
    regions = config.get_regions()
    cluster_config = config.get_cluster_config("us-east-1")
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

import boto3
from aws_cdk import App

from gco.argocd_config import ArgoCdConfigError, validate_argocd_config
from gco.eks_capabilities_config import (
    EKS_CAPABILITIES_CONTEXT_KEY,
    EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY,
    EksCapabilitiesConfigError,
    merge_eks_capabilities_overrides,
    parse_eks_capabilities_overrides,
    validate_eks_capabilities_config,
)
from gco.enablement_overrides import HELM_CHART_CONFIG_KEYS
from gco.fsx_lustre_config import (
    FSX_AUTO_IMPORT_POLICIES,
    FSX_CAPACITY_INCREMENT_GIB,
    FSX_DATA_COMPRESSION_TYPES,
    FSX_DEPLOYMENT_TYPES,
    FSX_FILE_SYSTEM_TYPE_VERSIONS,
    FSX_LUSTRE_CONTEXT_KEY,
    FSX_LUSTRE_DEFAULTS,
    FSX_LUSTRE_REGIONS_CONTEXT_KEY,
    FSX_THROUGHPUT_CHOICES,
    default_per_unit_storage_throughput,
    valid_storage_capacity,
)
from gco.gateway_health_check_config import (
    GATEWAY_HEALTH_CHECK_CONTEXT_KEY,
    GatewayHealthCheckConfigError,
    validate_gateway_health_check_config,
)
from gco.hyperpod_config import (
    HYPERPOD_FEATURE_KEY,
    HYPERPOD_OVERRIDES_CONTEXT_KEY,
    HYPERPOD_REGIONS_KEY,
    HyperpodConfigError,
    apply_hyperpod_overrides,
    validate_hyperpod_config,
)
from gco.inference_proxy_config import (
    INFERENCE_PROXY_MAX_REPLICAS_DEFAULT,
    INFERENCE_PROXY_MIN_REPLICAS_DEFAULT,
    INFERENCE_PROXY_TLS_CPU_REQUEST_MILLICORES_DEFAULT,
    INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION_DEFAULT,
)
from gco.k8s_quantity import memory_bytes
from gco.manifest_security_policy import validate_manifest_security_policy
from gco.models import ClusterConfig, ResourceThresholds
from gco.pcs_config import (
    PCS_FEATURE_KEY,
    PCS_OVERRIDES_CONTEXT_KEY,
    PCS_REGIONS_KEY,
    PcsConfigError,
    apply_pcs_overrides,
    validate_pcs_config,
)
from gco.resource_governance import parse_k8s_quantity, validated_resource_quota
from gco.slurm_config import SlurmConfigError, validate_slurm_config
from gco.stacks.constants import (
    CONTROL_PLANE_ONLY_CONTEXT_KEY,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    control_plane_only_requested,
    known_cloudformation_regions,
    validated_deployment_partition,
    validated_regional_deployment_regions,
    validated_request_body_limit,
)

logger = logging.getLogger(__name__)

#: Top-level cdk.json blocks that no longer take effect, each with what to do
#: instead. ``gco upgrade`` keeps an operator's cdk.json byte for byte, so a
#: file that still carries one validates, with a warning naming the block;
#: nothing reads its values.
DEPRECATED_CONTEXT_KEYS: dict[str, str] = {
    "alb_config": (
        "it never configured the platform ALB. Remove it from cdk.json; set "
        "gateway_health_check (interval_seconds, timeout_seconds, healthy_threshold, "
        "unhealthy_threshold) to change the ALB target-group health check, which "
        "defaults to the 15-second interval, 5-second timeout and thresholds of 2 "
        "that have always been deployed."
    ),
}

#: Keys inside known blocks that no longer take effect, by full path, each
#: with what to do instead. Like :data:`DEPRECATED_CONTEXT_KEYS` they still
#: validate, with a warning, and nothing reads them.
DEPRECATED_BLOCK_KEYS: dict[str, str] = {
    "manifest_processor.image": (
        "nothing reads it: the regional stack builds the manifest processor image from "
        "dockerfiles/Dockerfile.manifest-processor and deploys it from ECR. Remove it "
        "from cdk.json."
    ),
    "global_accelerator.health_check_grace_period": (
        "Global Accelerator endpoint groups have no grace period, so it never took "
        "effect. Remove it from cdk.json."
    ),
    "global_accelerator.health_check_timeout": (
        "Global Accelerator endpoint groups have no probe timeout setting, so it never "
        "took effect. Remove it from cdk.json."
    ),
}

#: The charts of the ``helm`` block that take nothing but ``enabled``.
#: ``helm.argocd`` and ``helm.slurm`` have their own validators
#: (gco/argocd_config.py, gco/slurm_config.py).
_SIMPLE_HELM_CHARTS = HELM_CHART_CONFIG_KEYS - {"argocd", "slurm"}

#: Every key each cdk.json block may hold, for the blocks whose own validator
#: does not reject unknown keys already (``fsx_lustre``, ``valkey``,
#: ``aurora_pgvector``, ``tracing``, ``gateway_health_check``,
#: ``inference_proxy``, ``vpc_endpoints``, ``eks_capabilities``, ``pcs``,
#: ``hyperpod``, ``helm.argocd``, ``helm.slurm`` and
#: ``job_validation_policy.manifest_security_policy`` do). A dotted path is a
#: sub-block, listed after its parent. Each block must be an object when
#: present, a key outside its set fails validation with the full path, and
#: ``_comment*`` keys are always allowed. ``inference_monitor`` is read by
#: the inference monitor's own manifest and is not listed.
BLOCK_KEYS: dict[str, frozenset[str]] = {
    "deployment_regions": frozenset({"global", "api_gateway", "monitoring", "regional"}),
    "bedrock": frozenset(
        {
            "mission_default_model_id",
            "capacity_advisor_default_model_id",
            "claude_code_default_model_id",
            "codex_default_model_id",
            "codex",
            "opencode_default_model_id",
            "embedding_model_id",
            "generation_reasoning",
            # Retired keys gco/bedrock.py reports with rename instructions.
            "default_model_id",
            "thinking",
        }
    ),
    "bedrock.codex": frozenset({"reasoning_effort"}),
    "bedrock.generation_reasoning": frozenset({"effort"}),
    "eks_cluster": frozenset(
        {"endpoint_access", "public_access_cidrs", "developer_access", "network_policy_enforcement"}
    ),
    "global_accelerator": frozenset(
        {
            "name",
            "health_check_interval",
            "health_check_threshold",
            "health_check_path",
            "client_affinity",
            "traffic_dial",
            # Deprecated (DEPRECATED_BLOCK_KEYS): warned about and ignored.
            "health_check_grace_period",
            "health_check_timeout",
        }
    ),
    "global_accelerator.traffic_dial": frozenset(
        {
            "enabled",
            "mode",
            "interval_minutes",
            "lookback_minutes",
            "min_dial_percentage",
            "max_step_percentage",
            "full_health_percentage",
        }
    ),
    "backend_tls": frozenset(
        {
            "root_generation",
            "root_validity_days",
            "root_rotate_before_days",
            "root_activation_delay_hours",
            "root_overlap_days",
            "leaf_validity_days",
            "leaf_rotate_before_days",
            "rotation_schedule_hours",
            "trust_cache_ttl_seconds",
            "trust_cache_max_stale_seconds",
        }
    ),
    "api_gateway": frozenset(
        {
            "throttle_rate_limit",
            "throttle_burst_limit",
            "log_level",
            "metrics_enabled",
            "tracing_enabled",
            "regional_api_enabled",
        }
    ),
    "waf": frozenset({"per_ip_rate_limit"}),
    "manifest_processor": frozenset(
        {
            "image",  # deprecated (DEPRECATED_BLOCK_KEYS)
            "replicas",
            "resource_limits",
            "autoscaling",
            "validation_enabled",
            "max_request_body_bytes",
            "yaml_max_depth",
            "central_queue_worker_enabled",
            "central_queue_poll_interval_seconds",
            "central_queue_batch_size",
            "central_queue_reconcile_limit",
            "central_queue_lease_seconds",
            "central_queue_lease_renewal_seconds",
        }
    ),
    "manifest_processor.resource_limits": frozenset({"cpu", "memory"}),
    "queue_processor": frozenset(
        {
            "enabled",
            "polling_interval",
            "max_concurrent_jobs",
            "messages_per_job",
            "successful_jobs_history",
            "failed_jobs_history",
        }
    ),
    "job_validation_policy": frozenset(
        {
            "allowed_namespaces",
            "resource_quotas",
            "require_accelerator_toleration",
            "trusted_registries",
            "trusted_dockerhub_orgs",
            "manifest_security_policy",
            "allowed_kinds",
        }
    ),
    "job_validation_policy.resource_quotas": frozenset(
        {"max_cpu_per_manifest", "max_memory_per_manifest", "max_gpu_per_manifest"}
    ),
    "resource_thresholds": frozenset(
        {
            "cpu_threshold",
            "memory_threshold",
            "gpu_threshold",
            "pending_pods_threshold",
            "pending_requested_cpu_vcpus",
            "pending_requested_memory_gb",
            "pending_requested_gpus",
        }
    ),
    "images": frozenset({"removal_policy", "empty_on_delete", "lifecycle", "replication"}),
    "images.lifecycle": frozenset({"keep_tagged", "expire_untagged_days"}),
    "images.replication": frozenset({"enabled", "destinations"}),
    "volcano_image_mirror": frozenset({"enabled", "ecr_namespace"}),
    "s3_access_logs": frozenset({"retention_days"}),
    "workload_metrics": frozenset({"cloudwatch_namespace"}),
    "regional_shared_bucket": frozenset({"removal_policy"}),
    "mission_memory": frozenset(
        {"enabled", "retention_days", "dimensions", "distance_function", "top_k"}
    ),
    "vector_store": frozenset(
        {
            "enabled",
            "dimensions",
            "distance_function",
            "embedding_model_id",
            "replica_regions",
            "corpus_prefix",
        }
    ),
    "analytics_environment": frozenset(
        {"enabled", "hyperpod", "canvas", "cognito", "efs", "studio"}
    ),
    "analytics_environment.hyperpod": frozenset({"enabled"}),
    "analytics_environment.canvas": frozenset({"enabled"}),
    "analytics_environment.cognito": frozenset({"domain_prefix", "removal_policy"}),
    "analytics_environment.efs": frozenset({"removal_policy"}),
    "analytics_environment.studio": frozenset({"user_profile_name_prefix"}),
    "cluster_observability": frozenset(
        {"enabled", "grafana", "prometheus", "alertmanager", "mlflow"}
    ),
    "cluster_observability.grafana": frozenset(
        {"persistence_size", "admin_user", "admin_password_rotation_schedule"}
    ),
    "cluster_observability.prometheus": frozenset({"persistence_size", "retention"}),
    "cluster_observability.alertmanager": frozenset({"enabled", "persistence_size"}),
    "cost_monitoring": frozenset({"enabled", "reports", "athena"}),
    "cost_monitoring.reports": frozenset(
        {"interval_minutes", "retention_days", "transition_to_infrequent_access_days"}
    ),
    "cost_monitoring.athena": frozenset({"query_results_retention_days"}),
    "helm": HELM_CHART_CONFIG_KEYS,
    **{f"helm.{chart}": frozenset({"enabled"}) for chart in sorted(_SIMPLE_HELM_CHARTS)},
    "mcp_server": frozenset({"enabled"}),
    "drift_detection": frozenset({"enabled", "schedule_hours"}),
    "historical": frozenset(
        {
            "enabled",
            "retention_days",
            "poll_interval_minutes",
            "capacity_block_duration_hours",
            "capacity_block_long_duration_hours",
            "spot_score_target_capacities",
            "watch_instance_types",
            "enabled_regions",
        }
    ),
}

#: Top-level context keys GCO reads besides the blocks above: scalars, blocks
#: with their own validators, per-Region overlays, and the run-scoped keys the
#: CLI and the release harnesses pass with ``--context``. A key in GCO's
#: ``snake_case`` style outside this set and the blocks above draws a warning
#: (it is most likely a typo) and is ignored. Keys CDK owns (``@aws-cdk/...``
#: feature flags, ``aws:cdk:...``, lookups such as ``availability-zones:...``)
#: and ``_comment*`` keys never match that style.
OTHER_CONTEXT_KEYS = frozenset(
    {
        "project_name",
        "kubernetes_version",
        "tags",
        "vpc_endpoint_cidrs",
        "vpc_endpoints",
        "inference_proxy",
        "gateway_health_check",
        "inference_monitor",
        "eks_capabilities",
        "eks_capabilities_overrides",
        "fsx_lustre",
        "fsx_lustre_regions",
        "valkey",
        "aurora_pgvector",
        "tracing",
        "tracing_overrides",
        "pcs",
        "pcs_regions",
        "pcs_overrides",
        "hyperpod",
        "hyperpod_regions",
        "hyperpod_overrides",
        "resource_quota",
        "feature_enabled_overrides",
        "helm_enabled_overrides",
        "gco_live_validation_disable_efs_automatic_backups",
        "gco_live_validation_retain_provider_log_groups",
    }
)

#: The style of GCO's own context keys (see :data:`OTHER_CONTEXT_KEYS`).
_GCO_CONTEXT_KEY_STYLE = re.compile(r"^[a-z][a-z0-9_]*$")

#: ``manifest_processor.autoscaling`` defaults. Off: the API tier is I/O-bound
#: (Kubernetes round trips), so CPU is a weak saturation signal; an operator who
#: has measured a CPU-bound API turns it on and the HPA owns the replica count
#: between ``manifest_processor.replicas`` and ``max_replicas``.
_MANIFEST_PROCESSOR_AUTOSCALING_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "max_replicas": 6,
    "cpu_target_utilization_percentage": 70,
}

#: The ``manifest-processor`` container's fixed requests in
#: ``lambda/kubectl-applier-simple/manifests/31-manifest-processor.yaml``;
#: ``manifest_processor.resource_limits`` may not go below them. Pinned to the
#: manifest by ``tests/test_config_loader.py``.
_MANIFEST_PROCESSOR_CONTAINER_REQUESTS: dict[str, str] = {
    "cpu": "500m",
    "memory": "1Gi",
}

#: Gateway VPC endpoints ``vpc_endpoints.gateway`` may name. Free, route-table
#: based, same-region only.
VPC_GATEWAY_ENDPOINT_SERVICES: tuple[str, ...] = ("s3", "dynamodb")

#: Interface (PrivateLink) endpoints ``vpc_endpoints.interface`` may name,
#: keyed the way the regional stack maps them onto
#: ``ec2.InterfaceVpcEndpointAwsService``. Each costs per AZ-hour plus per GB,
#: so the list is opt-in; together they keep every AWS API call the platform
#: and its jobs make inside the VPC (the cross-region calls to the global
#: region's tables and buckets still leave through the NAT gateways).
VPC_INTERFACE_ENDPOINT_SERVICES: tuple[str, ...] = (
    "sts",
    "ecr.api",
    "ecr.dkr",
    "logs",
    "monitoring",
    "sqs",
    "ssm",
    "secretsmanager",
    "kms",
    "eks",
    "elasticfilesystem",
    "bedrock-runtime",
    # OTLP span export from the traced API services (``tracing`` block).
    "xray",
)

_VPC_ENDPOINTS_DEFAULTS: dict[str, list[str]] = {
    "gateway": ["s3", "dynamodb"],
    "interface": [],
}

#: CDK context key that force-enables optional infrastructure features for one
#: deploy without touching cdk.json — the infrastructure sibling of the
#: ``helm_enabled_overrides`` context handled in ``gco/stacks/regional_stack.py``.
#: Used by validation harnesses (``gco examples validate``) whose preflight
#: requires a clean worktree.
FEATURE_OVERRIDE_CONTEXT_KEY = "feature_enabled_overrides"

#: The cdk.json blocks whose ``enabled`` flag the override may force on. Kept
#: deliberately narrow: each of these is a self-contained regional feature the
#: examples or the live validation harness exercise (Aurora pgvector, Valkey
#: Serverless, FSx for Lustre, the vector store, AWS PCS, SageMaker HyperPod).
FEATURE_OVERRIDE_KEYS = frozenset(
    {"aurora_pgvector", "valkey", "fsx_lustre", "vector_store", "pcs", "hyperpod"}
)


def parse_feature_enabled_overrides(raw: object) -> frozenset[str]:
    """Parse and validate the ``feature_enabled_overrides`` context value.

    Accepts a comma-separated string (the only shape the CDK CLI can pass with
    ``--context``) or a list of strings (cdk.json-style). Unknown names raise
    at synth time with the valid list — identical semantics to
    ``_parse_helm_enabled_overrides``.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list) and all(isinstance(part, str) for part in raw):
        names = [part.strip() for part in raw if part.strip()]
    else:
        raise ConfigValidationError(
            f"{FEATURE_OVERRIDE_CONTEXT_KEY} must be a comma-separated string or string list"
        )
    unknown = sorted(set(names) - FEATURE_OVERRIDE_KEYS)
    if unknown:
        valid = ", ".join(sorted(FEATURE_OVERRIDE_KEYS))
        raise ConfigValidationError(
            f"Unknown {FEATURE_OVERRIDE_CONTEXT_KEY} name(s): {', '.join(unknown)}. Valid: {valid}"
        )
    return frozenset(names)


#: cdk.json context key for the OpenTelemetry -> AWS X-Ray tracing block.
TRACING_CONTEXT_KEY = "tracing"

#: Run-scoped CDK context key carrying a JSON object deep-merged over the
#: ``tracing`` block for one deploy (``cdk deploy --context
#: tracing_overrides='{"sample_ratio": 1.0}'``) — the tracing sibling of
#: ``eks_capabilities_overrides``. The live release harness, whose preflight
#: requires a clean worktree, records every trace of its run this way.
TRACING_OVERRIDES_CONTEXT_KEY = "tracing_overrides"

#: The ``tracing`` block with every knob at its default. Tracing is ON by
#: default, like cluster observability: an absent block means the shipped
#: posture, not "off". ``sample_ratio`` is the head-sampling probability of a
#: new trace; ``enable_transaction_search`` lets each regional stack switch on
#: CloudWatch Transaction Search, which the X-Ray OTLP endpoint requires, and
#: only matters while ``enabled`` is true.
TRACING_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sample_ratio": 0.05,
    "enable_transaction_search": True,
}


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``overlay`` recursively merged over a copy of ``base``."""
    merged: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def parse_tracing_overrides(raw: object) -> dict[str, Any]:
    """Parse the ``tracing_overrides`` context value into a mapping.

    Accepts a JSON object string (the only shape ``cdk --context`` can carry)
    or a mapping (cdk.json-style); ``None`` and blank strings mean no
    overrides. Only the shape is checked here: the merged block is validated
    as a whole, so an override that produces an invalid block fails exactly
    like an invalid cdk.json does.
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
            raise ConfigValidationError(
                f"{TRACING_OVERRIDES_CONTEXT_KEY} must be a JSON object: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigValidationError(
                f"{TRACING_OVERRIDES_CONTEXT_KEY} must be a JSON object, got {type(parsed).__name__}"
            )
        return parsed
    raise ConfigValidationError(
        f"{TRACING_OVERRIDES_CONTEXT_KEY} must be a JSON object string or an object, "
        f"got {type(raw).__name__}"
    )


def resolve_tracing_config(raw: object, overrides: object = None) -> dict[str, Any]:
    """Return the validated ``tracing`` block with overrides and defaults applied.

    Single source of truth shared by :meth:`ConfigLoader.get_tracing_config`
    and the regional stack, which falls back to it for config doubles that
    predate the knob. ``overrides`` is the raw ``tracing_overrides`` context
    value. Types are checked exactly: the flags are rendered verbatim into
    the ``GCO_TRACING_ENABLED`` env var and a quoted ``"false"`` in cdk.json
    must not read as enabled, and ``sample_ratio`` must be a real number in
    ``[0, 1]`` because the services treat it as a probability.

    Raises:
        ConfigValidationError: If the block or the overrides are malformed.
    """
    if raw is None:
        block: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        block = raw
    else:
        raise ConfigValidationError(
            f"{TRACING_CONTEXT_KEY} must be an object, got {type(raw).__name__}"
        )
    parsed_overrides = parse_tracing_overrides(overrides)
    merged = _deep_merge(_deep_merge(TRACING_DEFAULTS, block), parsed_overrides)

    unknown = sorted(
        str(key) for key in merged if key not in TRACING_DEFAULTS and not is_comment_key(key)
    )
    if unknown:
        source = f" (after {TRACING_OVERRIDES_CONTEXT_KEY})" if parsed_overrides else ""
        raise ConfigValidationError(
            f"{TRACING_CONTEXT_KEY}{source} contains unknown key(s): "
            + ", ".join(f"{TRACING_CONTEXT_KEY}.{key}" for key in unknown)
            + "; allowed keys: "
            + ", ".join(f"{TRACING_CONTEXT_KEY}.{key}" for key in sorted(TRACING_DEFAULTS))
        )
    for flag in ("enabled", "enable_transaction_search"):
        if type(merged[flag]) is not bool:
            raise ConfigValidationError(
                f"{TRACING_CONTEXT_KEY}.{flag} must be a boolean, got {merged[flag]!r}"
            )
    ratio = merged["sample_ratio"]
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, int | float)
        or not math.isfinite(ratio)
        or not 0 <= ratio <= 1
    ):
        raise ConfigValidationError(
            f"{TRACING_CONTEXT_KEY}.sample_ratio must be a number from 0 to 1 (inclusive), "
            f"got {ratio!r}"
        )
    return {
        "enabled": merged["enabled"],
        "sample_ratio": float(ratio),
        "enable_transaction_search": merged["enable_transaction_search"],
    }


#: ``valkey`` defaults (an ElastiCache Serverless cache per region, off).
_VALKEY_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "max_data_storage_gb": 5,
    "max_ecpu_per_second": 5000,
    "snapshot_retention_limit": 1,
}


#: ``aurora_pgvector`` defaults (Aurora Serverless v2 + pgvector, off).
_AURORA_PGVECTOR_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "min_acu": 0,
    "max_acu": 16,
    "backup_retention_days": 7,
    "deletion_protection": False,
}


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""


#: Keys starting with this prefix are documentation (JSON has no comments, so
#: cdk.json carries ``_comment_<key>`` siblings). Nothing reads them, and the
#: unknown-key checks skip them.
COMMENT_KEY_PREFIX = "_comment"


def is_comment_key(key: object) -> bool:
    """Whether ``key`` is a ``_comment*`` documentation key."""
    return isinstance(key, str) and key.startswith(COMMENT_KEY_PREFIX)


def parse_bool_flag(value: object, key: str) -> bool:
    """Parse a top-level context flag, which ``cdk --context key=value`` may set.

    Accepts JSON booleans and the strings ``"true"`` / ``"false"`` in any
    case, because ``--context`` passes every value as a string. Anything
    else, including ``0``, ``1``, ``None`` and ``"yes"``, is an error naming
    ``key``. Python truthiness is never used: ``bool("false")`` is ``True``.
    A flag inside a cdk.json block goes through :func:`parse_block_flag`.

    Raises:
        ConfigValidationError: If ``value`` is not a boolean or one of the
            two strings.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        folded = value.lower()
        if folded == "true":
            return True
        if folded == "false":
            return False
    raise ConfigValidationError(f"{key} must be true or false, got {value!r}")


def parse_block_flag(
    value: object,
    key: str,
    warn: Callable[[str], None],
    *,
    off_deletes: str | None = None,
    read_by_truthiness: bool = True,
) -> bool:
    """Parse a boolean flag inside a cdk.json block, such as ``valkey.enabled``.

    A nested key can only come from cdk.json (``--context`` sets top-level
    keys), and earlier releases read these flags by Python truthiness, so
    every non-empty string, ``"false"`` included, meant on. A string is
    therefore never reinterpreted on upgrade:

    - JSON booleans are returned as they are.
    - ``"true"`` in any case still reads as true, with a deprecation warning
      passed to ``warn``.
    - Every other string is an error that says how earlier releases read it
      and what to write instead. Reading ``"false"`` as false would switch
      off a feature that has been running, and for a storage feature
      (``off_deletes`` names what switching it off deletes) that deletes its
      data.
    - Anything else (numbers, ``null``, lists) is an error naming ``key``.

    ``read_by_truthiness=False`` is for a flag earlier releases did not read
    by truthiness (a string failed the synth instead), so its error makes no
    claim about how the deployment has been running.

    Raises:
        ConfigValidationError: If ``value`` is not a boolean or ``"true"``.
    """
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ConfigValidationError(f"{key} must be true or false (a JSON boolean), got {value!r}")
    if value.lower() == "true":
        warn(
            f"{key} is the string {value!r}, read as true; write the JSON boolean true "
            "instead (string flags in cdk.json blocks are deprecated)."
        )
        return True
    problem = f"{key} must be true or false (a JSON boolean), not the string {value!r}."
    if not read_by_truthiness:
        raise ConfigValidationError(problem)
    if value:
        consequence = f", which deletes {off_deletes}" if off_deletes else ""
        raise ConfigValidationError(
            f"{problem} Earlier releases read every non-empty string here as true, so this "
            f"setting has been on. Write true to keep it on, or false to turn it off{consequence}."
        )
    raise ConfigValidationError(
        f"{problem} Earlier releases read an empty string here as false, so this setting "
        "has been off. Write false to keep it off, or true to turn it on."
    )


def _is_ipv4_cidr(value: object) -> bool:
    """Whether ``value`` is an IPv4 CIDR string with an explicit prefix (``a.b.c.d/n``)."""
    if not isinstance(value, str) or "/" not in value:
        return False
    try:
        ipaddress.IPv4Network(value, strict=False)
    except ValueError:
        return False
    return True


def _check_block_keys(path: str, block: Mapping[str, Any], allowed: Iterable[str]) -> None:
    """Reject keys of ``block`` (at cdk.json ``path``) outside ``allowed``.

    ``_comment*`` keys are documentation and always allowed. The message
    names every unknown key and every allowed key with its full path.
    """
    known = set(allowed)
    unknown = sorted(str(key) for key in block if not is_comment_key(key) and key not in known)
    if unknown:
        raise ConfigValidationError(
            f"{path} contains unknown key(s): "
            + ", ".join(f"{path}.{key}" for key in unknown)
            + "; allowed keys: "
            + ", ".join(f"{path}.{key}" for key in sorted(known))
        )


def _object_or_empty(path: str, value: object) -> dict[str, Any]:
    """Return ``value`` as a block, ``{}`` when absent; anything but an object is an error."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigValidationError(
            f"{path} must be an object, got {type(value).__name__}: {value!r}"
        )
    return value


def _require_int(path: str, value: object, minimum: int, maximum: int | None = None) -> int:
    """Return ``value`` if it is an integer (never a boolean) inside the inclusive range."""
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = (
            f"between {minimum} and {maximum}" if maximum is not None else f"of at least {minimum}"
        )
        raise ConfigValidationError(f"{path} must be an integer {bounds}, got {value!r}")
    return value


def _require_choice(path: str, value: object, choices: Iterable[str]) -> str:
    """Return ``value`` if it is one of the (case-sensitive) string ``choices``."""
    allowed = sorted(choices)
    if not isinstance(value, str) or value not in allowed:
        raise ConfigValidationError(f"{path} must be one of {', '.join(allowed)}, got {value!r}")
    return value


#: ``helm`` charts whose ``enabled`` toggle the regional stack ignores: the
#: mandatory platform components, which always install. Lockstep with
#: ``gco.stacks.regional_stack._MANDATORY_CHART_KEYS``.
_MANDATORY_HELM_CHARTS = frozenset({"aws_load_balancer_controller", "keda"})

#: What switching a ``helm`` chart off deletes besides its Helm release, for
#: the charts whose uninstall first deletes custom resources cluster-wide
#: (``CHART_CUSTOM_RESOURCE_API_GROUPS`` in lambda/helm-installer/handler.py).
#: ``helm.argocd`` and ``helm.slurm`` never read a string as true, so their
#: messages need none.
_HELM_CHART_OFF_DELETES: dict[str, str] = {
    "kueue": "every Kueue queue and workload object",
    "crossplane": "every Crossplane composite resource with the resources it composed",
}

#: Enabled flags inside cdk.json blocks that the regional stack reads from the
#: raw context instead of through a ConfigLoader getter, each with what
#: switching it off deletes (``None``: nothing that holds data), for
#: :func:`parse_block_flag`'s message. ConfigLoader validates each one and the
#: stack reads each one with :func:`read_block_flag`, so both read a value the
#: same way. Earlier releases read all of them by truthiness.
STACK_BLOCK_FLAGS: dict[str, str | None] = {
    "queue_processor.enabled": None,
    "drift_detection.enabled": "the drift-detection SNS topic and its subscriptions",
    "mcp_server.enabled": None,
    "volcano_image_mirror.enabled": None,
    **{
        f"helm.{chart}.enabled": (
            f"the {chart} Helm release and {_HELM_CHART_OFF_DELETES[chart]}"
            if chart in _HELM_CHART_OFF_DELETES
            else f"the {chart} Helm release"
        )
        for chart in sorted(HELM_CHART_CONFIG_KEYS - _MANDATORY_HELM_CHARTS)
    },
}

#: Flags of :data:`STACK_BLOCK_FLAGS` whose blocks have their own validators
#: (gco/argocd_config.py, gco/slurm_config.py), which have always required a
#: JSON boolean: a string there failed the synth instead of reading as true.
_OWN_VALIDATOR_STACK_BLOCK_FLAGS = frozenset({"helm.argocd.enabled", "helm.slurm.enabled"})


def _reported_by_config_loader(_message: str) -> None:
    """The ``warn`` of the stack's reads: ConfigLoader has reported the same value."""


def read_block_flag(
    block: object,
    path: str,
    *,
    default: bool,
    warn: Callable[[str], None] = _reported_by_config_loader,
) -> bool:
    """Read the :data:`STACK_BLOCK_FLAGS` flag at cdk.json ``path`` from its raw block.

    ``block`` is the raw value at the parent path (``None`` when absent). An
    absent block or key reads as ``default``; a present value goes through
    :func:`parse_block_flag` with the arguments the loader validates it with.
    The regional stack keeps the default ``warn``, because ConfigLoader has
    already warned about a deprecated ``"true"`` string.

    Raises:
        ConfigValidationError: If ``block`` is not an object, or the flag is
            neither a JSON boolean nor the string ``"true"``.
    """
    parent, _, key = path.rpartition(".")
    fields = _object_or_empty(parent, block)
    if key not in fields:
        return default
    return parse_block_flag(
        fields[key],
        path,
        warn,
        off_deletes=STACK_BLOCK_FLAGS[path],
        read_by_truthiness=path not in _OWN_VALIDATOR_STACK_BLOCK_FLAGS,
    )


class ConfigLoader:
    """
    Loads and validates configuration from CDK context (cdk.json)
    """

    # Keep the public class attribute for compatibility. Endpoint metadata
    # covers every CloudFormation Region known to the installed AWS SDK; this
    # is not a project-specific allowlist.
    VALID_REGIONS = known_cloudformation_regions()

    def __init__(self, app: App):
        self.app = app
        #: Non-fatal findings of the validation (deprecated keys, keys that
        #: take no effect), in the order they were found. Each is also logged
        #: at WARNING, which ``cdk synth`` prints.
        self.warnings: list[str] = []
        self._validate_configuration()

    def _warn(self, message: str) -> None:
        """Record and log one validation warning (each distinct message once)."""
        if message not in self.warnings:
            self.warnings.append(message)
            logger.warning("%s", message)

    def _context_at(self, path: str) -> object:
        """The raw value at a dotted cdk.json path (``None`` when any part is absent)."""
        head, *rest = path.split(".")
        value: object = self.app.node.try_get_context(head)
        for part in rest:
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def _validate_block_shapes(self) -> None:
        """Check the shape of every block in :data:`BLOCK_KEYS` before anything reads it.

        A block present in cdk.json must be an object holding only its known
        keys; the error names the full path. Earlier releases read a scalar
        such as ``"cluster_observability": false`` as an absent block, so its
        defaults (observability on) applied, and crashed on others, such as
        ``"cost_monitoring": {"reports": 30}``. ``tags`` must map tag names to
        strings. A deprecated key (:data:`DEPRECATED_BLOCK_KEYS`) and an
        unknown top-level key in GCO's style draw a warning.

        Raises:
            ConfigValidationError: If a block is not an object, holds an
                unknown key, or a tag value is not a string.
        """
        for path, allowed in BLOCK_KEYS.items():
            raw = self._context_at(path)
            if raw is not None:
                _check_block_keys(path, _object_or_empty(path, raw), allowed)
        for path, advice in DEPRECATED_BLOCK_KEYS.items():
            parent, _, key = path.rpartition(".")
            block = self._context_at(parent)
            if isinstance(block, dict) and key in block:
                self._warn(f"{path} is deprecated and ignored: {advice}")
        for name, value in _object_or_empty("tags", self.app.node.try_get_context("tags")).items():
            if not isinstance(value, str):
                raise ConfigValidationError(f"tags.{name} must be a string, got {value!r}")
        self._warn_unknown_context_keys()

    def _validate_stack_block_flags(self) -> None:
        """Validate the enabled flags the regional stack reads from the raw context.

        These are the :data:`STACK_BLOCK_FLAGS`: ``queue_processor``,
        ``drift_detection``, ``mcp_server``, ``volcano_image_mirror`` and the
        optional ``helm`` charts. Earlier releases read them by truthiness, so
        a string is never reinterpreted (:func:`parse_block_flag`): ``"true"``
        reads as true with a deprecation warning, and every other string fails
        with what to write instead. ``helm.argocd.enabled`` and
        ``helm.slurm.enabled`` are left to their blocks' own validators, which
        have always required a JSON boolean.

        Raises:
            ConfigValidationError: If one of the flags is neither a JSON
                boolean nor the string ``"true"``.
        """
        for path in STACK_BLOCK_FLAGS:
            if path not in _OWN_VALIDATOR_STACK_BLOCK_FLAGS:
                parent = path.rpartition(".")[0]
                read_block_flag(self._context_at(parent), path, default=False, warn=self._warn)

    def _warn_unknown_context_keys(self) -> None:
        """Warn about each top-level key in GCO's style that GCO does not read.

        Such a key is most likely a misspelt block (``cluster_observabilty``),
        whose settings would silently not apply. Only a warning: the CDK CLI
        and other tools may add context of their own.
        """
        get_all_context = getattr(self.app.node, "get_all_context", None)
        if not callable(get_all_context):
            return
        known = BLOCK_KEYS.keys() | OTHER_CONTEXT_KEYS | DEPRECATED_CONTEXT_KEYS.keys()
        for key in sorted(str(name) for name in get_all_context()):
            if _GCO_CONTEXT_KEY_STYLE.fullmatch(key) and key not in known:
                self._warn(
                    f"{key} is not a GCO cdk.json key and is ignored; check its spelling "
                    "against docs/CUSTOMIZATION.md."
                )

    def _warn_deprecated_blocks(self) -> None:
        """Warn about each deprecated top-level block present in the context.

        A deprecated block is ignored: nothing reads it, and its values never
        take effect. See :data:`DEPRECATED_CONTEXT_KEYS`.
        """
        for key, advice in DEPRECATED_CONTEXT_KEYS.items():
            if self.app.node.try_get_context(key) is not None:
                self._warn(f"{key} is deprecated and ignored: {advice}")

    def _validate_configuration(self) -> None:
        """Validate the entire configuration.

        Validation always runs. ``project_name`` is optional and defaults to
        ``gco`` (see :meth:`get_project_name`); an explicit value must match
        :attr:`PROJECT_NAME_PATTERN`. An absent ``project_name`` used to skip
        every check below, so a cdk.json without it synthesized whatever the
        other blocks said.
        """
        # Block shapes first, so no getter below reads a scalar as a block
        self._validate_block_shapes()
        # The enabled flags the regional stack reads from the raw context
        self._validate_stack_block_flags()

        # Validate required fields exist
        required_fields = [
            "kubernetes_version",
            "resource_thresholds",
        ]
        for field in required_fields:
            if not self.app.node.try_get_context(field):
                raise ConfigValidationError(f"Required configuration field '{field}' is missing")

        # Validate project_name format before anything consumes it (#139).
        self._validate_project_name()

        # kubernetes_version is a "<major>.<minor>" string: a JSON number
        # such as 1.30 used to reach EKS as "1.3".
        kubernetes_version = self.app.node.try_get_context("kubernetes_version")
        if not isinstance(kubernetes_version, str) or not re.fullmatch(
            r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)", kubernetes_version
        ):
            raise ConfigValidationError(
                'kubernetes_version must be a "<major>.<minor>" string such as "1.37", got '
                f"{kubernetes_version!r}; write it in quotes, because a JSON number such as "
                "1.30 is read as 1.3"
            )

        # Check for deployment_regions
        deployment_regions = self.app.node.try_get_context("deployment_regions")
        if not isinstance(deployment_regions, dict) or not deployment_regions:
            raise ConfigValidationError(
                "Required configuration field 'deployment_regions' must be a non-empty object"
            )

        # Validate regions, then the Region names the per-Region overlays use
        self._validate_regions()
        self._validate_region_overlays()

        # Validate resource thresholds
        self._validate_resource_thresholds()

        # Validate Global Accelerator config
        self._validate_global_accelerator_config()

        # Validate deployment-local backend TLS rotation policy
        self._validate_backend_tls_config()

        # Validate inference proxy TLS sidecar autoscaling settings
        self._validate_inference_proxy_config()

        # Deprecated blocks (alb_config, ...) are ignored with a warning
        self._warn_deprecated_blocks()

        # Validate the platform ALB target-group health check
        self._validate_gateway_health_check_config()

        # Validate the optional data services: FSx for Lustre (and its
        # per-region overlays), the Valkey cache and Aurora pgvector
        self._validate_fsx_lustre_config()
        self.get_valkey_config()
        self.get_aurora_pgvector_config()

        # Validate manifest processor config
        self._validate_manifest_processor_config()

        # Validate API Gateway config
        self._validate_api_gateway_config()

        # Validate EKS cluster config
        self._validate_eks_cluster_config()

        # Validate the opt-in EKS Capabilities block (ACK / kro)
        self._validate_eks_capabilities_config()
        # Validate the self-managed Argo CD block (helm.argocd)
        self._validate_argocd_config()
        # Validate the gco-jobs quota (resource_quota), then the Slurm
        # cluster block (helm.slurm), whose partitions must fit it
        self.get_resource_quota()
        self._validate_slurm_config()
        # Validate the AWS PCS block (pcs / pcs_regions)
        self._validate_pcs_config()
        self._validate_hyperpod_config()

        # Validate analytics environment config (optional block)
        self._validate_analytics_environment_config()

        # Validate cluster observability config (optional block)
        self._validate_cluster_observability_config()

        # Validate cost monitoring config (optional block)
        self._validate_cost_monitoring_config()

        # Validate the OpenTelemetry -> X-Ray tracing block and its run-scoped
        # overrides (optional; on by default)
        self._validate_tracing_config()

        # Validate historical capacity surface config (optional block)
        self._validate_capacity_history_config()

        # Validate mission-memory configuration (recall across mission sessions)
        self._validate_mission_memory_config()
        # Validate the vector-store configuration (global workload RAG corpus)
        self._validate_vector_store_config()

    #: Allowed ``project_name`` format (#139). ``project_name`` is the
    #: deployment's unique prefix and flows into S3 bucket names, the Cognito
    #: hosted-UI domain prefix, ECR repository namespaces (``<project>/...``),
    #: SSM parameter paths, IAM role names, CloudFormation stack and export
    #: names, and the Global Accelerator name. Together they require a leading
    #: lowercase letter, then lowercase letters and digits in runs joined by
    #: single hyphens (ECR rejects a repository path segment that ends in a
    #: hyphen or holds two in a row, and Cognito a prefix that ends in one),
    #: 2-31 characters in all.
    PROJECT_NAME_PATTERN = re.compile(r"^(?=.{2,31}$)[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

    #: Global Accelerator names: at most 64 alphanumerics, periods and
    #: hyphens, neither first nor last a period or hyphen (CreateAccelerator).
    GLOBAL_ACCELERATOR_NAME_PATTERN = re.compile(
        r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,62}[A-Za-z0-9])?$"
    )

    def _validate_project_name(self) -> None:
        """Validate ``project_name`` format so misconfigurations fail at synth.

        ``project_name`` is documented as the deployment's unique identifier
        and is used as the prefix for nearly every physical resource name. If
        it contains characters that are illegal in S3 bucket names or Cognito
        domain prefixes (uppercase, underscores, dots, leading digit, etc.),
        ``cdk synth`` still succeeds but the deploy fails late with an opaque
        AWS naming error. Catching it here turns that into an actionable
        message up front.
        """
        project_name = self.app.node.try_get_context("project_name")
        if project_name is None:
            # Absent (or JSON null, which CDK normalizes to absent): the
            # documented default ``gco`` applies.
            return
        if not isinstance(project_name, str) or not self.PROJECT_NAME_PATTERN.fullmatch(
            project_name
        ):
            raise ConfigValidationError(
                f"Invalid project_name {project_name!r}. It must match "
                f"{self.PROJECT_NAME_PATTERN.pattern} (2-31 characters: a lowercase letter, "
                "then lowercase letters and digits, with single hyphens between them and "
                "none at the end). "
                "project_name is the deployment prefix for S3 buckets, the Cognito "
                "domain, SSM paths, and CloudFormation exports, so it must be a valid "
                "lowercase DNS-style label."
            )

    def _validate_regions(self) -> None:
        """Validate region configuration against the shared app/CLI contract."""
        deployment_regions = self.get_deployment_regions()
        try:
            for field in ("global", "api_gateway", "monitoring"):
                region = deployment_regions[field]
                if not isinstance(region, str) or region not in self.VALID_REGIONS:
                    raise ValueError(
                        f"deployment_regions.{field} is not a valid region: {region!r}; "
                        "expected an AWS region with a "
                        "CloudFormation endpoint known to the installed SDK"
                    )
            regional = validated_regional_deployment_regions(
                deployment_regions["regional"],
                known_regions=self.VALID_REGIONS,
            )
            validated_deployment_partition(
                (
                    deployment_regions["global"],
                    deployment_regions["api_gateway"],
                    deployment_regions["monitoring"],
                    *regional,
                )
            )
        except (RuntimeError, ValueError) as exc:
            raise ConfigValidationError(str(exc)) from exc

    #: Top-level blocks keyed by Region whose entries patch a feature block
    #: for that Region only.
    REGION_OVERLAY_KEYS: tuple[str, ...] = (
        FSX_LUSTRE_REGIONS_CONTEXT_KEY,
        PCS_REGIONS_KEY,
        HYPERPOD_REGIONS_KEY,
    )

    def _validate_region_overlays(self) -> None:
        """Check the Region names of ``fsx_lustre_regions``, ``pcs_regions`` and ``hyperpod_regions``.

        An entry for a name that is not an AWS Region (a typo such as
        ``us-east1``) fails: it can never take effect. An entry for a real
        Region outside ``deployment_regions.regional`` only warns: no stack
        reads it today, which is expected while a Region is being staged
        before it is added, or in a control-plane-only deployment. Each
        feature's validator checks the entries' contents.
        """
        deployed = set(self.get_regions())
        for key in self.REGION_OVERLAY_KEYS:
            overlays = self.app.node.try_get_context(key)
            if not isinstance(overlays, dict):
                continue  # absent, or malformed and reported by the feature's validator
            for region in sorted(str(name) for name in overlays if not is_comment_key(name)):
                if region not in self.VALID_REGIONS:
                    raise ConfigValidationError(
                        f"{key}.{region} is not an AWS Region known to the installed SDK; "
                        f"{key} is keyed by Region name, such as us-east-1"
                    )
                if region not in deployed:
                    self._warn(
                        f"{key}.{region} has no effect: {region} is not in "
                        "deployment_regions.regional, so no regional stack reads it (expected "
                        "while staging a Region before adding it)."
                    )

    def _validate_resource_thresholds(self) -> None:
        """Validate resource threshold configuration"""
        thresholds_config = self.app.node.try_get_context("resource_thresholds")

        required_thresholds = ["cpu_threshold", "memory_threshold", "gpu_threshold"]
        for threshold in required_thresholds:
            if threshold not in thresholds_config:
                raise ConfigValidationError(f"resource_thresholds.{threshold} is required")

            value = thresholds_config[threshold]
            # type() rather than isinstance(): a boolean is not a threshold.
            if type(value) is not int or (value != -1 and not 0 <= value <= 100):
                raise ConfigValidationError(
                    f"resource_thresholds.{threshold} must be an integer between 0 and 100 "
                    f"(or -1 to disable), got {value!r}"
                )

        # Validate optional thresholds if present
        for opt_threshold in [
            "pending_pods_threshold",
            "pending_requested_cpu_vcpus",
            "pending_requested_memory_gb",
            "pending_requested_gpus",
        ]:
            if opt_threshold in thresholds_config:
                value = thresholds_config[opt_threshold]
                if type(value) is not int or (value != -1 and value < 0):
                    raise ConfigValidationError(
                        f"resource_thresholds.{opt_threshold} must be a non-negative integer "
                        f"(or -1 to disable), got {value!r}"
                    )

    #: Health-check probe intervals Global Accelerator accepts. The API
    #: constrains HealthCheckIntervalSeconds to exactly 10 or 30 seconds, so
    #: any other value must fail at synth instead of at deploy.
    GLOBAL_ACCELERATOR_HEALTH_CHECK_INTERVALS = frozenset({10, 30})

    #: Traffic-dial controller modes. ``monitor`` computes and publishes
    #: per-region dial decisions without mutating Global Accelerator;
    #: ``enforce`` additionally applies them via UpdateEndpointGroup.
    TRAFFIC_DIAL_MODES = frozenset({"monitor", "enforce"})

    def _validate_global_accelerator_config(self) -> None:
        """Validate the ``global_accelerator`` block in cdk.json.

        The block is optional; absence means the shipped defaults apply.
        Validation runs against the *merged* configuration so a partial block
        is checked together with every default it kept:

        - ``health_check_interval``: 10 or 30 — the only probe intervals the
          Global Accelerator API accepts (UpdateEndpointGroup rejects others).
        - ``health_check_threshold``: integer 1-10 (the API ThresholdCount
          range).
        - ``health_check_path``: must start with ``/``.
        - ``client_affinity``: NONE or SOURCE_IP, case-insensitive.
        - ``traffic_dial``: see :meth:`_validate_traffic_dial_config`.

        The legacy ``health_check_grace_period`` and ``health_check_timeout``
        keys are tolerated and ignored: Global Accelerator endpoint groups
        have no such settings (the keys were validated but never consumed),
        so their presence in an existing cdk.json must not fail synthesis.

        ``name`` is intentionally optional: when omitted it defaults to
        ``<project_name>-accelerator`` so a second deployment gets a
        project-scoped name from the single ``project_name`` knob (#139).
        """
        ga_config = self.get_global_accelerator_config()

        interval = ga_config["health_check_interval"]
        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval not in self.GLOBAL_ACCELERATOR_HEALTH_CHECK_INTERVALS
        ):
            raise ConfigValidationError(
                "global_accelerator.health_check_interval must be one of "
                f"{sorted(self.GLOBAL_ACCELERATOR_HEALTH_CHECK_INTERVALS)} — the only probe "
                f"intervals the Global Accelerator API accepts — got {interval!r}"
            )

        threshold = ga_config["health_check_threshold"]
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or not 1 <= threshold <= 10
        ):
            raise ConfigValidationError(
                "global_accelerator.health_check_threshold must be an integer between "
                f"1 and 10, got {threshold!r}"
            )

        name = ga_config["name"]
        if not isinstance(name, str) or not self.GLOBAL_ACCELERATOR_NAME_PATTERN.fullmatch(name):
            raise ConfigValidationError(
                "global_accelerator.name must be 1-64 letters, digits, periods or hyphens, "
                f"not starting or ending with a period or hyphen, got {name!r}"
            )

        path = ga_config["health_check_path"]
        if not isinstance(path, str) or not path.startswith("/"):
            raise ConfigValidationError(
                f"global_accelerator.health_check_path must start with '/', got {path!r}"
            )

        allowed_affinity = {"NONE", "SOURCE_IP"}
        affinity = ga_config["client_affinity"]
        if not isinstance(affinity, str) or affinity.upper() not in allowed_affinity:
            raise ConfigValidationError(
                f"global_accelerator.client_affinity must be one of {sorted(allowed_affinity)}, "
                f"got {affinity!r}"
            )

        self._validate_traffic_dial_config(ga_config["traffic_dial"])

    def _validate_traffic_dial_config(self, dial_config: Any) -> None:
        """Validate the merged ``global_accelerator.traffic_dial`` sub-block.

        Ranges mirror the Global Accelerator API and the controller contract:

        - ``enabled``: bool (default False — the controller is opt-in).
        - ``mode``: ``monitor`` or ``enforce``, case-insensitive.
        - ``interval_minutes`` / ``lookback_minutes``: 1-1440.
        - ``min_dial_percentage``: 0-100 (TrafficDialPercentage range); the
          floor a degraded region can be dialed down to.
        - ``max_step_percentage``: 1-100; the largest change one run applies.
        - ``full_health_percentage``: 1-100; the healthy fraction (percent)
          at or above which a region is restored toward 100.
        """
        enabled = dial_config["enabled"]
        if not isinstance(enabled, bool):
            raise ConfigValidationError(
                "global_accelerator.traffic_dial.enabled must be a bool, got "
                f"{type(enabled).__name__}: {enabled!r}"
            )

        mode = dial_config["mode"]
        if not isinstance(mode, str) or mode.lower() not in self.TRAFFIC_DIAL_MODES:
            raise ConfigValidationError(
                "global_accelerator.traffic_dial.mode must be one of "
                f"{sorted(self.TRAFFIC_DIAL_MODES)}, got {mode!r}"
            )

        int_ranges = (
            ("interval_minutes", 1, 1_440),
            ("lookback_minutes", 1, 1_440),
            ("min_dial_percentage", 0, 100),
            ("max_step_percentage", 1, 100),
            ("full_health_percentage", 1, 100),
        )
        for field, minimum, maximum in int_ranges:
            value = dial_config[field]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ConfigValidationError(
                    f"global_accelerator.traffic_dial.{field} must be an integer between "
                    f"{minimum} and {maximum}, got {value!r}"
                )

    def _validate_backend_tls_config(self) -> None:
        """Validate private-root and leaf-certificate lifecycle settings."""
        config = self.get_backend_tls_config()
        ranges = {
            "root_generation": (1, 1_000_000),
            "root_validity_days": (365, 36_500),
            "root_rotate_before_days": (30, 3_650),
            "root_activation_delay_hours": (1, 168),
            "root_overlap_days": (2, 365),
            "leaf_validity_days": (2, 397),
            "leaf_rotate_before_days": (1, 90),
            "rotation_schedule_hours": (1, 24),
            "trust_cache_ttl_seconds": (1, 3_600),
            "trust_cache_max_stale_seconds": (1, 86_400),
        }
        for field, (minimum, maximum) in ranges.items():
            value = config.get(field)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigValidationError(
                    f"backend_tls.{field} must be an integer between "
                    f"{minimum} and {maximum}, got {value!r}"
                )

        if config["root_rotate_before_days"] >= config["root_validity_days"]:
            raise ConfigValidationError(
                "backend_tls.root_rotate_before_days must be less than root_validity_days"
            )
        if config["leaf_rotate_before_days"] >= config["leaf_validity_days"]:
            raise ConfigValidationError(
                "backend_tls.leaf_rotate_before_days must be less than leaf_validity_days"
            )
        if config["root_validity_days"] <= config["leaf_validity_days"]:
            raise ConfigValidationError(
                "backend_tls.root_validity_days must exceed leaf_validity_days"
            )
        if config["root_overlap_days"] <= config["leaf_validity_days"]:
            raise ConfigValidationError(
                "backend_tls.root_overlap_days must exceed leaf_validity_days so old leaves "
                "remain trusted throughout root rollover"
            )
        if config["trust_cache_max_stale_seconds"] < config["trust_cache_ttl_seconds"]:
            raise ConfigValidationError(
                "backend_tls.trust_cache_max_stale_seconds must be at least trust_cache_ttl_seconds"
            )
        if config["root_activation_delay_hours"] * 3_600 <= config["trust_cache_max_stale_seconds"]:
            raise ConfigValidationError(
                "backend_tls.root_activation_delay_hours must exceed the maximum stale trust "
                "cache window so every proxy can observe a pending root before leaf rollover"
            )

    def _validate_inference_proxy_config(self) -> None:
        """Validate the inference proxy TLS CPU request, HPA target and replica bounds."""
        config = self.get_inference_proxy_config()
        ranges = {
            "tls_proxy_cpu_request_millicores": (1, 250),
            "tls_proxy_cpu_target_utilization_percentage": (1, 100),
            "min_replicas": (1, 50),
            "max_replicas": (1, 100),
        }
        for field, (minimum, maximum) in ranges.items():
            value = config[field]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigValidationError(
                    f"inference_proxy.{field} must be an integer between "
                    f"{minimum} and {maximum}, got {value!r}"
                )
        if config["max_replicas"] < config["min_replicas"]:
            raise ConfigValidationError(
                "inference_proxy.max_replicas must be at least inference_proxy.min_replicas, "
                f"got {config['max_replicas']} < {config['min_replicas']}"
            )

    def _validate_gateway_health_check_config(self) -> None:
        """Validate the optional ``gateway_health_check`` block (see the getter)."""
        self.get_gateway_health_check_config()

    def _validate_manifest_processor_config(self) -> None:
        """Validate manifest processor configuration.

        The manifest processor section in cdk.json holds service-specific
        settings only. The shared validation policy (allowed_namespaces,
        resource_quotas, trusted_registries, trusted_dockerhub_orgs,
        manifest_security_policy, allowed_kinds) lives under
        ``job_validation_policy`` because the queue_processor reads the
        same values.
        """
        mp_config = self.app.node.try_get_context("manifest_processor")
        if not mp_config:
            raise ConfigValidationError("manifest_processor configuration is required")

        for field in ("replicas", "resource_limits"):
            if field not in mp_config:
                raise ConfigValidationError(f"manifest_processor.{field} is required")

        # Validate replicas (type(), not isinstance(): a boolean is not a count)
        if type(mp_config["replicas"]) is not int or mp_config["replicas"] <= 0:
            raise ConfigValidationError(
                "manifest_processor.replicas must be a positive integer, got "
                f"{mp_config['replicas']!r}"
            )

        try:
            validated_request_body_limit(
                mp_config.get("max_request_body_bytes", DEFAULT_MAX_REQUEST_BODY_BYTES)
            )
        except ValueError as exc:
            raise ConfigValidationError(f"manifest_processor.{exc}") from exc

        validation_enabled = mp_config.get("validation_enabled", True)
        if type(validation_enabled) is not bool:
            raise ConfigValidationError("manifest_processor.validation_enabled must be a boolean")

        # Validate the shared policy section separately so a misconfigured
        # policy block surfaces a clear error pointing at the right key.
        policy = self.app.node.try_get_context("job_validation_policy")
        if policy is None:
            raise ConfigValidationError(
                "job_validation_policy configuration is required (shared between "
                "manifest_processor and queue_processor)"
            )
        for policy_field in ("allowed_namespaces", "resource_quotas"):
            if policy_field not in policy:
                raise ConfigValidationError(f"job_validation_policy.{policy_field} is required")

        try:
            validate_manifest_security_policy(policy.get("manifest_security_policy", {}))
        except ValueError as exc:
            raise ConfigValidationError(f"job_validation_policy.{exc}") from exc

        require_toleration = policy.get("require_accelerator_toleration", True)
        if type(require_toleration) is not bool:
            raise ConfigValidationError(
                "job_validation_policy.require_accelerator_toleration must be a boolean"
            )

        # Validate resource limits. They render verbatim into the Deployment's
        # container limits, so they must be non-empty Kubernetes quantities no
        # smaller than the requests fixed in 31-manifest-processor.yaml —
        # Kubernetes rejects limit < request, and catching that here fails the
        # synth instead of the applier halfway through a deploy.
        resource_limits = mp_config["resource_limits"]
        if "cpu" not in resource_limits or "memory" not in resource_limits:
            raise ConfigValidationError(
                "manifest_processor.resource_limits must contain 'cpu' and 'memory'"
            )
        for resource, request in _MANIFEST_PROCESSOR_CONTAINER_REQUESTS.items():
            quantity = resource_limits[resource]
            try:
                if not isinstance(quantity, str):
                    raise ValueError("must be a string")
                parsed = parse_k8s_quantity(quantity)
            except ValueError as exc:
                raise ConfigValidationError(
                    f"manifest_processor.resource_limits.{resource} must be a Kubernetes "
                    f"quantity string such as '1000m' or '2Gi', got {quantity!r} ({exc})"
                ) from exc
            if parsed < parse_k8s_quantity(request):
                raise ConfigValidationError(
                    f"manifest_processor.resource_limits.{resource} must be at least the "
                    f"container request of {request!r} "
                    f"(31-manifest-processor.yaml), got {quantity!r}"
                )

        # Validate the optional CPU autoscaler. Off by default: the API tier is
        # I/O-bound, so CPU is a weak saturation signal; operators who have
        # measured otherwise turn it on here and the HPA takes over the count.
        autoscaling = mp_config.get("autoscaling")
        if autoscaling is None:
            autoscaling = {}
        if not isinstance(autoscaling, dict):
            raise ConfigValidationError("manifest_processor.autoscaling must be an object")
        _check_block_keys(
            "manifest_processor.autoscaling", autoscaling, _MANIFEST_PROCESSOR_AUTOSCALING_DEFAULTS
        )
        merged_autoscaling = {**_MANIFEST_PROCESSOR_AUTOSCALING_DEFAULTS, **autoscaling}
        if type(merged_autoscaling["enabled"]) is not bool:
            raise ConfigValidationError("manifest_processor.autoscaling.enabled must be a boolean")
        for key, minimum, maximum in (
            ("max_replicas", 1, 100),
            ("cpu_target_utilization_percentage", 1, 100),
        ):
            value = merged_autoscaling[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigValidationError(
                    f"manifest_processor.autoscaling.{key} must be an integer between "
                    f"{minimum} and {maximum}, got {value!r}"
                )
        if merged_autoscaling["max_replicas"] < mp_config["replicas"]:
            raise ConfigValidationError(
                "manifest_processor.autoscaling.max_replicas must be at least "
                f"manifest_processor.replicas, got {merged_autoscaling['max_replicas']} "
                f"< {mp_config['replicas']}"
            )

        # Validate allowed namespaces (lives under job_validation_policy).
        if not isinstance(policy["allowed_namespaces"], list):
            raise ConfigValidationError("job_validation_policy.allowed_namespaces must be a list")

    def _validate_api_gateway_config(self) -> None:
        """Validate API Gateway configuration"""
        api_gw_config = self.app.node.try_get_context("api_gateway")
        if not api_gw_config:
            raise ConfigValidationError("api_gateway configuration is required")

        required_fields = [
            "throttle_rate_limit",
            "throttle_burst_limit",
            "log_level",
            "metrics_enabled",
            "tracing_enabled",
        ]
        for field in required_fields:
            if field not in api_gw_config:
                raise ConfigValidationError(f"api_gateway.{field} is required")

        # Validate throttle limits (type(), not isinstance(): a boolean is
        # not a request rate)
        throttle_rate = api_gw_config["throttle_rate_limit"]
        throttle_burst = api_gw_config["throttle_burst_limit"]

        if type(throttle_rate) is not int or throttle_rate <= 0:
            raise ConfigValidationError(
                f"api_gateway.throttle_rate_limit must be a positive integer, got {throttle_rate!r}"
            )

        if type(throttle_burst) is not int or throttle_burst <= 0:
            raise ConfigValidationError(
                "api_gateway.throttle_burst_limit must be a positive integer, got "
                f"{throttle_burst!r}"
            )

        if throttle_burst < throttle_rate:
            raise ConfigValidationError(
                "api_gateway.throttle_burst_limit must be greater than or equal to "
                "api_gateway.throttle_rate_limit"
            )

        # Validate log level
        valid_log_levels = ["OFF", "ERROR", "INFO"]
        log_level = api_gw_config["log_level"]
        if log_level not in valid_log_levels:
            raise ConfigValidationError(
                f"api_gateway.log_level must be one of {valid_log_levels}, got {log_level!r}"
            )

        # Validate boolean flags
        for flag in ("metrics_enabled", "tracing_enabled", "regional_api_enabled"):
            if flag in api_gw_config and not isinstance(api_gw_config[flag], bool):
                raise ConfigValidationError(f"api_gateway.{flag} must be a boolean")

    def _validate_eks_cluster_config(self) -> None:
        """Validate EKS cluster configuration"""
        eks_config = self.app.node.try_get_context("eks_cluster") or {}

        # Validate endpoint_access if present
        if "endpoint_access" in eks_config:
            valid_access_modes = ["PRIVATE", "PUBLIC_AND_PRIVATE"]
            if eks_config["endpoint_access"] not in valid_access_modes:
                raise ConfigValidationError(
                    f"eks_cluster.endpoint_access must be one of {valid_access_modes}, "
                    f"got {eks_config['endpoint_access']}"
                )
        # The NetworkPolicy enforcement switch renders straight into a
        # kube-system ConfigMap value, so only a literal JSON boolean is
        # accepted — a string "false" would be applied verbatim and enable
        # nothing while reading as disabled.
        if (
            "network_policy_enforcement" in eks_config
            and type(eks_config["network_policy_enforcement"]) is not bool
        ):
            raise ConfigValidationError(
                "eks_cluster.network_policy_enforcement must be a boolean, got "
                f"{eks_config['network_policy_enforcement']!r}"
            )
        if "public_access_cidrs" in eks_config:
            self._validate_public_access_cidrs(eks_config["public_access_cidrs"])
        self._validate_vpc_endpoints_config()

    @staticmethod
    def _validate_public_access_cidrs(cidrs: object) -> None:
        """``eks_cluster.public_access_cidrs`` must be a list of IPv4 CIDR strings.

        The regional stack passes each entry to the EKS public-endpoint
        allowlist. A single string used to be iterated character by
        character, so ``"203.0.113.7/32"`` became the "CIDRs" ``"2"``,
        ``"0"``, ``"3"``, ... and the deploy failed (or allowed the wrong
        sources); the CLI (``gco stacks eks endpoint set --cidr``) has always
        written a list.
        """
        entries = cidrs if isinstance(cidrs, list) else None
        invalid = (
            [cidrs] if entries is None else [cidr for cidr in entries if not _is_ipv4_cidr(cidr)]
        )
        if invalid:
            raise ConfigValidationError(
                "eks_cluster.public_access_cidrs must be a list of IPv4 CIDR strings such "
                f'as ["203.0.113.0/24"], got {cidrs!r} (invalid: '
                f"{', '.join(repr(cidr) for cidr in invalid)})"
            )

    def _validate_vpc_endpoints_config(self) -> None:
        """Validate the optional ``vpc_endpoints`` block.

        ``gateway`` lists the free route-table endpoints (``s3``,
        ``dynamodb``); ``interface`` lists PrivateLink endpoints by the
        service key in :data:`VPC_INTERFACE_ENDPOINT_SERVICES`. Interface
        endpoints bill per AZ-hour, so the list is explicit and unknown names
        fail at synth instead of silently creating nothing.
        """
        raw = self.app.node.try_get_context("vpc_endpoints")
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ConfigValidationError("vpc_endpoints must be an object")
        _check_block_keys("vpc_endpoints", raw, _VPC_ENDPOINTS_DEFAULTS)
        for key, allowed in (
            ("gateway", VPC_GATEWAY_ENDPOINT_SERVICES),
            ("interface", VPC_INTERFACE_ENDPOINT_SERVICES),
        ):
            if key not in raw:
                continue
            value = raw[key]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ConfigValidationError(f"vpc_endpoints.{key} must be a list of strings")
            unsupported = sorted(set(value) - set(allowed))
            if unsupported:
                raise ConfigValidationError(
                    f"vpc_endpoints.{key} contains unsupported service(s): "
                    + ", ".join(unsupported)
                    + "; supported: "
                    + ", ".join(sorted(allowed))
                )
            if len(set(value)) != len(value):
                raise ConfigValidationError(f"vpc_endpoints.{key} lists a service twice")

    def _validate_eks_capabilities_config(self) -> None:
        """Validate the optional ``eks_capabilities`` block.

        Every capability type is off by default, so an absent block is valid.
        When present, the shape rules live in
        :mod:`gco.eks_capabilities_config`; per-type ``regions`` subsets must
        name regional deployment regions and every ACK role or policy entry
        must be an ARN of the right shape. The run-scoped
        ``eks_capabilities_overrides`` context (a JSON object) is merged in
        first, so an override that would produce an invalid block fails the
        same way an invalid cdk.json does.
        """
        raw = self._raw_eks_capabilities_config()
        if raw is None:
            return
        try:
            validate_eks_capabilities_config(raw, self.get_regions())
        except EksCapabilitiesConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def _raw_eks_capabilities_config(self) -> object:
        """The cdk.json ``eks_capabilities`` block with run-scoped overrides merged in."""
        raw = self.app.node.try_get_context(EKS_CAPABILITIES_CONTEXT_KEY)
        try:
            overrides = parse_eks_capabilities_overrides(
                self.app.node.try_get_context(EKS_CAPABILITIES_OVERRIDES_CONTEXT_KEY)
            )
        except EksCapabilitiesConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc
        return merge_eks_capabilities_overrides(raw, overrides)

    def _raw_argocd_config(self) -> object:
        """The cdk.json ``helm.argocd`` block, or ``None`` when absent."""
        helm = self.app.node.try_get_context("helm")
        return helm.get("argocd") if isinstance(helm, dict) else None

    def _validate_argocd_config(self) -> None:
        """Validate the optional self-managed Argo CD block (``helm.argocd``).

        Argo CD is off by default, so an absent block is valid. When present,
        the shape rules live in :mod:`gco.argocd_config`: unknown keys fail by
        name, the GitOps repository must be a Git URL admitted by
        ``source_repos``, and the sync policy is ``manual`` or ``automated``.
        """
        try:
            validate_argocd_config(self._raw_argocd_config())
        except ArgoCdConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_argocd_config(self) -> dict[str, Any]:
        """Return the ``helm.argocd`` block with defaults merged in.

        Keys: ``enabled``, ``source_repos``, ``gitops`` ``{repo_url, revision,
        path, sync_policy}`` (see :data:`gco.argocd_config.ARGOCD_DEFAULTS`).
        Whether the chart actually installs also honors the run-scoped
        ``helm_enabled_overrides`` context, which the regional stack resolves.
        """
        try:
            return validate_argocd_config(self._raw_argocd_config())
        except ArgoCdConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def _raw_slurm_config(self) -> object:
        """The cdk.json ``helm.slurm`` block, or ``None`` when absent."""
        helm = self.app.node.try_get_context("helm")
        return helm.get("slurm") if isinstance(helm, dict) else None

    def _slurm_fsx_available_everywhere(self) -> bool:
        """True when every regional cluster gets an FSx for Lustre filesystem.

        ``helm.slurm.storage.scratch: fsx`` asks for the FSx claim on every
        Slurm worker and login pod, and FSx is a per-region toggle, so the
        strict setting is only valid when each region has it. (``auto``
        resolves per region in the regional stack and never needs this.)

        Consulted only when the block actually says ``scratch: fsx``: the FSx
        lookup validates the run-scoped ``feature_enabled_overrides`` as a
        side effect, and a Slurm block that never asked for FSx must not turn
        an unrelated override typo into a construction-time Slurm error.
        """
        raw = self._raw_slurm_config()
        storage = raw.get("storage") if isinstance(raw, dict) else None
        if not isinstance(storage, dict) or storage.get("scratch") != "fsx":
            return False
        regions = self.get_regions()
        return bool(regions) and all(
            bool(self.get_fsx_lustre_config(region)["enabled"]) for region in regions
        )

    def _validate_slurm_config(self) -> None:
        """Validate the optional Slurm cluster block (``helm.slurm``).

        Slurm is off by default, so an absent block is valid. When present,
        the shape rules live in :mod:`gco.slurm_config`: unknown keys fail by
        name, every partition must target a known node pool with a coherent
        accelerator request and fit the ``gco-jobs`` resource quota, exactly
        one enabled partition is the default, autoscaled partitions need
        ``cluster_observability`` (their metrics come through Prometheus), and
        ``storage.scratch: fsx`` needs FSx in every region.
        """
        try:
            validate_slurm_config(
                self._raw_slurm_config(),
                resource_quota=self.get_resource_quota(),
                observability_enabled=self.get_cluster_observability_enabled(),
                fsx_enabled=self._slurm_fsx_available_everywhere(),
            )
        except SlurmConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_resource_quota(self) -> dict[str, str]:
        """The ``resource_quota`` block merged over its defaults, validated.

        The same merge the regional stack renders into the ``gco-jobs``
        ResourceQuota and LimitRange (see
        :func:`gco.resource_governance.validated_resource_quota`), so the
        Slurm partitions are checked against what deploys even when cdk.json
        leaves the block out.

        Raises:
            ConfigValidationError: If the block is not an object, names an
                unknown key, holds a value that is not a Kubernetes quantity,
                or a per-container ceiling exceeds its namespace ceiling.
        """
        raw = self.app.node.try_get_context("resource_quota")
        try:
            return validated_resource_quota({} if raw is None else raw)
        except ValueError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_slurm_config(self) -> dict[str, Any]:
        """Return the ``helm.slurm`` block with defaults merged in.

        Keys: ``enabled``, ``cluster_name``, ``controller``, ``restapi``,
        ``accounting``, ``metrics``, ``login``, ``autoscaling``,
        ``partitions``, ``storage``, ``slurm_conf``, ``slurmdbd_conf`` (see
        :data:`gco.slurm_config.SLURM_DEFAULTS`). Whether the charts actually
        install also honors the run-scoped ``helm_enabled_overrides`` context,
        which the regional stack resolves.
        """
        try:
            return validate_slurm_config(
                self._raw_slurm_config(),
                resource_quota=self.get_resource_quota(),
                observability_enabled=self.get_cluster_observability_enabled(),
                fsx_enabled=self._slurm_fsx_available_everywhere(),
            )
        except SlurmConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def _raw_pcs_config(self, region: str | None = None) -> object:
        """The cdk.json ``pcs`` block with the region's ``pcs_regions`` overlay applied.

        Both are optional; ``None`` means neither exists. A per-region overlay
        is a patch over the global block (``pcs_regions.us-east-1.enabled``
        turns PCS on in that region only), merged one level at a time so an
        overlay that names one nested key keeps the global block's siblings.
        An overlay for a region is only applied when the overlay is an object;
        anything else is reported by the validator with the region named. The
        run-scoped ``pcs_overrides`` context (a JSON object) is merged last.
        """
        global_raw = self.app.node.try_get_context(PCS_FEATURE_KEY)
        overrides = self.app.node.try_get_context(PCS_OVERRIDES_CONTEXT_KEY)
        try:
            if region is None:
                return apply_pcs_overrides(global_raw, overrides)
            overlays = self.app.node.try_get_context(PCS_REGIONS_KEY)
            if not isinstance(overlays, dict) or region not in overlays:
                return apply_pcs_overrides(global_raw, overrides)
            overlay = overlays[region]
            if not isinstance(overlay, dict):
                raise ConfigValidationError(
                    f"{PCS_REGIONS_KEY}.{region} must be an object, got {overlay!r}"
                )
            base: dict[str, Any] = global_raw if isinstance(global_raw, dict) else {}
            # The run-scoped overrides win over the region's overlay, as the
            # tracing and EKS-capabilities overrides win over their blocks.
            return apply_pcs_overrides(_deep_merge(base, overlay), overrides)
        except PcsConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def _pcs_fsx_enabled(self, region: str | None) -> bool:
        """Whether ``pcs.managed.storage.scratch: fsx`` can be honored.

        Per region this is that region's ``fsx_lustre`` toggle; for the global
        block it is "every deployment region has FSx", like the Slinky rule.
        Consulted only when the block actually asks for ``fsx``: the FSx lookup
        validates the run-scoped ``feature_enabled_overrides`` as a side effect,
        and a PCS block that never asked for FSx must not turn an unrelated
        override typo into a PCS error.
        """
        raw = self._raw_pcs_config(region)
        managed = raw.get("managed") if isinstance(raw, dict) else None
        storage = managed.get("storage") if isinstance(managed, dict) else None
        if not isinstance(storage, dict) or storage.get("scratch") != "fsx":
            return False
        if region is not None:
            return bool(self.get_fsx_lustre_config(region)["enabled"])
        regions = self.get_regions()
        return bool(regions) and all(
            bool(self.get_fsx_lustre_config(r)["enabled"]) for r in regions
        )

    def _validate_pcs_config(self) -> None:
        """Validate the optional AWS PCS block (``pcs`` and ``pcs_regions``).

        PCS is off by default, so an absent block is valid. The shape rules
        live in :mod:`gco.pcs_config`: unknown keys fail by name, ``existing``
        mode needs a cluster id, managed mode needs coherent node groups and
        queues with exactly one default, reservation types and purchase options
        must agree, and ``storage.scratch: fsx`` needs FSx in the region. The
        global block and every per-region overlay are validated separately so
        the error names the region whose overlay is wrong.
        """
        overlays = self.app.node.try_get_context(PCS_REGIONS_KEY)
        if overlays is not None and not isinstance(overlays, dict):
            raise ConfigValidationError(
                f"{PCS_REGIONS_KEY} must be an object keyed by region, got {overlays!r}"
            )
        try:
            validate_pcs_config(self._raw_pcs_config(), fsx_enabled=self._pcs_fsx_enabled(None))
            for region in sorted(overlays or {}):
                validate_pcs_config(
                    self._raw_pcs_config(str(region)),
                    fsx_enabled=self._pcs_fsx_enabled(str(region)),
                )
        except PcsConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_pcs_config(self, region: str | None = None) -> dict[str, Any]:
        """Return the ``pcs`` block for a region with defaults merged in.

        Keys: ``enabled``, ``mode``, ``cluster_id``, ``endpoint_cidr``,
        ``identity``, ``managed`` (see :data:`gco.pcs_config.PCS_DEFAULTS`).
        ``region`` applies that region's ``pcs_regions`` overlay; the
        run-scoped ``feature_enabled_overrides=pcs`` context forces
        ``enabled`` on, like the other feature blocks.
        """
        try:
            config = validate_pcs_config(
                self._raw_pcs_config(region), fsx_enabled=self._pcs_fsx_enabled(region)
            )
        except PcsConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc
        if self._feature_override_enabled(PCS_FEATURE_KEY):
            config["enabled"] = True
        return config

    def _raw_hyperpod_config(self, region: str | None = None) -> object:
        """The cdk.json ``hyperpod`` block with the region's ``hyperpod_regions`` overlay applied.

        Same layering as :meth:`_raw_pcs_config`: the per-region overlay is a
        patch over the global block, and the run-scoped ``hyperpod_overrides``
        context (a JSON object) is merged last.
        """
        global_raw = self.app.node.try_get_context(HYPERPOD_FEATURE_KEY)
        overrides = self.app.node.try_get_context(HYPERPOD_OVERRIDES_CONTEXT_KEY)
        try:
            if region is None:
                return apply_hyperpod_overrides(global_raw, overrides)
            overlays = self.app.node.try_get_context(HYPERPOD_REGIONS_KEY)
            if not isinstance(overlays, dict) or region not in overlays:
                return apply_hyperpod_overrides(global_raw, overrides)
            overlay = overlays[region]
            if not isinstance(overlay, dict):
                raise ConfigValidationError(
                    f"{HYPERPOD_REGIONS_KEY}.{region} must be an object, got {overlay!r}"
                )
            base: dict[str, Any] = global_raw if isinstance(global_raw, dict) else {}
            return apply_hyperpod_overrides(_deep_merge(base, overlay), overrides)
        except HyperpodConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def _validate_hyperpod_config(self) -> None:
        """Validate the optional SageMaker HyperPod block (``hyperpod`` and ``hyperpod_regions``).

        HyperPod is off by default, so an absent block is valid. The shape
        rules live in :mod:`gco.hyperpod_config`; the global block and every
        per-region overlay are validated separately so the error names the
        region whose overlay is wrong.
        """
        overlays = self.app.node.try_get_context(HYPERPOD_REGIONS_KEY)
        if overlays is not None and not isinstance(overlays, dict):
            raise ConfigValidationError(
                f"{HYPERPOD_REGIONS_KEY} must be an object keyed by region, got {overlays!r}"
            )
        try:
            validate_hyperpod_config(self._raw_hyperpod_config())
            for region in sorted(overlays or {}):
                validate_hyperpod_config(self._raw_hyperpod_config(str(region)))
        except HyperpodConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_hyperpod_config(self, region: str | None = None) -> dict[str, Any]:
        """Return the ``hyperpod`` block for a region with defaults merged in.

        Keys: ``enabled``, ``cluster_name``, ``node_recovery``,
        ``availability_zone``, ``instance_groups``, ``queue``, ``removal_policy``
        (see :data:`gco.hyperpod_config.HYPERPOD_DEFAULTS`). ``region`` applies
        that region's ``hyperpod_regions`` overlay; the run-scoped
        ``feature_enabled_overrides=hyperpod`` context forces ``enabled`` on.
        """
        try:
            config = validate_hyperpod_config(self._raw_hyperpod_config(region))
        except HyperpodConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc
        if self._feature_override_enabled(HYPERPOD_FEATURE_KEY):
            config["enabled"] = True
        return config

    def get_eks_capabilities_config(self) -> dict[str, Any]:
        """Return the ``eks_capabilities`` block with defaults merged in.

        Keys: ``ack`` / ``kro``, each ``{enabled, regions, ...}``
        (see :data:`gco.eks_capabilities_config.EKS_CAPABILITIES_DEFAULTS`).
        Both are disabled by default; ``regions: []`` means every
        regional deployment region. Honors the run-scoped
        ``eks_capabilities_overrides`` context like the validator does.
        """
        try:
            return validate_eks_capabilities_config(self._raw_eks_capabilities_config(), None)
        except EksCapabilitiesConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_vpc_endpoints_config(self) -> dict[str, list[str]]:
        """Return the VPC endpoint selection with defaults merged in.

        Defaults: the two free gateway endpoints (S3 carries the platform's
        largest data path — models, datasets, checkpoints, MLflow artifacts,
        cost reports — off the NAT gateways' per-GB metering; DynamoDB serves
        single-region topologies) and no interface endpoints.
        """
        configured = self.app.node.try_get_context("vpc_endpoints") or {}
        return {
            "gateway": list(configured.get("gateway", _VPC_ENDPOINTS_DEFAULTS["gateway"])),
            "interface": list(configured.get("interface", _VPC_ENDPOINTS_DEFAULTS["interface"])),
        }

    def _validate_analytics_environment_config(self) -> None:
        """Validate the optional analytics_environment block in cdk.json.

        The block is entirely optional; absence means the feature is disabled
        and no validation is needed. When present, we validate:

        - ``enabled``: must be a bool if present (defaults to False via merge).
        - ``hyperpod.enabled``: must be a bool if present (defaults to False).
        - ``cognito.removal_policy`` and ``efs.removal_policy``: must be the
          literal strings ``"destroy"`` or ``"retain"`` (case sensitive — they
          are passed verbatim to CDK's ``RemovalPolicy`` lookup by the
          consumer).
        """
        analytics_ctx = self.app.node.try_get_context("analytics_environment")
        if not isinstance(analytics_ctx, dict):
            # Block is absent or malformed — defaults apply, nothing to validate.
            return

        # Top-level `enabled` must be a bool if provided.
        if "enabled" in analytics_ctx and not isinstance(analytics_ctx["enabled"], bool):
            raise ConfigValidationError(
                f"analytics_environment.enabled must be a bool, got "
                f"{type(analytics_ctx['enabled']).__name__}: {analytics_ctx['enabled']!r}"
            )

        # `hyperpod.enabled` must be a bool if the sub-block is a dict and
        # carries the key.
        hyperpod_ctx = analytics_ctx.get("hyperpod")
        if (
            isinstance(hyperpod_ctx, dict)
            and "enabled" in hyperpod_ctx
            and not isinstance(hyperpod_ctx["enabled"], bool)
        ):
            raise ConfigValidationError(
                f"analytics_environment.hyperpod.enabled must be a bool, got "
                f"{type(hyperpod_ctx['enabled']).__name__}: {hyperpod_ctx['enabled']!r}"
            )

        # `canvas.enabled` must be a bool if the sub-block is a dict and
        # carries the key. Mirrors the hyperpod validation above.
        canvas_ctx = analytics_ctx.get("canvas")
        if (
            isinstance(canvas_ctx, dict)
            and "enabled" in canvas_ctx
            and not isinstance(canvas_ctx["enabled"], bool)
        ):
            raise ConfigValidationError(
                f"analytics_environment.canvas.enabled must be a bool, got "
                f"{type(canvas_ctx['enabled']).__name__}: {canvas_ctx['enabled']!r}"
            )

        valid_removal_policies = {"destroy", "retain"}

        for sub_block in ("cognito", "efs"):
            sub_ctx = analytics_ctx.get(sub_block)
            if not isinstance(sub_ctx, dict):
                continue
            if "removal_policy" not in sub_ctx:
                continue
            removal_policy = sub_ctx["removal_policy"]
            if removal_policy not in valid_removal_policies:
                raise ConfigValidationError(
                    f"analytics_environment.{sub_block}.removal_policy must be one of "
                    f"{sorted(valid_removal_policies)}, got {removal_policy!r}"
                )

    def _validate_cluster_observability_config(self) -> None:
        """Validate the optional cluster_observability block in cdk.json.

        The block is entirely optional; absence means the on-by-default
        defaults apply and nothing needs validating. When present, we check:

        - ``enabled``: must be a bool if present (defaults to True via merge —
          in-cluster observability is on unless explicitly disabled).
        - ``grafana``/``prometheus``/``alertmanager`` sub-block ``persistence_size``
          and ``prometheus.retention``: must be non-empty strings if present
          (they are passed verbatim to Helm chart values as Kubernetes
          quantity / duration strings).
        - ``alertmanager.enabled``: must be a bool if present.
        """
        obs_ctx = self.app.node.try_get_context("cluster_observability")
        if not isinstance(obs_ctx, dict):
            # Block is absent or malformed — defaults apply, nothing to validate.
            return

        if "enabled" in obs_ctx and not isinstance(obs_ctx["enabled"], bool):
            raise ConfigValidationError(
                f"cluster_observability.enabled must be a bool, got "
                f"{type(obs_ctx['enabled']).__name__}: {obs_ctx['enabled']!r}"
            )

        # The volume sizes render verbatim into the charts' PVC requests, so
        # each must be a positive Kubernetes quantity string ("10Gi"); a typo
        # such as "10GB" or "10 Gi" used to fail the Helm install in every
        # Region. The Prometheus retention is a duration string.
        for sub_block in ("grafana", "prometheus", "alertmanager", "mlflow"):
            sub_ctx = obs_ctx.get(sub_block)
            if isinstance(sub_ctx, dict) and "persistence_size" in sub_ctx:
                self._validate_volume_size(
                    f"cluster_observability.{sub_block}.persistence_size",
                    sub_ctx["persistence_size"],
                )
        prometheus_ctx = obs_ctx.get("prometheus")
        if isinstance(prometheus_ctx, dict) and "retention" in prometheus_ctx:
            retention = prometheus_ctx["retention"]
            if not isinstance(retention, str) or not retention.strip():
                raise ConfigValidationError(
                    "cluster_observability.prometheus.retention must be a non-empty "
                    f"string, got {retention!r}"
                )

        # `grafana.admin_password_rotation_schedule`: the cron for the in-cluster
        # CronJob that rotates the Grafana admin password. Validate the 5-field
        # cron shape so a typo fails at synth rather than yielding an
        # un-schedulable CronJob in every region.
        grafana_ctx = obs_ctx.get("grafana")
        if isinstance(grafana_ctx, dict) and "admin_password_rotation_schedule" in grafana_ctx:
            schedule = grafana_ctx["admin_password_rotation_schedule"]
            if not isinstance(schedule, str) or len(schedule.split()) != 5:
                raise ConfigValidationError(
                    "cluster_observability.grafana.admin_password_rotation_schedule must be "
                    f"a 5-field cron expression string, got {schedule!r}"
                )

        # `mlflow`: the tracking-server sub-toggle and its metadata volume.
        # Earlier releases read `enabled` by truthiness (`"false"` meant on),
        # so it goes through parse_block_flag and the getter returns the
        # parsed boolean.
        mlflow_ctx = _object_or_empty("cluster_observability.mlflow", obs_ctx.get("mlflow"))
        _check_block_keys(
            "cluster_observability.mlflow", mlflow_ctx, ("enabled", "persistence_size")
        )
        if "enabled" in mlflow_ctx:
            self._parse_mlflow_enabled(mlflow_ctx["enabled"])
        # `alertmanager.enabled` must be a bool if the sub-block carries it.
        alertmanager_ctx = obs_ctx.get("alertmanager")
        if (
            isinstance(alertmanager_ctx, dict)
            and "enabled" in alertmanager_ctx
            and not isinstance(alertmanager_ctx["enabled"], bool)
        ):
            raise ConfigValidationError(
                f"cluster_observability.alertmanager.enabled must be a bool, got "
                f"{type(alertmanager_ctx['enabled']).__name__}: "
                f"{alertmanager_ctx['enabled']!r}"
            )

    @staticmethod
    def _validate_volume_size(path: str, value: object) -> None:
        """A PVC size must be a positive Kubernetes quantity string such as ``"10Gi"``."""
        try:
            if not isinstance(value, str):
                raise ValueError("it is not a string")
            if memory_bytes(value) <= 0:
                raise ValueError("it is not positive")
        except ValueError as exc:
            raise ConfigValidationError(
                f"{path} must be a positive Kubernetes quantity string such as '10Gi', "
                f"got {value!r} ({exc})"
            ) from exc

    def _validate_cost_monitoring_config(self) -> None:
        """Validate the optional ``cost_monitoring`` block in cdk.json.

        The block is entirely optional; absence means the on-by-default
        defaults apply and nothing needs validating. When present, we check:

        - ``enabled``: must be a bool if present (defaults to True via merge).
        - ``reports.interval_minutes``: positive int between 5 and 1440 if
          present (the cost-monitor service's scheduled report cadence).
        - ``reports.retention_days`` /
          ``reports.transition_to_infrequent_access_days`` /
          ``athena.query_results_retention_days``: positive ints if present.
        - The IA transition must happen strictly before expiration, otherwise
          the S3 lifecycle configuration is rejected at deploy time — fail at
          synth instead.

        There is deliberately no cross-toggle error against
        ``cluster_observability``: cost monitoring's *effective* enablement is
        the conjunction of both toggles (see
        :meth:`get_cost_monitoring_enabled`), so disabling observability
        simply switches the cost pipeline off with it — ``gco monitoring
        disable`` must not break synthesis.
        """
        cost_ctx = self.app.node.try_get_context("cost_monitoring")
        if not isinstance(cost_ctx, dict):
            # Block absent or malformed — defaults apply, nothing to validate.
            return

        if "enabled" in cost_ctx and not isinstance(cost_ctx["enabled"], bool):
            raise ConfigValidationError(
                f"cost_monitoring.enabled must be a bool, got "
                f"{type(cost_ctx['enabled']).__name__}: {cost_ctx['enabled']!r}"
            )

        int_fields = (
            ("reports", "interval_minutes", 5, 1_440),
            ("reports", "retention_days", 1, 3_650),
            ("reports", "transition_to_infrequent_access_days", 30, 3_650),
            ("athena", "query_results_retention_days", 1, 3_650),
        )
        for sub_block, field, minimum, maximum in int_fields:
            sub_ctx = cost_ctx.get(sub_block)
            if not isinstance(sub_ctx, dict) or field not in sub_ctx:
                continue
            value = sub_ctx[field]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ConfigValidationError(
                    f"cost_monitoring.{sub_block}.{field} must be an integer between "
                    f"{minimum} and {maximum}, got {value!r}"
                )

        merged = self.get_cost_monitoring_config()
        reports = merged["reports"]
        if reports["transition_to_infrequent_access_days"] >= reports["retention_days"]:
            raise ConfigValidationError(
                "cost_monitoring.reports.transition_to_infrequent_access_days "
                f"({reports['transition_to_infrequent_access_days']}) must be smaller than "
                f"cost_monitoring.reports.retention_days ({reports['retention_days']}); "
                "S3 rejects lifecycle rules that transition on or after expiration."
            )

    def _validate_capacity_history_config(self) -> None:
        """Validate the optional ``historical`` block in cdk.json.

        The block is entirely optional; absence means the historical capacity
        surface is disabled and no validation is needed. When present, types
        are validated so a typo fails fast at synth time:

        - ``enabled``: bool if present.
        - ``retention_days`` / ``poll_interval_minutes``: positive ints if present.
        - ``watch_instance_types`` / ``enabled_regions``: lists of strings if present.
        - every region in ``enabled_regions`` must be a known AWS region.
        - ``spot_score_target_capacities``: non-empty list of positive
          integers (booleans rejected), every value a member of the supported
          set exported by ``cli/capacity/history.py`` — metric fields are
          statically named, so an unsupported capacity has nowhere to land.
        """
        historical_ctx = self.app.node.try_get_context("historical")
        if not isinstance(historical_ctx, dict):
            return

        if "enabled" in historical_ctx and not isinstance(historical_ctx["enabled"], bool):
            raise ConfigValidationError(
                f"historical.enabled must be a bool, got "
                f"{type(historical_ctx['enabled']).__name__}: {historical_ctx['enabled']!r}"
            )

        for int_field in (
            "retention_days",
            "poll_interval_minutes",
            "capacity_block_duration_hours",
        ):
            if int_field not in historical_ctx:
                continue
            value = historical_ctx[int_field]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigValidationError(
                    f"historical.{int_field} must be a positive integer, got {value!r}"
                )

        # The long-block probe duration may be 0 to disable the long probe
        # entirely, so it is validated as non-negative rather than positive.
        if "capacity_block_long_duration_hours" in historical_ctx:
            long_value = historical_ctx["capacity_block_long_duration_hours"]
            if not isinstance(long_value, int) or isinstance(long_value, bool) or long_value < 0:
                raise ConfigValidationError(
                    "historical.capacity_block_long_duration_hours must be a non-negative "
                    f"integer (0 disables the long probe), got {long_value!r}"
                )

        for list_field in ("watch_instance_types", "enabled_regions"):
            if list_field not in historical_ctx:
                continue
            value = historical_ctx[list_field]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigValidationError(
                    f"historical.{list_field} must be a list of strings, got {value!r}"
                )

        for region in historical_ctx.get("enabled_regions", []) or []:
            if region not in self.VALID_REGIONS:
                raise ConfigValidationError(
                    f"historical.enabled_regions contains invalid region '{region}'. "
                    f"Valid regions: {sorted(self.VALID_REGIONS)}"
                )

        if "spot_score_target_capacities" in historical_ctx:
            # Function-local import: cli/capacity/history.py owns the supported
            # set and the capacity->field naming rule, and imports nothing from
            # gco, so validation and storage cannot drift apart.
            from cli.capacity.history import SUPPORTED_SPOT_SCORE_TARGET_CAPACITIES

            capacities = historical_ctx["spot_score_target_capacities"]
            if (
                not isinstance(capacities, list)
                or not capacities
                or not all(
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                    for value in capacities
                )
            ):
                raise ConfigValidationError(
                    "historical.spot_score_target_capacities must be a non-empty list "
                    f"of positive integers, got {capacities!r}"
                )
            for value in capacities:
                if value not in SUPPORTED_SPOT_SCORE_TARGET_CAPACITIES:
                    raise ConfigValidationError(
                        "historical.spot_score_target_capacities contains unsupported "
                        f"target capacity {value!r}. Supported target capacities: "
                        f"{list(SUPPORTED_SPOT_SCORE_TARGET_CAPACITIES)} (each needs a "
                        "statically declared metric field; see cli/capacity/history.py)"
                    )

    #: Distance functions the DynamoDB vector-index API accepts. The choice is
    #: immutable after index creation, so a typo must fail at synth time.
    MISSION_MEMORY_DISTANCE_FUNCTIONS = frozenset({"COSINE", "DOT_PRODUCT", "EUCLIDEAN"})

    #: DynamoDB vector-index maximum dimensionality (service quota).
    MISSION_MEMORY_MAX_DIMENSIONS = 4096

    def _validate_mission_memory_config(self) -> None:
        """Validate the ``mission_memory`` block in cdk.json.

        The block is optional; absence means the shipped defaults apply
        (feature on). When present, types are validated so a typo fails fast
        at synth time — especially the one-way-door fields (``dimensions``,
        ``distance_function``) that cannot be corrected after the vector
        index exists:

        - ``enabled``: bool if present.
        - ``retention_days`` / ``top_k``: positive ints if present.
        - ``dimensions``: positive int <= 4096 if present.
        - ``distance_function``: one of COSINE / DOT_PRODUCT / EUCLIDEAN.
        """
        mission_memory_ctx = self.app.node.try_get_context("mission_memory")
        if not isinstance(mission_memory_ctx, dict):
            return

        if "enabled" in mission_memory_ctx and not isinstance(mission_memory_ctx["enabled"], bool):
            raise ConfigValidationError(
                f"mission_memory.enabled must be a bool, got "
                f"{type(mission_memory_ctx['enabled']).__name__}: "
                f"{mission_memory_ctx['enabled']!r}"
            )

        for int_field in ("retention_days", "top_k"):
            if int_field not in mission_memory_ctx:
                continue
            value = mission_memory_ctx[int_field]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigValidationError(
                    f"mission_memory.{int_field} must be a positive integer, got {value!r}"
                )

        if "dimensions" in mission_memory_ctx:
            dimensions = mission_memory_ctx["dimensions"]
            if (
                not isinstance(dimensions, int)
                or isinstance(dimensions, bool)
                or dimensions <= 0
                or dimensions > self.MISSION_MEMORY_MAX_DIMENSIONS
            ):
                raise ConfigValidationError(
                    "mission_memory.dimensions must be a positive integer <= "
                    f"{self.MISSION_MEMORY_MAX_DIMENSIONS} (DynamoDB vector-index "
                    f"limit), got {dimensions!r}. This is a one-way door: it is "
                    "immutable after index creation and must match the "
                    "bedrock.embedding_model_id output width."
                )

        if "distance_function" in mission_memory_ctx:
            distance = mission_memory_ctx["distance_function"]
            if (
                not isinstance(distance, str)
                or distance not in self.MISSION_MEMORY_DISTANCE_FUNCTIONS
            ):
                valid = ", ".join(sorted(self.MISSION_MEMORY_DISTANCE_FUNCTIONS))
                raise ConfigValidationError(
                    f"mission_memory.distance_function must be one of {valid}, got "
                    f"{distance!r}. This is immutable after index creation."
                )

    #: A Bedrock foundation-model id (``provider.model[:version]``), the shape
    #: the ``foundation-model/<id>`` resource ARN and InvokeModel's modelId take.
    BEDROCK_FOUNDATION_MODEL_ID_PATTERN = re.compile(
        r"[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}(?:[.:][a-z0-9-]{1,63})*"
    )

    def _validate_vector_store_config(self) -> None:
        """Validate the optional ``vector_store`` block in cdk.json.

        The block is optional; absence means the feature stays off. When
        present, types are validated so a typo fails fast at synth time —
        especially the one-way-door fields (``dimensions``,
        ``distance_function``) that cannot be corrected after the vector
        index exists. The distance-function set and dimension ceiling reuse
        the mission-memory constants because both features target the same
        DynamoDB vector-index API limits:

        - ``enabled``: bool if present.
        - ``dimensions``: positive int <= 4096 if present.
        - ``distance_function``: one of COSINE / DOT_PRODUCT / EUCLIDEAN.
        - ``embedding_model_id``: non-empty string. Deliberately independent
          of ``bedrock.embedding_model_id`` (mission memory) — the two
          corpora may use different models.
        - ``replica_regions``: list of known regions, no duplicates, and
          never the global region (that is the table's primary).
        - ``corpus_prefix``: non-empty S3 key prefix ending in ``/``.
        """
        vector_store_ctx = self.app.node.try_get_context("vector_store")
        if not isinstance(vector_store_ctx, dict):
            return

        if "enabled" in vector_store_ctx and not isinstance(vector_store_ctx["enabled"], bool):
            raise ConfigValidationError(
                f"vector_store.enabled must be a bool, got "
                f"{type(vector_store_ctx['enabled']).__name__}: "
                f"{vector_store_ctx['enabled']!r}"
            )
        if "dimensions" in vector_store_ctx:
            dimensions = vector_store_ctx["dimensions"]
            if (
                not isinstance(dimensions, int)
                or isinstance(dimensions, bool)
                or dimensions <= 0
                or dimensions > self.MISSION_MEMORY_MAX_DIMENSIONS
            ):
                raise ConfigValidationError(
                    "vector_store.dimensions must be a positive integer <= "
                    f"{self.MISSION_MEMORY_MAX_DIMENSIONS} (DynamoDB vector-index "
                    f"limit), got {dimensions!r}. This is a one-way door: it is "
                    "immutable after index creation and must match the "
                    "vector_store.embedding_model_id output width."
                )
        if "distance_function" in vector_store_ctx:
            distance = vector_store_ctx["distance_function"]
            if (
                not isinstance(distance, str)
                or distance not in self.MISSION_MEMORY_DISTANCE_FUNCTIONS
            ):
                valid = ", ".join(sorted(self.MISSION_MEMORY_DISTANCE_FUNCTIONS))
                raise ConfigValidationError(
                    f"vector_store.distance_function must be one of {valid}, got "
                    f"{distance!r}. This is immutable after index creation."
                )
        if "embedding_model_id" in vector_store_ctx:
            model_id = vector_store_ctx["embedding_model_id"]
            if not isinstance(
                model_id, str
            ) or not self.BEDROCK_FOUNDATION_MODEL_ID_PATTERN.fullmatch(model_id.strip()):
                raise ConfigValidationError(
                    "vector_store.embedding_model_id must be a Bedrock foundation-model id such "
                    f"as 'amazon.titan-embed-text-v2:0', got {model_id!r}; the stacks grant "
                    "bedrock:InvokeModel on foundation-model/<id> and the ingest Lambda "
                    "invokes it by that id"
                )
        if "replica_regions" in vector_store_ctx:
            replica_regions = vector_store_ctx["replica_regions"]
            if not isinstance(replica_regions, list) or not all(
                isinstance(region, str) for region in replica_regions
            ):
                raise ConfigValidationError(
                    f"vector_store.replica_regions must be a list of region strings, "
                    f"got {replica_regions!r}"
                )
            if len(replica_regions) != len(set(replica_regions)):
                raise ConfigValidationError(
                    f"vector_store.replica_regions contains duplicates: {replica_regions!r}"
                )
            global_region = self.get_global_region()
            for region in replica_regions:
                if region not in self.VALID_REGIONS:
                    raise ConfigValidationError(
                        f"vector_store.replica_regions contains invalid region "
                        f"'{region}'. Valid regions: {sorted(self.VALID_REGIONS)}"
                    )
                if region == global_region:
                    raise ConfigValidationError(
                        f"vector_store.replica_regions must not include the global "
                        f"region '{global_region}': the primary table already lives "
                        "there and a global table cannot replicate into its own region."
                    )
        if "corpus_prefix" in vector_store_ctx:
            corpus_prefix = vector_store_ctx["corpus_prefix"]
            if (
                not isinstance(corpus_prefix, str)
                or not corpus_prefix.strip()
                or not corpus_prefix.endswith("/")
                or corpus_prefix.startswith("/")
            ):
                raise ConfigValidationError(
                    "vector_store.corpus_prefix must be a non-empty S3 key prefix "
                    f"ending in '/' (and not starting with '/'), got {corpus_prefix!r}"
                )

    def get_project_name(self) -> str:
        """Get project name from configuration"""
        return self.app.node.try_get_context("project_name") or "gco"

    def get_deployment_regions(self) -> dict[str, Any]:
        """Get deployment regions configuration.

        Returns a dict with:
        - global: Region for Global Accelerator and SSM parameters (default: us-east-2)
        - api_gateway: Region for API Gateway stack (default: us-east-2)
        - monitoring: Region for Monitoring stack (default: us-east-2)
        - regional: List of regions for EKS clusters (default: ["us-east-1"]).
          An explicitly empty list is valid and means a control-plane-only
          deployment: only the global, API Gateway, and monitoring stacks are
          synthesized until a workload Region is added back.

        Note: Global Accelerator is a global service but requires a "home" region
        for CloudFormation deployment. us-east-2 is used by default to keep
        global infrastructure separate from workload regions.
        """
        deployment_regions = self.app.node.try_get_context("deployment_regions") or {}

        regional = deployment_regions.get("regional", ["us-east-1"])
        if self.is_control_plane_only():
            regional = []
        return {
            "global": deployment_regions.get("global", "us-east-2"),
            "api_gateway": deployment_regions.get("api_gateway", "us-east-2"),
            "monitoring": deployment_regions.get("monitoring", "us-east-2"),
            "regional": regional,
        }

    def is_control_plane_only(self) -> bool:
        """Return whether this run synthesizes with zero workload Regions.

        True when ``--context gco:control-plane-only=true`` was passed (see
        :data:`gco.stacks.constants.CONTROL_PLANE_ONLY_CONTEXT_KEY`). The
        configured ``deployment_regions.regional`` list in ``cdk.json`` is
        left untouched; only this synthesis ignores it.
        """
        return control_plane_only_requested(
            self.app.node.try_get_context(CONTROL_PLANE_ONLY_CONTEXT_KEY)
        )

    def get_deployment_partition(self) -> str:
        """Return the one SDK partition shared by every configured Region."""
        deployment_regions = self.get_deployment_regions()
        regional = validated_regional_deployment_regions(
            deployment_regions["regional"],
            known_regions=self.VALID_REGIONS,
        )
        return validated_deployment_partition(
            (
                deployment_regions["global"],
                deployment_regions["api_gateway"],
                deployment_regions["monitoring"],
                *regional,
            )
        )

    def supports_global_accelerator(self) -> bool:
        """Return whether this partition exposes the Global Accelerator topology."""
        return self.get_deployment_partition() == "aws"

    def get_global_region(self) -> str:
        """Get the region for global resources and shared SSM parameters."""
        region = self.get_deployment_regions()["global"]
        return str(region)

    def get_api_gateway_region(self) -> str:
        """Get the region for API Gateway stack."""
        region = self.get_deployment_regions()["api_gateway"]
        return str(region)

    def get_monitoring_region(self) -> str:
        """Get the region for Monitoring stack."""
        region = self.get_deployment_regions()["monitoring"]
        return str(region)

    def get_regions(self) -> list[str]:
        """Get list of regions for EKS cluster deployment (empty when scaled to zero)."""
        deployment_regions = self.get_deployment_regions()
        regional = deployment_regions["regional"]
        return list(regional) if isinstance(regional, list) else [str(regional)]

    def get_kubernetes_version(self) -> str:
        """Get Kubernetes version from configuration"""
        return self.app.node.try_get_context("kubernetes_version") or "1.37"

    def get_resource_thresholds(self) -> ResourceThresholds:
        """Get resource thresholds configuration.

        ``cpu_threshold``, ``memory_threshold`` and ``gpu_threshold`` are
        required (see :meth:`_validate_resource_thresholds`). An omitted
        ``pending_*`` key takes the :class:`ResourceThresholds` default, the
        same value the health monitor falls back to: 10 pods, 100 vCPUs,
        200 GB and 8 GPUs. The shipped cdk.json sets
        ``pending_requested_gpus`` to -1 (disabled), so 8 applies only to a
        file that leaves the key out.
        """
        thresholds_config = self.app.node.try_get_context("resource_thresholds") or {}
        optional = {
            key: thresholds_config[key]
            for key in (
                "pending_pods_threshold",
                "pending_requested_cpu_vcpus",
                "pending_requested_memory_gb",
                "pending_requested_gpus",
            )
            if key in thresholds_config
        }
        return ResourceThresholds(
            cpu_threshold=thresholds_config["cpu_threshold"],
            memory_threshold=thresholds_config["memory_threshold"],
            gpu_threshold=thresholds_config["gpu_threshold"],
            **optional,
        )

    def get_cluster_config(self, region: str) -> ClusterConfig:
        """Get complete cluster configuration for a region"""
        return ClusterConfig(
            region=region,
            cluster_name=f"{self.get_project_name()}-{region}",
            kubernetes_version=self.get_kubernetes_version(),
            addons=["metrics-server"],
            resource_thresholds=self.get_resource_thresholds(),
        )

    def get_global_accelerator_config(self) -> dict[str, Any]:
        """Get the merged Global Accelerator configuration.

        Returns the ``global_accelerator`` block from cdk.json layered on top
        of the defaults below. This is a real merge, not the historical
        all-or-nothing fallback: a partial block keeps every unspecified
        default instead of silently dropping it. The ``traffic_dial``
        sub-block is deep-merged so overriding a single dial knob does not
        wipe the sub-block's other defaults, mirroring
        ``get_cost_monitoring_config``.

        Keys:
            - name: accelerator name (default ``<project_name>-accelerator``)
            - health_check_interval: seconds between endpoint-group probes;
              the Global Accelerator API accepts only 10 or 30 (default 30)
            - health_check_threshold: consecutive probes before an endpoint
              flips healthy/unhealthy, 1-10 (default 3)
            - health_check_path: HTTPS path probed on each regional ALB
              (default ``/api/v1/health``)
            - client_affinity: ``NONE`` or ``SOURCE_IP`` (default ``NONE``)
            - traffic_dial: capacity-driven traffic-dial controller
              sub-block (default disabled, ``monitor`` mode; knob reference
              in :meth:`_validate_traffic_dial_config`)
        """
        default_config: dict[str, Any] = {
            "name": f"{self.get_project_name()}-accelerator",
            "health_check_interval": 30,
            "health_check_threshold": 3,
            "health_check_path": "/api/v1/health",
            "client_affinity": "NONE",
            "traffic_dial": {
                "enabled": False,
                "mode": "monitor",
                "interval_minutes": 5,
                "lookback_minutes": 15,
                "min_dial_percentage": 10,
                "max_step_percentage": 20,
                "full_health_percentage": 95,
            },
        }
        configured = _object_or_empty(
            "global_accelerator", self.app.node.try_get_context("global_accelerator")
        )
        merged: dict[str, Any] = {**default_config, **configured}

        # Deep-merge the nested sub-block so a partial override does not drop
        # the other defaults in the same sub-block.
        merged["traffic_dial"] = {
            **cast(dict[str, Any], default_config["traffic_dial"]),
            **_object_or_empty("global_accelerator.traffic_dial", configured.get("traffic_dial")),
        }

        return merged

    def get_backend_tls_config(self) -> dict[str, Any]:
        """Return the mandatory deployment-local backend TLS lifecycle policy."""
        defaults = {
            "root_generation": 1,
            "root_validity_days": 3_650,
            "root_rotate_before_days": 180,
            "root_activation_delay_hours": 24,
            "root_overlap_days": 45,
            "leaf_validity_days": 30,
            "leaf_rotate_before_days": 10,
            "rotation_schedule_hours": 12,
            "trust_cache_ttl_seconds": 300,
            "trust_cache_max_stale_seconds": 3_600,
        }
        configured = self.app.node.try_get_context("backend_tls") or {}
        if not isinstance(configured, dict):
            raise ConfigValidationError("backend_tls must be a mapping")
        return {**defaults, **configured}

    def get_inference_proxy_config(self) -> dict[str, int]:
        """Return merged inference TLS proxy autoscaling settings.

        Omission is backward compatible and returns the shipped defaults. AWS
        CDK normalizes a top-level JSON ``null`` context value to omission, so
        ``None`` follows the same default-preserving contract.
        """
        defaults = {
            "tls_proxy_cpu_request_millicores": (
                INFERENCE_PROXY_TLS_CPU_REQUEST_MILLICORES_DEFAULT
            ),
            "tls_proxy_cpu_target_utilization_percentage": (
                INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION_DEFAULT
            ),
            "min_replicas": INFERENCE_PROXY_MIN_REPLICAS_DEFAULT,
            "max_replicas": INFERENCE_PROXY_MAX_REPLICAS_DEFAULT,
        }
        configured = self.app.node.try_get_context("inference_proxy")
        if configured is None:
            return dict(defaults)
        if not isinstance(configured, dict):
            raise ConfigValidationError(
                "inference_proxy must be an object, got "
                f"{type(configured).__name__}: {configured!r}"
            )

        unknown = sorted(str(key) for key in configured if key not in defaults)
        if unknown:
            unknown_paths = ", ".join(f"inference_proxy.{key}" for key in unknown)
            allowed_paths = ", ".join(f"inference_proxy.{key}" for key in sorted(defaults))
            raise ConfigValidationError(
                f"inference_proxy contains unknown key(s): {unknown_paths}; "
                f"allowed keys: {allowed_paths}"
            )
        return {**defaults, **configured}

    def get_gateway_health_check_config(self) -> dict[str, int]:
        """Return the platform ALB's target-group health check settings.

        The ``gateway_health_check`` block merged over
        :data:`gco.gateway_health_check_config.GATEWAY_HEALTH_CHECK_DEFAULTS`
        (15-second interval, 5-second timeout, 2 consecutive results to flip
        a target either way, the values the gateway manifest shipped with).
        The regional stack renders them into the default
        ``TargetGroupConfiguration`` of ``post-helm-gateway.yaml``, which
        every platform target group inherits.

        Keys:
            - interval_seconds: seconds between probes of one target (5-300)
            - timeout_seconds: seconds without a response that fail a probe
              (2-120, less than ``interval_seconds``)
            - healthy_threshold: consecutive passes that mark a target
              healthy (2-10)
            - unhealthy_threshold: consecutive failures that mark a target
              unhealthy (2-10)

        Raises:
            ConfigValidationError: If the block is malformed.
        """
        try:
            return validate_gateway_health_check_config(
                self.app.node.try_get_context(GATEWAY_HEALTH_CHECK_CONTEXT_KEY)
            )
        except GatewayHealthCheckConfigError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def get_manifest_processor_config(self) -> dict[str, Any]:
        """Get manifest processor configuration.

        Merges three cdk.json sections into a single runtime config:

        - ``manifest_processor``: service-specific settings (replicas,
          resource_limits, autoscaling, validation_enabled,
          max_request_body_bytes, yaml_max_depth, the central queue worker)
        - ``job_validation_policy``: shared validation policy
          (allowed_namespaces, resource_quotas, trusted_registries,
          trusted_dockerhub_orgs, manifest_security_policy, allowed_kinds).
          Pulled in verbatim, with no defaults of its own: the stack and the
          services read the same block, and an omitted trust list falls back
          to the gco.job_admission defaults inside the services.

        There is no image setting: the regional stack builds the image from
        dockerfiles/Dockerfile.manifest-processor, pushes it to ECR, and
        replaces the {{MANIFEST_PROCESSOR_IMAGE}} placeholder in the manifests
        with its URI. A ``manifest_processor.image`` key, which earlier
        releases required but never read, is reported as deprecated.
        """
        default_config = {
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
            "autoscaling": dict(_MANIFEST_PROCESSOR_AUTOSCALING_DEFAULTS),
            "validation_enabled": True,
            "max_request_body_bytes": DEFAULT_MAX_REQUEST_BODY_BYTES,
            "central_queue_worker_enabled": True,
            "central_queue_poll_interval_seconds": 10,
            "central_queue_batch_size": 5,
            "central_queue_reconcile_limit": 100,
            "central_queue_lease_seconds": 300,
            "central_queue_lease_renewal_seconds": 60,
            # The job_validation_policy keys (allowed_namespaces,
            # resource_quotas, trusted_registries, ...) are merged in below,
            # as written: job_validation_policy requires the first two, and the
            # trust lists' defaults live in gco.job_admission
            # (DEFAULT_TRUSTED_REGISTRIES / DEFAULT_TRUSTED_DOCKERHUB_ORGS),
            # which both services apply when the stack renders an empty list.
        }
        context_config = self.app.node.try_get_context("manifest_processor") or {}

        # Merge in the shared job_validation_policy section. These keys apply
        # to BOTH the manifest processor and the queue processor; they live
        # in their own top-level cdk.json section so neither service "owns"
        # them. We flatten them into the manifest processor's runtime config
        # so service code keeps its existing attribute layout.
        shared_policy = self.app.node.try_get_context("job_validation_policy") or {}
        merged = {**default_config, **context_config, **shared_policy}
        # Nested block: a partial ``autoscaling`` object (or JSON null) keeps
        # the unspecified defaults instead of replacing the whole mapping.
        # ``_validate_config`` has already rejected anything that is not a
        # mapping or null.
        merged["autoscaling"] = {
            **_MANIFEST_PROCESSOR_AUTOSCALING_DEFAULTS,
            **(context_config.get("autoscaling") or {}),
        }

        enabled = merged.get("central_queue_worker_enabled")
        if not isinstance(enabled, bool):
            raise ConfigValidationError(
                "manifest_processor.central_queue_worker_enabled must be a boolean"
            )
        for key, minimum, maximum in (
            ("central_queue_poll_interval_seconds", 1, 300),
            ("central_queue_batch_size", 1, 20),
            ("central_queue_reconcile_limit", 1, 500),
            ("central_queue_lease_seconds", 30, 3600),
            ("central_queue_lease_renewal_seconds", 1, 300),
        ):
            value = merged.get(key)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigValidationError(
                    f"manifest_processor.{key} must be an integer between {minimum} and {maximum}"
                )
        if (
            merged["central_queue_lease_renewal_seconds"] * 2
            > merged["central_queue_lease_seconds"]
        ):
            raise ConfigValidationError(
                "manifest_processor.central_queue_lease_renewal_seconds must be no more than "
                "half of central_queue_lease_seconds"
            )
        return merged

    def get_api_gateway_config(self) -> dict[str, Any]:
        """Get API Gateway configuration.

        Returns:
            API Gateway configuration dictionary with the following keys:
            - throttle_rate_limit: Requests per second limit
            - throttle_burst_limit: Burst capacity
            - log_level: CloudWatch logging level (OFF, ERROR, INFO)
            - metrics_enabled: Enable CloudWatch metrics
            - tracing_enabled: Enable X-Ray tracing
            - regional_api_enabled: In the commercial ``aws`` partition,
              permit direct same-account callers to use the always-deployed
              regional API bridges. Other partitions force this access on
              because the bridges are the supported workload ingress without
              Global Accelerator. Centralized aggregation always uses them.
        """
        default_config = {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
            "regional_api_enabled": False,
        }
        return {**default_config, **(self.app.node.try_get_context("api_gateway") or {})}

    def get_eks_cluster_config(self) -> dict[str, Any]:
        """Get EKS cluster configuration.

        Returns:
            EKS cluster configuration dictionary with the following keys:
            - endpoint_access: EKS API endpoint access mode
              - "PRIVATE": API server only accessible from within VPC (default, most secure)
              - "PUBLIC_AND_PRIVATE": API server accessible from internet and VPC
            - public_access_cidrs: CIDR allowlist for the public endpoint when
              endpoint_access is PUBLIC_AND_PRIVATE. Empty (the default) means
              0.0.0.0/0, which synthesis calls out with a loud warning.
            - developer_access: list of EKS access entries to synthesize for
              human principals, each ``{principal_arn, scope, namespaces}``.
              scope defaults to "namespace" and namespaces to ["gco-jobs"];
              scope "cluster" grants AmazonEKSClusterAdminPolicy instead.
              Empty (the default) synthesizes exactly today's entries.
            - network_policy_enforcement: whether the Auto Mode network policy
              controller is switched on (default True). False keeps the
              NetworkPolicy objects but stops enforcing them.

        Note:
            PRIVATE endpoint is recommended for production. Job submission still works
            via API Gateway → Lambda (in VPC) or SQS queues. For kubectl access with
            PRIVATE endpoint, use `gco cluster tunnel` (SSM), a bastion host, or a VPN —
            and an access entry for your principal either way (`gco stacks access`).
        """
        default_config: dict[str, Any] = {
            "endpoint_access": "PRIVATE",
            "public_access_cidrs": [],
            "developer_access": [],
            # Switches on the Auto Mode network policy controller
            # (06-network-policy-controller.yaml) so 03-network-policies.yaml
            # is enforced rather than merely stored.
            "network_policy_enforcement": True,
        }
        return {**default_config, **(self.app.node.try_get_context("eks_cluster") or {})}

    #: Keys of the ``fsx_lustre`` block and of each ``fsx_lustre_regions``
    #: overlay. ``node_group`` is deprecated: accepted for existing files,
    #: warned about and ignored.
    FSX_LUSTRE_KEYS = frozenset(
        {
            "enabled",
            "storage_capacity_gib",
            "deployment_type",
            "file_system_type_version",
            "per_unit_storage_throughput",
            "data_compression_type",
            "import_path",
            "export_path",
            "auto_import_policy",
            "node_group",
        }
    )

    def _validate_fsx_lustre_config(self) -> None:
        """Validate ``fsx_lustre`` and every ``fsx_lustre_regions`` overlay.

        Resolving the configuration is the validation: the global block is
        checked on its own and each overlay merged over it, so an error names
        the block (and Region) that holds the bad value.
        """
        self.get_fsx_lustre_config()
        overlays = _object_or_empty(
            FSX_LUSTRE_REGIONS_CONTEXT_KEY,
            self.app.node.try_get_context(FSX_LUSTRE_REGIONS_CONTEXT_KEY),
        )
        for region in sorted(overlays):
            self.get_fsx_lustre_config(str(region))

    def get_fsx_lustre_config(self, region: str | None = None) -> dict[str, Any]:
        """Get the validated FSx for Lustre configuration.

        Args:
            region: Optional region. When ``fsx_lustre_regions`` has an entry
                for it, that entry is merged over the global block.

        Returns:
            FSx configuration dictionary with the following keys:
            - enabled: Whether FSx is enabled (a real boolean; ``"true"`` /
              ``"false"`` strings are parsed, anything else is an error)
            - storage_capacity_gib: Storage capacity in GiB: 1200, 2400, then
              multiples of 3600 (SCRATCH_1) or 2400 (the other types)
            - deployment_type: SCRATCH_1, SCRATCH_2 (default), PERSISTENT_1,
              PERSISTENT_2
            - file_system_type_version: Lustre version 2.10, 2.12 or 2.15
              (default 2.15, which kernel 6.x clients such as AL2023 and
              Bottlerocket need; PERSISTENT_2 does not support 2.10)
            - per_unit_storage_throughput: MB/s per TiB for the PERSISTENT
              types (PERSISTENT_1: 50, 100 or 200, default 200;
              PERSISTENT_2: 125, 250, 500 or 1000, default 250); ``None`` for
              the SCRATCH types, which take no throughput setting (a
              configured value is ignored for them)
            - data_compression_type: LZ4 (default) or NONE
            - import_path: S3 path (``s3://...``) for data import, or None
            - export_path: S3 path for data export in the import bucket, or None
            - auto_import_policy: NONE, NEW, NEW_CHANGED or NEW_CHANGED_DELETED
              (default), used only with an import path

        FSx workloads run on the EKS Auto Mode node pools, so there is no
        node-group setting: a ``node_group`` key, which earlier releases
        documented but never read, is reported as deprecated and ignored.

        Raises:
            ConfigValidationError: If a block is not an object, names an
                unknown key, or holds a value FSx for Lustre rejects.
        """
        global_path = FSX_LUSTRE_CONTEXT_KEY
        global_block = _object_or_empty(
            global_path, self.app.node.try_get_context(FSX_LUSTRE_CONTEXT_KEY)
        )
        _check_block_keys(global_path, global_block, self.FSX_LUSTRE_KEYS)
        # Which block set each key, so an error names the value's own path.
        sources = dict.fromkeys(global_block, global_path)
        merged: dict[str, Any] = {**FSX_LUSTRE_DEFAULTS, **global_block}
        self._warn_fsx_node_group(global_path, global_block)

        if region:
            overlays = _object_or_empty(
                FSX_LUSTRE_REGIONS_CONTEXT_KEY,
                self.app.node.try_get_context(FSX_LUSTRE_REGIONS_CONTEXT_KEY),
            )
            if region in overlays:
                overlay_path = f"{FSX_LUSTRE_REGIONS_CONTEXT_KEY}.{region}"
                overlay = _object_or_empty(overlay_path, overlays[region])
                _check_block_keys(overlay_path, overlay, self.FSX_LUSTRE_KEYS)
                sources.update(dict.fromkeys(overlay, overlay_path))
                merged.update(overlay)
                self._warn_fsx_node_group(overlay_path, overlay)

        def path_of(key: str) -> str:
            return f"{sources.get(key, global_path)}.{key}"

        resolved = self._resolve_fsx_lustre(merged, path_of, self._warn)
        if self._feature_override_enabled("fsx_lustre"):
            resolved["enabled"] = True
        return resolved

    def _warn_fsx_node_group(self, path: str, block: Mapping[str, Any]) -> None:
        """Report a deprecated ``node_group`` key in an FSx block (it is ignored)."""
        if "node_group" in block:
            self._warn(
                f"{path}.node_group is deprecated and ignored: FSx workloads run on the "
                "EKS Auto Mode node pools and nothing reads it. Remove it from cdk.json."
            )

    @staticmethod
    def _resolve_fsx_lustre(
        merged: Mapping[str, Any],
        path_of: Callable[[str], str],
        warn: Callable[[str], None],
    ) -> dict[str, Any]:
        """Validate one merged FSx block and return it normalized.

        ``path_of(key)`` is the full cdk.json path of the value ``merged[key]``
        came from; ``warn`` receives the deprecation warnings.
        """
        enabled = parse_block_flag(
            merged["enabled"],
            path_of("enabled"),
            warn,
            off_deletes="the FSx for Lustre file system and the data on it",
        )
        deployment_type = _require_choice(
            path_of("deployment_type"), merged["deployment_type"], FSX_DEPLOYMENT_TYPES
        )
        capacity = _require_int(
            path_of("storage_capacity_gib"), merged["storage_capacity_gib"], minimum=1200
        )
        if not valid_storage_capacity(deployment_type, capacity):
            increment = FSX_CAPACITY_INCREMENT_GIB[deployment_type]
            raise ConfigValidationError(
                f"{path_of('storage_capacity_gib')} must be 1200, 2400 or a multiple of "
                f"{increment} GiB for {deployment_type}, got {capacity}"
            )
        version = _require_choice(
            path_of("file_system_type_version"),
            merged["file_system_type_version"],
            FSX_FILE_SYSTEM_TYPE_VERSIONS,
        )
        if deployment_type == "PERSISTENT_2" and version == "2.10":
            raise ConfigValidationError(
                f"{path_of('file_system_type_version')} 2.10 is not supported by "
                "PERSISTENT_2; use 2.12 or 2.15"
            )
        throughput: int | None = None
        choices = FSX_THROUGHPUT_CHOICES.get(deployment_type)
        if choices is not None:
            configured = merged["per_unit_storage_throughput"]
            if configured is None:
                throughput = default_per_unit_storage_throughput(deployment_type)
            elif type(configured) is not int or configured not in choices:
                raise ConfigValidationError(
                    f"{path_of('per_unit_storage_throughput')} must be one of "
                    f"{', '.join(str(choice) for choice in choices)} (MB/s per TiB) for "
                    f"{deployment_type}, got {configured!r}"
                )
            else:
                throughput = configured
        compression = _require_choice(
            path_of("data_compression_type"),
            merged["data_compression_type"],
            FSX_DATA_COMPRESSION_TYPES,
        )
        paths: dict[str, str | None] = {}
        for key in ("import_path", "export_path"):
            value = merged[key]
            if value is not None and (
                not isinstance(value, str) or not value.startswith("s3://") or len(value) < 6
            ):
                raise ConfigValidationError(
                    f"{path_of(key)} must be an S3 URI (s3://bucket[/prefix]) or null, "
                    f"got {value!r}"
                )
            paths[key] = value
        if paths["export_path"] is not None:
            export_bucket = paths["export_path"][5:].split("/", 1)[0]
            if paths["import_path"] is None:
                raise ConfigValidationError(
                    f"{path_of('export_path')} needs an import_path: FSx exports to the "
                    "bucket it imports from"
                )
            if paths["import_path"][5:].split("/", 1)[0] != export_bucket:
                raise ConfigValidationError(
                    f"{path_of('export_path')} must use the import_path bucket, got "
                    f"{paths['export_path']!r} for import_path {paths['import_path']!r}"
                )
        auto_import_policy = _require_choice(
            path_of("auto_import_policy"), merged["auto_import_policy"], FSX_AUTO_IMPORT_POLICIES
        )
        return {
            "enabled": enabled,
            "storage_capacity_gib": capacity,
            "deployment_type": deployment_type,
            "file_system_type_version": version,
            "per_unit_storage_throughput": throughput,
            "data_compression_type": compression,
            "import_path": paths["import_path"],
            "export_path": paths["export_path"],
            "auto_import_policy": auto_import_policy,
        }

    def _feature_override_enabled(self, feature_key: str) -> bool:
        """True when ``feature_enabled_overrides`` context forces this feature on."""
        overrides = parse_feature_enabled_overrides(
            self.app.node.try_get_context(FEATURE_OVERRIDE_CONTEXT_KEY)
        )
        return feature_key in overrides

    def get_valkey_config(self) -> dict[str, Any]:
        """Get the validated Valkey Serverless cache configuration.

        Returns:
            Valkey configuration dictionary with the following keys:
            - enabled: Whether Valkey cache is enabled (default: False)
            - max_data_storage_gb: Maximum data storage in GB, an integer of
              at least 1, the cache's minimum (default: 5)
            - max_ecpu_per_second: Maximum ECPUs per second, an integer of at
              least 1000, the cache's minimum (default: 5000)
            - snapshot_retention_limit: Daily snapshots to retain, an integer
              of at least 0 (default: 1)

        Raises:
            ConfigValidationError: If the block is malformed.
        """
        path = "valkey"
        block = _object_or_empty(path, self.app.node.try_get_context("valkey"))
        _check_block_keys(path, block, _VALKEY_DEFAULTS)
        merged = {**_VALKEY_DEFAULTS, **block}
        resolved: dict[str, Any] = {
            "enabled": parse_block_flag(
                merged["enabled"],
                f"{path}.enabled",
                self._warn,
                off_deletes="the Valkey cache and the data in it",
            ),
            "max_data_storage_gb": _require_int(
                f"{path}.max_data_storage_gb", merged["max_data_storage_gb"], minimum=1
            ),
            "max_ecpu_per_second": _require_int(
                f"{path}.max_ecpu_per_second", merged["max_ecpu_per_second"], minimum=1000
            ),
            "snapshot_retention_limit": _require_int(
                f"{path}.snapshot_retention_limit", merged["snapshot_retention_limit"], minimum=0
            ),
        }
        if self._feature_override_enabled("valkey"):
            resolved["enabled"] = True
        return resolved

    def get_aurora_pgvector_config(self) -> dict[str, Any]:
        """Get the validated Aurora Serverless v2 + pgvector configuration.

        Returns:
            Aurora pgvector configuration dictionary with the following keys:
            - enabled: Whether Aurora pgvector is enabled (default: False)
            - min_acu: Minimum Aurora Capacity Units, 0-256 in steps of 0.5
              (default: 0, scales to zero)
            - max_acu: Maximum Aurora Capacity Units, 1-256 in steps of 0.5
              and at least min_acu (default: 16)
            - backup_retention_days: Days to retain automated backups, 1-35
              (default: 7)
            - deletion_protection: Whether deletion protection is enabled
              (default: False)

        Raises:
            ConfigValidationError: If the block is malformed.
        """
        path = "aurora_pgvector"
        block = _object_or_empty(path, self.app.node.try_get_context("aurora_pgvector"))
        _check_block_keys(path, block, _AURORA_PGVECTOR_DEFAULTS)
        merged = {**_AURORA_PGVECTOR_DEFAULTS, **block}
        capacities: dict[str, float] = {}
        for key, minimum in (("min_acu", 0), ("max_acu", 1)):
            value = merged[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not minimum <= value <= 256
                or (value * 2) % 1 != 0
            ):
                raise ConfigValidationError(
                    f"{path}.{key} must be a number from {minimum} to 256 in steps of 0.5 "
                    f"(Aurora Serverless v2 capacity units), got {value!r}"
                )
            capacities[key] = value
        if capacities["max_acu"] < capacities["min_acu"]:
            raise ConfigValidationError(
                f"{path}.max_acu ({merged['max_acu']}) must be at least "
                f"{path}.min_acu ({merged['min_acu']})"
            )
        resolved: dict[str, Any] = {
            "enabled": parse_block_flag(
                merged["enabled"],
                f"{path}.enabled",
                self._warn,
                off_deletes="the Aurora pgvector cluster and its data",
            ),
            "min_acu": merged["min_acu"],
            "max_acu": merged["max_acu"],
            "backup_retention_days": _require_int(
                f"{path}.backup_retention_days", merged["backup_retention_days"], 1, 35
            ),
            # Earlier releases passed this value to the CDK construct as it
            # was, and a string failed the synth there; nothing has been
            # running on a string.
            "deletion_protection": parse_block_flag(
                merged["deletion_protection"],
                f"{path}.deletion_protection",
                self._warn,
                read_by_truthiness=False,
            ),
        }
        if self._feature_override_enabled("aurora_pgvector"):
            resolved["enabled"] = True
        return resolved

    def get_analytics_config(self) -> dict[str, Any]:
        """Get optional analytics environment configuration.

        Returns the fully-merged analytics_environment block from cdk.json
        layered on top of the defaults below. Sub-blocks (``hyperpod``,
        ``cognito``, ``efs``, ``studio``) are deep-merged so a user who
        overrides a single nested key (e.g. ``cognito.domain_prefix``) does
        not inadvertently wipe the sub-block's other defaults.

        Returns:
            Analytics configuration dictionary with the following keys:
            - enabled: Whether the analytics environment stack is deployed
              (default: False — the feature is off unless explicitly opted in)
            - hyperpod: SageMaker HyperPod integration sub-block
              - enabled: Whether to add the HyperPod IAM grants to
                SageMaker_Execution_Role (default: False)
            - canvas: SageMaker Canvas integration sub-block
              - enabled: Whether to enable the SageMaker Canvas app on
                the Studio domain and attach ``AmazonSageMakerCanvasFullAccess``
                to the SageMaker_Execution_Role (default: False)
            - cognito: Cognito user-pool sub-block
              - domain_prefix: UserPoolDomain prefix, or None to let the
                analytics stack derive one (default: None)
              - removal_policy: "destroy" (default) or "retain" — controls
                the Cognito pool's CloudFormation DeletionPolicy
            - efs: Studio_EFS sub-block
              - removal_policy: "destroy" (default) or "retain" — controls
                the Studio EFS file system's CloudFormation DeletionPolicy
            - studio: SageMaker Studio sub-block
              - user_profile_name_prefix: Optional prefix for per-user
                profile names, or None to use the Cognito username verbatim
                (default: None)
        """
        default_config: dict[str, Any] = {
            "enabled": False,
            "hyperpod": {"enabled": False},
            "canvas": {"enabled": False},
            "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
            "efs": {"removal_policy": "destroy"},
            "studio": {"user_profile_name_prefix": None},
        }
        analytics_ctx = self.app.node.try_get_context("analytics_environment")
        analytics_config: dict[str, Any] = analytics_ctx if isinstance(analytics_ctx, dict) else {}
        merged_config: dict[str, Any] = {**default_config, **analytics_config}

        # Deep-merge each nested sub-block so a partial override does not
        # drop the other defaults in the same sub-block.
        for sub_block in ("hyperpod", "canvas", "cognito", "efs", "studio"):
            override = analytics_config.get(sub_block)
            if isinstance(override, dict):
                default_sub = cast(dict[str, Any], default_config[sub_block])
                merged_config[sub_block] = {**default_sub, **override}

        return merged_config

    def get_analytics_enabled(self) -> bool:
        """Return whether the analytics environment stack is enabled.

        Thin wrapper around ``get_analytics_config()["enabled"]`` to mirror
        the existing ``get_valkey_config`` / ``get_aurora_pgvector_config``
        access pattern without forcing every call site to index into the
        merged dict.
        """
        return bool(self.get_analytics_config()["enabled"])

    def get_cluster_observability_config(self) -> dict[str, Any]:
        """Get the in-cluster observability configuration.

        Returns the fully-merged cluster_observability block from cdk.json
        layered on top of the defaults below. Sub-blocks (``grafana``,
        ``prometheus``, ``alertmanager``) are deep-merged so a user who
        overrides a single nested key (e.g. ``prometheus.retention``) does not
        inadvertently wipe the sub-block's other defaults — mirroring the
        nested-merge pattern used by ``get_analytics_config``.

        Unlike most optional features, this one is **on by default**: a stock
        deployment installs kube-prometheus-stack on every regional cluster.
        Operators opt out by setting ``cluster_observability.enabled = false``.

        Returns:
            Cluster observability configuration dictionary with the keys:
            - enabled: Whether kube-prometheus-stack is installed per region
              (default: True)
            - grafana: Grafana sub-block
              - persistence_size: EBS PVC size for Grafana's user database
                and dashboards (default: "10Gi")
              - admin_user: Grafana admin username; the password is
                chart-generated in the <release>-grafana Secret, never
                authored here (default: "admin")
              - admin_password_rotation_schedule: 5-field cron for the
                in-cluster CronJob that rotates the chart-generated admin
                password (default: "0 4 1 * *", monthly)
            - prometheus: Prometheus sub-block
              - persistence_size: EBS PVC size for the Prometheus TSDB
                (default: "50Gi")
              - retention: Prometheus retention window (default: "15d")
            - alertmanager: Alertmanager sub-block
              - enabled: Whether Alertmanager is deployed (default: True)
              - persistence_size: EBS PVC size for Alertmanager (default: "5Gi")
            - mlflow: MLflow experiment-tracking sub-block
              - enabled: Whether the MLflow tracking server is installed per
                region (default: True). Effective only while observability
                itself is enabled — see ``get_mlflow_enabled``.
              - persistence_size: EBS PVC size for the tracking server's
                SQLite run-metadata store (default: "10Gi"); artifacts go
                to S3, not this volume
        """
        default_config: dict[str, Any] = {
            "enabled": True,
            "grafana": {
                "persistence_size": "10Gi",
                "admin_user": "admin",
                # Monthly (04:00 on the 1st) rotation of the chart-generated
                # Grafana admin password, run by an in-cluster CronJob.
                "admin_password_rotation_schedule": "0 4 1 * *",  # nosec B105  # cron schedule, not a password
            },
            "prometheus": {"persistence_size": "50Gi", "retention": "15d"},
            "alertmanager": {"enabled": True, "persistence_size": "5Gi"},
            "mlflow": {"enabled": True, "persistence_size": "10Gi"},
        }
        obs_ctx = self.app.node.try_get_context("cluster_observability")
        obs_config: dict[str, Any] = obs_ctx if isinstance(obs_ctx, dict) else {}
        merged_config: dict[str, Any] = {**default_config, **obs_config}

        # Deep-merge each nested sub-block so a partial override does not
        # drop the other defaults in the same sub-block.
        for sub_block in ("grafana", "prometheus", "alertmanager", "mlflow"):
            override = obs_config.get(sub_block)
            if isinstance(override, dict):
                default_sub = cast(dict[str, Any], default_config[sub_block])
                merged_config[sub_block] = {**default_sub, **override}

        # Resolved before the validators run (the Slurm check asks whether
        # observability is on), so the mlflow sub-block's shape is checked
        # here rather than read through a scalar.
        mlflow = {
            **cast(dict[str, Any], default_config["mlflow"]),
            **_object_or_empty("cluster_observability.mlflow", obs_config.get("mlflow")),
        }
        mlflow["enabled"] = self._parse_mlflow_enabled(mlflow["enabled"])
        merged_config["mlflow"] = mlflow
        return merged_config

    def _parse_mlflow_enabled(self, value: object) -> bool:
        """Parse ``cluster_observability.mlflow.enabled`` (see :func:`parse_block_flag`)."""
        return parse_block_flag(
            value,
            "cluster_observability.mlflow.enabled",
            self._warn,
            off_deletes="the MLflow tracking server and its run-metadata volume",
        )

    def get_cluster_observability_enabled(self) -> bool:
        """Return whether in-cluster observability is enabled (default True).

        Thin wrapper around ``get_cluster_observability_config()["enabled"]``
        so call sites (the regional stack's chart-enable and value-override
        methods, the CLI) do not have to index into the merged dict.
        """
        return bool(self.get_cluster_observability_config()["enabled"])

    def get_mlflow_enabled(self) -> bool:
        """Return whether the MLflow tracking server is effectively enabled.

        The conjunction of ``cluster_observability.mlflow.enabled`` (default
        True) and ``cluster_observability.enabled`` (default True): MLflow
        installs into the ``monitoring`` namespace kube-prometheus-stack
        creates, stores run metadata on the observability gp3 StorageClass,
        and is reached through the same tunnel commands, so disabling
        observability switches the tracking server off with it rather than
        deploying it against missing storage — the same conjunction shape
        ``get_cost_monitoring_enabled`` uses for OpenCost.
        """
        obs = self.get_cluster_observability_config()
        return bool(obs["mlflow"]["enabled"]) and bool(obs["enabled"])

    def get_cost_monitoring_config(self) -> dict[str, Any]:
        """Get the cost monitoring configuration.

        Returns the fully-merged ``cost_monitoring`` block from cdk.json
        layered on top of the defaults below. Sub-blocks (``reports``,
        ``athena``) are deep-merged so a user who overrides a single nested
        key does not inadvertently wipe the sub-block's other defaults —
        mirroring the nested-merge pattern used by
        ``get_cluster_observability_config``.

        Like cluster observability, cost monitoring is **on by default**: a
        stock deployment installs OpenCost per region, provisions the cost
        report bucket + Athena analytics in the monitoring stack, and runs
        the cost-monitor service on every regional cluster. Operators opt out
        by setting ``cost_monitoring.enabled = false``.

        Returns:
            Cost monitoring configuration dictionary with the keys:
            - enabled: Whether the cost monitoring pipeline is deployed
              (default: True). Requires ``cluster_observability.enabled``.
            - reports: Cost report sub-block
              - interval_minutes: cadence of the cost-monitor service's
                scheduled Parquet reports (default: 60)
              - retention_days: S3 lifecycle expiration for report objects
                (default: 365)
              - transition_to_infrequent_access_days: S3 lifecycle transition
                to STANDARD_IA (default: 90; must be < retention_days)
            - athena: Athena analytics sub-block
              - query_results_retention_days: S3 lifecycle expiration for
                Athena query results written under ``athena-results/``
                (default: 30)
        """
        default_config: dict[str, Any] = {
            "enabled": True,
            "reports": {
                "interval_minutes": 60,
                "retention_days": 365,
                "transition_to_infrequent_access_days": 90,
            },
            "athena": {
                "query_results_retention_days": 30,
            },
        }
        cost_ctx = self.app.node.try_get_context("cost_monitoring")
        cost_config: dict[str, Any] = cost_ctx if isinstance(cost_ctx, dict) else {}
        merged_config: dict[str, Any] = {**default_config, **cost_config}

        # Deep-merge each nested sub-block so a partial override does not
        # drop the other defaults in the same sub-block.
        for sub_block in ("reports", "athena"):
            override = cost_config.get(sub_block)
            if isinstance(override, dict):
                default_sub = cast(dict[str, Any], default_config[sub_block])
                merged_config[sub_block] = {**default_sub, **override}

        return merged_config

    def get_cost_monitoring_enabled(self) -> bool:
        """Return whether the cost monitoring pipeline is effectively enabled.

        The conjunction of ``cost_monitoring.enabled`` (default True) and
        ``cluster_observability.enabled`` (default True): OpenCost reads its
        usage data from the in-cluster Prometheus, so disabling observability
        switches the whole cost pipeline off with it rather than deploying a
        pipeline with no data source (or failing synthesis for a user who
        only ran ``gco monitoring disable``). Call sites — the regional
        stack's chart-enable and image-build methods, the monitoring stack,
        the CLI — all gate on this one conjunction.
        """
        return bool(self.get_cost_monitoring_config()["enabled"]) and bool(
            self.get_cluster_observability_config()["enabled"]
        )

    def _validate_tracing_config(self) -> None:
        """Validate the optional ``tracing`` block and ``tracing_overrides``.

        Resolving the block is the validation: unknown keys, non-boolean
        flags, a ``sample_ratio`` outside ``[0, 1]`` and malformed overrides
        all raise :class:`ConfigValidationError` at synth instead of shipping
        a value the services would silently replace with their defaults.
        """
        self.get_tracing_config()

    def get_tracing_config(self) -> dict[str, Any]:
        """Return the validated ``tracing`` block with defaults merged in.

        Keys (see :data:`TRACING_DEFAULTS`):
            - enabled: render ``GCO_TRACING_ENABLED=true`` into the
              health-monitor, manifest-processor, inference-proxy and
              cost-monitor Deployments and grant their roles the X-Ray span
              export actions (default True)
            - sample_ratio: head-sampling probability of a new trace, a float
              in ``[0, 1]`` (default 0.05)
            - enable_transaction_search: let each regional stack switch on
              CloudWatch Transaction Search in its Region (default True;
              effective only while ``enabled`` — see
              :meth:`get_transaction_search_enabled`)

        Honors the run-scoped ``tracing_overrides`` context (a JSON object
        deep-merged over the block), which the live release harness uses to
        sample every trace of its run.
        """
        return resolve_tracing_config(
            self.app.node.try_get_context(TRACING_CONTEXT_KEY),
            self.app.node.try_get_context(TRACING_OVERRIDES_CONTEXT_KEY),
        )

    def get_tracing_enabled(self) -> bool:
        """Return whether the API services export traces (default True)."""
        return bool(self.get_tracing_config()["enabled"])

    def get_transaction_search_enabled(self) -> bool:
        """Return whether the regional stacks enable CloudWatch Transaction Search.

        The conjunction of ``tracing.enabled`` and
        ``tracing.enable_transaction_search``: Transaction Search only exists
        to receive GCO's spans, so a deployment with tracing off never touches
        the account-level setting.
        """
        config = self.get_tracing_config()
        return bool(config["enabled"]) and bool(config["enable_transaction_search"])

    def get_capacity_history_config(self) -> dict[str, Any]:
        """Get the optional historical capacity surface configuration.

        Returns the merged ``historical`` block from cdk.json layered on top of
        the defaults below. The feature is off unless ``historical.enabled`` is
        explicitly true, mirroring the analytics-environment opt-in pattern.

        Keys:
            - enabled: deploy the capacity poller stack + history table (default False)
            - retention_days: DynamoDB TTL window for snapshots (default 90)
            - poll_interval_minutes: EventBridge schedule cadence (default 15)
            - capacity_block_duration_hours: short Capacity Block probe duration
              the poller snapshots (default 24 = 1 day)
            - capacity_block_long_duration_hours: long Capacity Block probe
              duration in hours (default 1512 = 63 days); 0 disables the long
              probe and its ``capacity_blocks_long_*`` metrics
            - spot_score_target_capacities: Spot Placement Score target
              capacities the poller snapshots per instance pool (default
              [1, 10, 50]); a subset selector over the supported set exported
              by ``cli/capacity/history.py``, where capacity 1 keeps the
              original ``spot_score`` field and N > 1 writes ``spot_score_at_N``
            - watch_instance_types: instance types the poller snapshots
            - enabled_regions: regions to poll; empty means all deployed regions
        """
        default_config: dict[str, Any] = {
            "enabled": False,
            "retention_days": 90,
            "poll_interval_minutes": 15,
            "capacity_block_duration_hours": 24,
            "capacity_block_long_duration_hours": 63 * 24,
            "spot_score_target_capacities": [1, 10, 50],
            "watch_instance_types": [
                "g4dn.12xlarge",
                "g4dn.16xlarge",
                "g4dn.2xlarge",
                "g4dn.4xlarge",
                "g4dn.8xlarge",
                "g4dn.metal",
                "g4dn.xlarge",
                "g5.12xlarge",
                "g5.16xlarge",
                "g5.24xlarge",
                "g5.2xlarge",
                "g5.48xlarge",
                "g5.4xlarge",
                "g5.8xlarge",
                "g5.xlarge",
                "g5g.16xlarge",
                "g5g.2xlarge",
                "g5g.4xlarge",
                "g5g.8xlarge",
                "g5g.metal",
                "g5g.xlarge",
                "g6.12xlarge",
                "g6.16xlarge",
                "g6.24xlarge",
                "g6.2xlarge",
                "g6.48xlarge",
                "g6.4xlarge",
                "g6.8xlarge",
                "g6.xlarge",
                "g6e.12xlarge",
                "g6e.16xlarge",
                "g6e.24xlarge",
                "g6e.2xlarge",
                "g6e.48xlarge",
                "g6e.4xlarge",
                "g6e.8xlarge",
                "g6e.xlarge",
                "g6f.2xlarge",
                "g6f.4xlarge",
                "g6f.large",
                "g6f.xlarge",
                "g7.12xlarge",
                "g7.24xlarge",
                "g7.2xlarge",
                "g7.48xlarge",
                "g7.4xlarge",
                "g7.8xlarge",
                "g7e.12xlarge",
                "g7e.24xlarge",
                "g7e.2xlarge",
                "g7e.48xlarge",
                "g7e.4xlarge",
                "g7e.8xlarge",
                "gr6.4xlarge",
                "gr6.8xlarge",
                "gr6f.4xlarge",
                "inf1.24xlarge",
                "inf1.2xlarge",
                "inf1.6xlarge",
                "inf1.xlarge",
                "inf2.24xlarge",
                "inf2.48xlarge",
                "inf2.8xlarge",
                "inf2.xlarge",
                "p3dn.24xlarge",
                "p4d.24xlarge",
                "p4de.24xlarge",
                "p5.48xlarge",
                "p5.4xlarge",
                "p5e.48xlarge",
                "p5en.48xlarge",
                "p6-b200.48xlarge",
                "p6-b300.48xlarge",
                "trn1.2xlarge",
                "trn1.32xlarge",
                "trn1n.32xlarge",
                "trn2.3xlarge",
                "trn2.48xlarge",
            ],
            "enabled_regions": [],
        }
        historical_ctx = self.app.node.try_get_context("historical")
        historical_config = historical_ctx if isinstance(historical_ctx, dict) else {}
        return {**default_config, **historical_config}

    def get_capacity_history_enabled(self) -> bool:
        """Return whether the historical capacity surface is enabled."""
        return bool(self.get_capacity_history_config()["enabled"])

    def get_mission_memory_config(self) -> dict[str, Any]:
        """Get the mission-memory configuration (recall across mission sessions).

        Returns the merged ``mission_memory`` block from cdk.json layered on
        top of the defaults below. The feature is ON by default — memory is
        cheap (one small PAY_PER_REQUEST item plus one embedding call per
        completed mission) and silently missing recall is the worse failure
        mode; set ``mission_memory.enabled: false`` to opt out.

        Keys:
            - enabled: provision the mission-memory table + vector index and
              activate best-effort write/retrieval in the engine (default True)
            - retention_days: DynamoDB TTL window for memory items (default 365)
            - dimensions: embedding vector width (default 1024). ONE-WAY DOOR:
              immutable after index creation and must match the configured
              ``bedrock.embedding_model_id`` output width.
            - distance_function: vector distance metric (default COSINE);
              immutable after index creation.
            - top_k: similar past missions retrieved into the sampling prompt
              (default 3)
        """
        default_config: dict[str, Any] = {
            "enabled": True,
            "retention_days": 365,
            "dimensions": 1024,
            "distance_function": "COSINE",
            "top_k": 3,
        }
        mission_memory_ctx = self.app.node.try_get_context("mission_memory")
        mission_memory_config = mission_memory_ctx if isinstance(mission_memory_ctx, dict) else {}
        return {**default_config, **mission_memory_config}

    def get_mission_memory_enabled(self) -> bool:
        """Return whether mission memory is enabled."""
        return bool(self.get_mission_memory_config()["enabled"])

    def get_vector_store_config(self) -> dict[str, Any]:
        """Get the vector-store configuration (global workload RAG corpus).

        Returns the merged ``vector_store`` block from cdk.json layered on
        top of the defaults below. The feature is OFF by default — a
        replicated vector store carries real per-region storage and write
        cost, so it is an explicit opt-in like ``aurora_pgvector``; set
        ``vector_store.enabled: true`` to provision it.

        Keys:
            - enabled: provision the global vector-store table + index,
              the S3-triggered ingest pipeline, and the regional read wiring
              (default False)
            - dimensions: embedding vector width (default 1024). ONE-WAY
              DOOR: immutable after index creation and must match the
              configured ``embedding_model_id`` output width.
            - distance_function: vector distance metric (default COSINE);
              immutable after index creation.
            - embedding_model_id: Bedrock text-embedding model used by the
              ingest pipeline and query paths (default
              amazon.titan-embed-text-v2:0). Independent of
              ``bedrock.embedding_model_id`` on purpose. Changing it means
              re-ingesting the corpus: vectors from different models are
              not comparable.
            - replica_regions: regions to replicate the table into. Empty
              (the default) means "follow deployment_regions.regional",
              excluding the global region (the primary).
            - corpus_prefix: S3 key prefix on the Cluster_Shared_Bucket
              watched by the ingest pipeline (default vector-corpus/).
        """
        default_config: dict[str, Any] = {
            "enabled": False,
            "dimensions": 1024,
            "distance_function": "COSINE",
            "embedding_model_id": "amazon.titan-embed-text-v2:0",
            "replica_regions": [],
            "corpus_prefix": "vector-corpus/",
        }
        vector_store_ctx = self.app.node.try_get_context("vector_store")
        vector_store_config = vector_store_ctx if isinstance(vector_store_ctx, dict) else {}
        merged = {**default_config, **vector_store_config}
        # The validator checks the stripped id, so every consumer (IAM
        # resources, the ingest Lambda's environment, the job config) gets
        # the same stripped value, as gco.bedrock does for its own ids.
        merged["embedding_model_id"] = str(merged["embedding_model_id"]).strip()
        if self._feature_override_enabled("vector_store"):
            merged["enabled"] = True
        return merged

    def get_vector_store_enabled(self) -> bool:
        """Return whether the vector store is enabled (default False)."""
        return bool(self.get_vector_store_config()["enabled"])

    def get_vector_store_replica_regions(self) -> list[str]:
        """Return the effective replica region list for the vector store.

        The configured ``replica_regions`` when non-empty, otherwise the
        regional deployment list — in both cases with the global region
        removed, because the primary table lives there and a global table
        cannot replicate into its own region. May legitimately be empty
        (single-region deployments get a single-region global table).
        """
        config = self.get_vector_store_config()
        configured = [str(region) for region in config["replica_regions"]]
        candidates = configured or self.get_regions()
        global_region = self.get_global_region()
        return [region for region in candidates if region != global_region]

    def get_tags(self) -> dict[str, str]:
        """Get common tags from configuration"""
        return self.app.node.try_get_context("tags") or {}

    def validate_region_availability(self, region: str) -> bool:
        """Validate that a region is available in the current AWS account"""
        try:
            ec2 = boto3.client("ec2", region_name=region)
            ec2.describe_regions(RegionNames=[region])
            return True
        except Exception as e:
            logger.debug("Region %s not available: %s", region, e)
            return False

    def get_available_regions(self) -> list[str]:
        """Get list of available AWS regions for the current account"""
        try:
            ec2 = boto3.client("ec2")
            response = ec2.describe_regions()
            return [region["RegionName"] for region in response["Regions"]]
        except Exception as e:
            logger.debug("Failed to list regions, using defaults: %s", e)
            return list(self.VALID_REGIONS)
