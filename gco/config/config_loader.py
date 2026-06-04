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
from typing import Any, cast

import boto3
from aws_cdk import App

from gco.models import ClusterConfig, ResourceThresholds

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    pass


class ConfigLoader:
    """
    Loads and validates configuration from CDK context (cdk.json)
    """

    # Valid AWS regions (subset of commonly used regions)
    VALID_REGIONS = {
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "eu-central-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ca-central-1",
        "sa-east-1",
    }

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

        # Check for deployment_regions
        deployment_regions = self.app.node.try_get_context("deployment_regions")
        if not deployment_regions:
            raise ConfigValidationError(
                "Required configuration field 'deployment_regions' is missing"
            )

        # Validate regions
        self._validate_regions()

        # Validate resource thresholds
        self._validate_resource_thresholds()

        # Validate Global Accelerator config
        self._validate_global_accelerator_config()

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

    def _validate_regions(self) -> None:
        """Validate region configuration"""
        regions = self.get_regions()

        if not regions:
            raise ConfigValidationError("At least one region must be specified")

        if len(regions) > 10:
            raise ConfigValidationError("Maximum of 10 regions supported")

        for region in regions:
            if region not in self.VALID_REGIONS:
                raise ConfigValidationError(
                    f"Invalid region '{region}'. Valid regions: {sorted(self.VALID_REGIONS)}"
                )

        # Check for duplicates
        if len(regions) != len(set(regions)):
            raise ConfigValidationError("Duplicate regions found in configuration")

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

        required_fields = [
            "name",
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
                raise ConfigValidationError(
                    f"Missing job_validation_policy configuration: {policy_field}"
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

    def get_global_region(self) -> str:
        """Get the region for global resources (Global Accelerator, SSM params)."""
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
            "cpu_threshold": 60,
            "memory_threshold": 60,
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
            "name": "gco-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        }

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
            # allowed_namespaces, resource_quotas, trusted_registries,
            # trusted_dockerhub_orgs, manifest_security_policy, and
            # allowed_kinds are merged in below from job_validation_policy.
            "allowed_namespaces": ["default", "gco-jobs"],
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
        return {**default_config, **context_config, **shared_policy}

    def get_api_gateway_config(self) -> dict[str, Any]:
        """Get API Gateway configuration.

        Returns:
            API Gateway configuration dictionary with the following keys:
            - throttle_rate_limit: Requests per second limit
            - throttle_burst_limit: Burst capacity
            - log_level: CloudWatch logging level (OFF, ERROR, INFO)
            - metrics_enabled: Enable CloudWatch metrics
            - tracing_enabled: Enable X-Ray tracing
            - regional_api_enabled: Enable regional API Gateways for private access
              When true, deploys a regional API Gateway with VPC Lambda in each
              region, allowing API access when the ALB is internal-only.
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

        return merged_config

    def get_valkey_config(self) -> dict[str, Any]:
        """Get Valkey Serverless cache configuration.

        Returns:
            Valkey configuration dictionary with the following keys:
            - enabled: Whether Valkey cache is enabled (default: True)
            - max_data_storage_gb: Maximum data storage in GB (default: 5)
            - max_ecpu_per_second: Maximum ECPUs per second (default: 5000)
            - snapshot_retention_limit: Daily snapshots to retain (default: 1)
        """
        default_config: dict[str, Any] = {
            "enabled": True,
            "max_data_storage_gb": 5,
            "max_ecpu_per_second": 5000,
            "snapshot_retention_limit": 1,
        }
        valkey_ctx = self.app.node.try_get_context("valkey")
        valkey_config: dict[str, Any] = valkey_ctx if isinstance(valkey_ctx, dict) else {}
        return {**default_config, **valkey_config}

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
        return {**default_config, **aurora_config}

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
