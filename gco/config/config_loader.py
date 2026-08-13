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
- alb_config: Application Load Balancer health check settings
- manifest_processor: Manifest validation and resource limits
- api_gateway: Throttling and logging configuration
- tags: Common tags applied to all resources

Usage:
    config = ConfigLoader(app)
    regions = config.get_regions()
    cluster_config = config.get_cluster_config("us-east-1")
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import boto3
from aws_cdk import App

from gco.manifest_security_policy import validate_manifest_security_policy
from gco.models import ClusterConfig, ResourceThresholds
from gco.stacks.constants import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    known_cloudformation_regions,
    validated_deployment_partition,
    validated_regional_deployment_regions,
    validated_request_body_limit,
)

logger = logging.getLogger(__name__)

#: CDK context key that force-enables optional infrastructure features for one
#: deploy without touching cdk.json — the infrastructure sibling of the
#: ``helm_enabled_overrides`` context handled in ``gco/stacks/regional_stack.py``.
#: Used by validation harnesses (``gco examples validate``) whose preflight
#: requires a clean worktree.
FEATURE_OVERRIDE_CONTEXT_KEY = "feature_enabled_overrides"

#: The cdk.json blocks whose ``enabled`` flag the override may force on. Kept
#: deliberately narrow: each of these is a self-contained regional feature the
#: examples exercise (Aurora pgvector, Valkey Serverless, FSx for Lustre).
FEATURE_OVERRIDE_KEYS = frozenset({"aurora_pgvector", "valkey", "fsx_lustre"})


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


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    pass


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
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        """Validate the entire configuration"""
        # Check if we have any context at all (might be running outside CDK)
        project_name = self.app.node.try_get_context("project_name")
        if project_name is None:
            # Running outside CDK context, skip validation
            return

        # Validate required fields exist
        required_fields = [
            "project_name",
            "kubernetes_version",
            "resource_thresholds",
        ]
        for field in required_fields:
            if not self.app.node.try_get_context(field):
                raise ConfigValidationError(f"Required configuration field '{field}' is missing")

        # Validate project_name format before anything consumes it (#139).
        self._validate_project_name()

        # Check for deployment_regions
        deployment_regions = self.app.node.try_get_context("deployment_regions")
        if not isinstance(deployment_regions, dict) or not deployment_regions:
            raise ConfigValidationError(
                "Required configuration field 'deployment_regions' must be a non-empty object"
            )

        # Validate regions
        self._validate_regions()

        # Validate resource thresholds
        self._validate_resource_thresholds()

        # Validate Global Accelerator config
        self._validate_global_accelerator_config()

        # Validate deployment-local backend TLS rotation policy
        self._validate_backend_tls_config()

        # Validate ALB config
        self._validate_alb_config()

        # Validate manifest processor config
        self._validate_manifest_processor_config()

        # Validate API Gateway config
        self._validate_api_gateway_config()

        # Validate EKS cluster config
        self._validate_eks_cluster_config()

        # Validate analytics environment config (optional block)
        self._validate_analytics_environment_config()

        # Validate cluster observability config (optional block)
        self._validate_cluster_observability_config()

        # Validate cost monitoring config (optional block)
        self._validate_cost_monitoring_config()

        # Validate historical capacity surface config (optional block)
        self._validate_capacity_history_config()

        # Validate mission-memory configuration (recall across mission sessions)
        self._validate_mission_memory_config()
        # Validate the vector-store configuration (global workload RAG corpus)
        self._validate_vector_store_config()

    #: Allowed ``project_name`` format (#139). ``project_name`` is the
    #: deployment's unique prefix and flows into S3 bucket names, the Cognito
    #: hosted-UI domain prefix, SSM parameter paths, IAM role names, and
    #: CloudFormation export names. The tightest of those constraints is S3 /
    #: Cognito naming (lowercase letters, digits, hyphens; must start with a
    #: letter), so require: a leading lowercase letter followed by 1–30
    #: lowercase letters, digits, or hyphens (total length 2–31).
    PROJECT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

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
        if not isinstance(project_name, str) or not self.PROJECT_NAME_PATTERN.match(project_name):
            raise ConfigValidationError(
                f"Invalid project_name {project_name!r}. It must match "
                f"{self.PROJECT_NAME_PATTERN.pattern} (start with a lowercase letter, then "
                "2–31 total characters of lowercase letters, digits, or hyphens). "
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
                        f"Invalid {field} region {region!r}; expected an AWS region with a "
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

    def _validate_resource_thresholds(self) -> None:
        """Validate resource threshold configuration"""
        thresholds_config = self.app.node.try_get_context("resource_thresholds")

        required_thresholds = ["cpu_threshold", "memory_threshold", "gpu_threshold"]
        for threshold in required_thresholds:
            if threshold not in thresholds_config:
                raise ConfigValidationError(f"Missing threshold configuration: {threshold}")

            value = thresholds_config[threshold]
            if not isinstance(value, int) or (value != -1 and not 0 <= value <= 100):
                raise ConfigValidationError(
                    f"{threshold} must be an integer between 0 and 100 (or -1 to disable), got {value}"
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
                if not isinstance(value, int) or (value != -1 and value < 0):
                    raise ConfigValidationError(
                        f"{opt_threshold} must be a non-negative integer (or -1 to disable), got {value}"
                    )

    def _validate_global_accelerator_config(self) -> None:
        """Validate Global Accelerator configuration"""
        ga_config = self.app.node.try_get_context("global_accelerator")
        if not ga_config:
            raise ConfigValidationError("global_accelerator configuration is required")

        # ``name`` is intentionally optional: when omitted it defaults to
        # ``<project_name>-accelerator`` (see get_global_accelerator_config /
        # GCOGlobalStack) so a second deployment gets a project-scoped name
        # from the single ``project_name`` knob (#139).
        required_fields = [
            "health_check_grace_period",
            "health_check_interval",
            "health_check_timeout",
            "health_check_path",
        ]
        for field in required_fields:
            if field not in ga_config:
                raise ConfigValidationError(f"Missing global_accelerator configuration: {field}")

        # Validate timing values
        for field in ["health_check_grace_period", "health_check_interval", "health_check_timeout"]:
            value = ga_config[field]
            if not isinstance(value, int) or value <= 0:
                raise ConfigValidationError(f"{field} must be a positive integer, got {value}")

        # Validate health check path
        if not ga_config["health_check_path"].startswith("/"):
            raise ConfigValidationError("health_check_path must start with '/'")

        # Validate optional client affinity. Omitting the key is allowed and
        # defaults to "NONE" in get_global_accelerator_config().
        if "client_affinity" in ga_config:
            allowed_affinity = {"NONE", "SOURCE_IP"}
            value = ga_config["client_affinity"]
            if not isinstance(value, str) or value.upper() not in allowed_affinity:
                raise ConfigValidationError(
                    f"client_affinity must be one of {sorted(allowed_affinity)}, got {value!r}"
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

    def _validate_alb_config(self) -> None:
        """Validate ALB configuration"""
        alb_config = self.app.node.try_get_context("alb_config")
        if not alb_config:
            raise ConfigValidationError("alb_config configuration is required")

        required_fields = [
            "health_check_interval",
            "health_check_timeout",
            "healthy_threshold",
            "unhealthy_threshold",
        ]
        for field in required_fields:
            if field not in alb_config:
                raise ConfigValidationError(f"Missing alb_config configuration: {field}")

            value = alb_config[field]
            if not isinstance(value, int) or value <= 0:
                raise ConfigValidationError(f"{field} must be a positive integer, got {value}")

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

        required_fields = [
            "image",
            "replicas",
            "resource_limits",
        ]
        for field in required_fields:
            if field not in mp_config:
                raise ConfigValidationError(f"Missing manifest_processor configuration: {field}")

        # Validate replicas
        if not isinstance(mp_config["replicas"], int) or mp_config["replicas"] <= 0:
            raise ConfigValidationError("manifest_processor replicas must be a positive integer")

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
        if not isinstance(policy, dict):
            raise ConfigValidationError("job_validation_policy must be an object")
        for policy_field in ("allowed_namespaces", "resource_quotas"):
            if policy_field not in policy:
                raise ConfigValidationError(
                    f"Missing job_validation_policy configuration: {policy_field}"
                )

        try:
            validate_manifest_security_policy(policy.get("manifest_security_policy", {}))
        except ValueError as exc:
            raise ConfigValidationError(f"job_validation_policy.{exc}") from exc

        require_toleration = policy.get("require_accelerator_toleration", True)
        if type(require_toleration) is not bool:
            raise ConfigValidationError(
                "job_validation_policy.require_accelerator_toleration must be a boolean"
            )

        # Validate resource limits
        resource_limits = mp_config["resource_limits"]
        if "cpu" not in resource_limits or "memory" not in resource_limits:
            raise ConfigValidationError(
                "manifest_processor resource_limits must contain 'cpu' and 'memory'"
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
                raise ConfigValidationError(f"Missing api_gateway configuration: {field}")

        # Validate throttle limits
        throttle_rate = api_gw_config["throttle_rate_limit"]
        throttle_burst = api_gw_config["throttle_burst_limit"]

        if not isinstance(throttle_rate, int) or throttle_rate <= 0:
            raise ConfigValidationError(
                f"throttle_rate_limit must be a positive integer, got {throttle_rate}"
            )

        if not isinstance(throttle_burst, int) or throttle_burst <= 0:
            raise ConfigValidationError(
                f"throttle_burst_limit must be a positive integer, got {throttle_burst}"
            )

        if throttle_burst < throttle_rate:
            raise ConfigValidationError(
                "throttle_burst_limit should be greater than or equal to throttle_rate_limit"
            )

        # Validate log level
        valid_log_levels = ["OFF", "ERROR", "INFO"]
        log_level = api_gw_config["log_level"]
        if log_level not in valid_log_levels:
            raise ConfigValidationError(
                f"log_level must be one of {valid_log_levels}, got {log_level}"
            )

        # Validate boolean flags
        if not isinstance(api_gw_config["metrics_enabled"], bool):
            raise ConfigValidationError("metrics_enabled must be a boolean")

        if not isinstance(api_gw_config["tracing_enabled"], bool):
            raise ConfigValidationError("tracing_enabled must be a boolean")

        if "regional_api_enabled" in api_gw_config and not isinstance(
            api_gw_config["regional_api_enabled"], bool
        ):
            raise ConfigValidationError("regional_api_enabled must be a boolean")

    def _validate_eks_cluster_config(self) -> None:
        """Validate EKS cluster configuration"""
        eks_config = self.app.node.try_get_context("eks_cluster") or {}

        # Validate endpoint_access if present
        if "endpoint_access" in eks_config:
            valid_access_modes = ["PRIVATE", "PUBLIC_AND_PRIVATE"]
            if eks_config["endpoint_access"] not in valid_access_modes:
                raise ConfigValidationError(
                    f"endpoint_access must be one of {valid_access_modes}, "
                    f"got {eks_config['endpoint_access']}"
                )

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

        # Non-empty-string checks for the size / retention knobs.
        string_fields = (
            ("grafana", "persistence_size"),
            ("prometheus", "persistence_size"),
            ("prometheus", "retention"),
            ("alertmanager", "persistence_size"),
        )
        for sub_block, field in string_fields:
            sub_ctx = obs_ctx.get(sub_block)
            if not isinstance(sub_ctx, dict) or field not in sub_ctx:
                continue
            value = sub_ctx[field]
            if not isinstance(value, str) or not value.strip():
                raise ConfigValidationError(
                    f"cluster_observability.{sub_block}.{field} must be a non-empty "
                    f"string, got {value!r}"
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
            if not isinstance(model_id, str) or not model_id.strip():
                raise ConfigValidationError(
                    f"vector_store.embedding_model_id must be a non-empty string, got {model_id!r}"
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
        - regional: List of regions for EKS clusters (default: ["us-east-1"])

        Note: Global Accelerator is a global service but requires a "home" region
        for CloudFormation deployment. us-east-2 is used by default to keep
        global infrastructure separate from workload regions.
        """
        deployment_regions = self.app.node.try_get_context("deployment_regions") or {}

        return {
            "global": deployment_regions.get("global", "us-east-2"),
            "api_gateway": deployment_regions.get("api_gateway", "us-east-2"),
            "monitoring": deployment_regions.get("monitoring", "us-east-2"),
            "regional": deployment_regions.get("regional", ["us-east-1"]),
        }

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
        """Get list of regions for EKS cluster deployment."""
        deployment_regions = self.get_deployment_regions()
        regional = deployment_regions["regional"]
        return list(regional) if isinstance(regional, list) else [str(regional)]

    def get_kubernetes_version(self) -> str:
        """Get Kubernetes version from configuration"""
        return self.app.node.try_get_context("kubernetes_version") or "1.36"

    def get_resource_thresholds(self) -> ResourceThresholds:
        """Get resource thresholds configuration"""
        thresholds_config = self.app.node.try_get_context("resource_thresholds") or {
            "cpu_threshold": 80,
            "memory_threshold": 80,
            "gpu_threshold": -1,
            "pending_pods_threshold": 10,
            "pending_requested_cpu_vcpus": 100,
            "pending_requested_memory_gb": 200,
            "pending_requested_gpus": -1,
        }
        return ResourceThresholds(
            cpu_threshold=thresholds_config["cpu_threshold"],
            memory_threshold=thresholds_config["memory_threshold"],
            gpu_threshold=thresholds_config["gpu_threshold"],
            pending_pods_threshold=thresholds_config.get("pending_pods_threshold", 10),
            pending_requested_cpu_vcpus=thresholds_config.get("pending_requested_cpu_vcpus", 100),
            pending_requested_memory_gb=thresholds_config.get("pending_requested_memory_gb", 200),
            pending_requested_gpus=thresholds_config.get("pending_requested_gpus", 8),
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
        """Get Global Accelerator configuration"""
        return self.app.node.try_get_context("global_accelerator") or {
            "name": f"{self.get_project_name()}-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
            "client_affinity": "NONE",
        }

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

    def get_alb_config(self) -> dict[str, Any]:
        """Get ALB configuration"""
        return self.app.node.try_get_context("alb_config") or {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        }

    def get_manifest_processor_config(self) -> dict[str, Any]:
        """Get manifest processor configuration.

        Merges three cdk.json sections into a single runtime config:

        - ``manifest_processor``: service-specific settings (replicas, image,
          resource_limits, allowed_namespaces, validation_enabled,
          max_request_body_bytes, yaml_max_depth)
        - ``job_validation_policy``: shared validation policy (resource_quotas,
          trusted_registries, trusted_dockerhub_orgs, manifest_security_policy,
          allowed_kinds). Pulled in verbatim so the REST path reads the same
          policy the SQS queue processor enforces.

        Note: The 'image' field is a placeholder default. In practice, the actual
        image is built from dockerfiles/manifest-processor-dockerfile and pushed
        to ECR during CDK deployment. The {{MANIFEST_PROCESSOR_IMAGE}} placeholder
        in manifests is replaced with the ECR image URI.
        """
        default_config = {
            "image": "gco/manifest-processor:latest",  # Placeholder, replaced by ECR image
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
            "validation_enabled": True,
            "max_request_body_bytes": DEFAULT_MAX_REQUEST_BODY_BYTES,
            "central_queue_worker_enabled": True,
            "central_queue_poll_interval_seconds": 10,
            "central_queue_batch_size": 5,
            "central_queue_reconcile_limit": 100,
            "central_queue_lease_seconds": 300,
            "central_queue_lease_renewal_seconds": 60,
            # allowed_namespaces, resource_quotas, trusted_registries,
            # trusted_dockerhub_orgs, manifest_security_policy, and
            # allowed_kinds are merged in below from job_validation_policy.
            "allowed_namespaces": ["gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
            "trusted_registries": [
                "docker.io",
                "gcr.io",
                "quay.io",
                "registry.k8s.io",
                "k8s.gcr.io",
                "public.ecr.aws",
                "nvcr.io",
                "gco",
            ],
            "trusted_dockerhub_orgs": [
                "nvidia",
                "pytorch",
                "rayproject",
                "tensorflow",
                "huggingface",
                "amazon",
                "bitnami",
            ],
        }
        context_config = self.app.node.try_get_context("manifest_processor") or {}

        # Merge in the shared job_validation_policy section. These keys apply
        # to BOTH the manifest processor and the queue processor; they live
        # in their own top-level cdk.json section so neither service "owns"
        # them. We flatten them into the manifest processor's runtime config
        # so service code keeps its existing attribute layout.
        shared_policy = self.app.node.try_get_context("job_validation_policy") or {}
        merged = {**default_config, **context_config, **shared_policy}

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

        Note:
            PRIVATE endpoint is recommended for production. Job submission still works
            via API Gateway → Lambda (in VPC) or SQS queues. For kubectl access with
            PRIVATE endpoint, use a bastion host, VPN, or AWS SSM Session Manager.
        """
        default_config = {
            "endpoint_access": "PRIVATE",
        }
        return {**default_config, **(self.app.node.try_get_context("eks_cluster") or {})}

    def get_fsx_lustre_config(self, region: str | None = None) -> dict[str, Any]:
        """Get FSx for Lustre configuration.

        Args:
            region: Optional region to get config for. If provided, checks for
                    region-specific overrides first.

        Returns:
            FSx configuration dictionary with the following keys:
            - enabled: Whether FSx is enabled
            - storage_capacity_gib: Storage capacity in GiB (min 1200)
            - deployment_type: SCRATCH_1, SCRATCH_2, PERSISTENT_1, PERSISTENT_2
            - file_system_type_version: Lustre version (2.12 or 2.15, default: 2.15)
              IMPORTANT: Use 2.15 for kernel 6.x compatibility (AL2023, Bottlerocket)
            - per_unit_storage_throughput: Throughput for PERSISTENT types
            - data_compression_type: LZ4 or NONE
            - import_path: S3 path for data import
            - export_path: S3 path for data export
            - auto_import_policy: NEW, NEW_CHANGED, NEW_CHANGED_DELETED
            - node_group: Node group configuration for FSx workloads
              - instance_types: List of instance types
              - min_size: Minimum nodes (default: 0)
              - max_size: Maximum nodes (default: 10)
              - desired_size: Desired nodes (default: 0, scales from zero)
              - ami_type: AMI type - one of:
                  AL2023_X86_64_STANDARD (default), AL2023_ARM_64_STANDARD,
                  AL2023_X86_64_NVIDIA, AL2023_ARM_64_NVIDIA, AL2023_X86_64_NEURON
              - capacity_type: ON_DEMAND (default) or SPOT
              - disk_size: Root disk size in GB (default: 100)
              - labels: Additional node labels (dict)
        """
        default_config = {
            "enabled": False,
            "storage_capacity_gib": 1200,
            "deployment_type": "SCRATCH_2",
            "file_system_type_version": "2.15",  # Use 2.15 for kernel 6.x compatibility
            "per_unit_storage_throughput": 200,
            "data_compression_type": "LZ4",
            "import_path": None,
            "export_path": None,
            "auto_import_policy": "NEW_CHANGED_DELETED",
            "node_group": {
                "instance_types": ["m5.large", "m5.xlarge", "m6i.large", "m6i.xlarge"],
                "min_size": 0,
                "max_size": 10,
                "desired_size": 1,
                "ami_type": "AL2023_X86_64_STANDARD",
                "capacity_type": "ON_DEMAND",
                "disk_size": 100,
                "labels": {},
            },
        }

        # Get global FSx config
        global_ctx = self.app.node.try_get_context("fsx_lustre")
        global_config: dict[str, Any] = global_ctx if isinstance(global_ctx, dict) else {}
        merged_config: dict[str, Any] = {**default_config, **global_config}

        # Ensure node_group has all required fields with defaults
        if "node_group" in global_config:
            global_node_group = global_config["node_group"]
            if isinstance(global_node_group, dict):
                default_node_group = cast(dict[str, Any], default_config["node_group"])
                merged_config["node_group"] = {
                    **default_node_group,
                    **global_node_group,
                }

        # Check for region-specific override
        if region:
            region_overrides_ctx = self.app.node.try_get_context("fsx_lustre_regions")
            region_overrides: dict[str, Any] = (
                region_overrides_ctx if isinstance(region_overrides_ctx, dict) else {}
            )
            if region in region_overrides:
                region_config = region_overrides[region]
                if isinstance(region_config, dict):
                    merged_config = {**merged_config, **region_config}
                    # Handle nested node_group override
                    if "node_group" in region_config:
                        region_node_group = region_config["node_group"]
                        if isinstance(region_node_group, dict):
                            existing_node_group = merged_config.get("node_group")
                            if isinstance(existing_node_group, dict):
                                base_node_group = existing_node_group
                            else:
                                base_node_group = cast(dict[str, Any], default_config["node_group"])
                            merged_config["node_group"] = {
                                **base_node_group,
                                **region_node_group,
                            }

        if self._feature_override_enabled("fsx_lustre"):
            merged_config["enabled"] = True
        return merged_config

    def _feature_override_enabled(self, feature_key: str) -> bool:
        """True when ``feature_enabled_overrides`` context forces this feature on."""
        overrides = parse_feature_enabled_overrides(
            self.app.node.try_get_context(FEATURE_OVERRIDE_CONTEXT_KEY)
        )
        return feature_key in overrides

    def get_valkey_config(self) -> dict[str, Any]:
        """Get Valkey Serverless cache configuration.

        Returns:
            Valkey configuration dictionary with the following keys:
            - enabled: Whether Valkey cache is enabled (default: False)
            - max_data_storage_gb: Maximum data storage in GB (default: 5)
            - max_ecpu_per_second: Maximum ECPUs per second (default: 5000)
            - snapshot_retention_limit: Daily snapshots to retain (default: 1)
        """
        default_config: dict[str, Any] = {
            "enabled": False,
            "max_data_storage_gb": 5,
            "max_ecpu_per_second": 5000,
            "snapshot_retention_limit": 1,
        }
        valkey_ctx = self.app.node.try_get_context("valkey")
        valkey_config: dict[str, Any] = valkey_ctx if isinstance(valkey_ctx, dict) else {}
        merged = {**default_config, **valkey_config}
        if self._feature_override_enabled("valkey"):
            merged["enabled"] = True
        return merged

    def get_aurora_pgvector_config(self) -> dict[str, Any]:
        """Get Aurora Serverless v2 + pgvector vector database configuration.

        Returns:
            Aurora pgvector configuration dictionary with the following keys:
            - enabled: Whether Aurora pgvector is enabled (default: False)
            - min_acu: Minimum Aurora Capacity Units (default: 0, scales to zero)
            - max_acu: Maximum Aurora Capacity Units (default: 16)
            - backup_retention_days: Number of days to retain automated backups (default: 7)
            - deletion_protection: Whether deletion protection is enabled (default: False)
        """
        default_config: dict[str, Any] = {
            "enabled": False,
            "min_acu": 0,
            "max_acu": 16,
            "backup_retention_days": 7,
            "deletion_protection": False,
        }
        aurora_ctx = self.app.node.try_get_context("aurora_pgvector")
        aurora_config: dict[str, Any] = aurora_ctx if isinstance(aurora_ctx, dict) else {}
        merged = {**default_config, **aurora_config}
        if self._feature_override_enabled("aurora_pgvector"):
            merged["enabled"] = True
        return merged

    def get_analytics_config(self) -> dict[str, Any]:
        """Get optional analytics environment configuration.

        Returns the fully-merged analytics_environment block from cdk.json
        layered on top of the defaults below. Sub-blocks (``hyperpod``,
        ``cognito``, ``efs``, ``studio``) are deep-merged so a user who
        overrides a single nested key (e.g. ``cognito.domain_prefix``) does
        not inadvertently wipe the sub-block's other defaults — mirroring the
        nested-merge pattern used by ``get_fsx_lustre_config`` for its
        ``node_group`` sub-block.

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
        """
        default_config: dict[str, Any] = {
            "enabled": True,
            "grafana": {
                "persistence_size": "10Gi",
                "admin_user": "admin",
                # Monthly (04:00 on the 1st) rotation of the chart-generated
                # Grafana admin password, run by an in-cluster CronJob.
                "admin_password_rotation_schedule": "0 4 1 * *",
            },
            "prometheus": {"persistence_size": "50Gi", "retention": "15d"},
            "alertmanager": {"enabled": True, "persistence_size": "5Gi"},
        }
        obs_ctx = self.app.node.try_get_context("cluster_observability")
        obs_config: dict[str, Any] = obs_ctx if isinstance(obs_ctx, dict) else {}
        merged_config: dict[str, Any] = {**default_config, **obs_config}

        # Deep-merge each nested sub-block so a partial override does not
        # drop the other defaults in the same sub-block.
        for sub_block in ("grafana", "prometheus", "alertmanager"):
            override = obs_config.get(sub_block)
            if isinstance(override, dict):
                default_sub = cast(dict[str, Any], default_config[sub_block])
                merged_config[sub_block] = {**default_sub, **override}

        return merged_config

    def get_cluster_observability_enabled(self) -> bool:
        """Return whether in-cluster observability is enabled (default True).

        Thin wrapper around ``get_cluster_observability_config()["enabled"]``
        so call sites (the regional stack's chart-enable and value-override
        methods, the CLI) do not have to index into the merged dict.
        """
        return bool(self.get_cluster_observability_config()["enabled"])

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
        return {**default_config, **vector_store_config}

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
