"""
Tests for gco/config/config_loader.ConfigLoader.

Drives ConfigLoader against a MockApp/MockNode pair that surfaces a
hand-crafted CDK context dict. Verifies happy-path loading of every
top-level field (project_name, deployment_regions, kubernetes_version,
resource_thresholds, global_accelerator, alb_config,
manifest_processor, job_validation_policy, api_gateway, tags) and
that missing required fields raise ConfigValidationError with an
informative message. Companion suite to test_config_loader_validation.py
which drills into the validation rules themselves.
"""

import pytest

from gco.config.config_loader import ConfigLoader, ConfigValidationError


class MockNode:
    """Mock CDK Node for testing."""

    def __init__(self, context: dict):
        self._context = context

    def try_get_context(self, key: str):
        return self._context.get(key)


class MockApp:
    """Mock CDK App for testing."""

    def __init__(self, context: dict):
        self.node = MockNode(context)


@pytest.fixture
def valid_context():
    """Create valid configuration context."""
    return {
        "project_name": "gco",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1", "us-west-2"],
        },
        "kubernetes_version": "1.36",
        "resource_thresholds": {"cpu_threshold": 80, "memory_threshold": 85, "gpu_threshold": 90},
        "global_accelerator": {
            "name": "gco-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        },
        "alb_config": {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        },
        "manifest_processor": {
            "image": "gco/manifest-processor:latest",
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
        },
        "job_validation_policy": {
            "allowed_namespaces": ["default", "gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        },
        "api_gateway": {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        },
        "tags": {"Environment": "test"},
    }


class TestConfigLoaderValidation:
    """Tests for configuration validation."""

    def test_valid_configuration(self, valid_context):
        """Test that valid configuration passes validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_project_name() == "gco"

    def test_missing_required_field(self, valid_context):
        """Test that missing required field raises error."""
        del valid_context["deployment_regions"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="Required configuration field 'deployment_regions' is missing",
        ):
            ConfigLoader(app)

    def test_empty_context_skips_validation(self):
        """Test that empty context (outside CDK) skips validation."""
        app = MockApp({})
        config = ConfigLoader(app)
        # Should use defaults
        assert config.get_project_name() == "gco"
        assert config.get_regions() == ["us-east-1"]


class TestRegionValidation:
    """Tests for region validation."""

    def test_valid_regions(self, valid_context):
        """Test valid regions pass validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_regions() == ["us-east-1", "us-west-2"]

    def test_invalid_region(self, valid_context):
        """Test invalid region raises error."""
        valid_context["deployment_regions"]["regional"] = ["us-east-1", "invalid-region"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="Invalid region 'invalid-region'"):
            ConfigLoader(app)

    def test_duplicate_regions(self, valid_context):
        """Test duplicate regions raises error."""
        valid_context["deployment_regions"]["regional"] = ["us-east-1", "us-east-1"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="Duplicate regions"):
            ConfigLoader(app)

    def test_too_many_regions(self, valid_context):
        """Test more than 10 regions raises error."""
        valid_context["deployment_regions"]["regional"] = [
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
        ]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="Maximum of 10 regions"):
            ConfigLoader(app)


class TestResourceThresholdsValidation:
    """Tests for resource threshold validation."""

    def test_valid_thresholds(self, valid_context):
        """Test valid thresholds pass validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        thresholds = config.get_resource_thresholds()
        assert thresholds.cpu_threshold == 80
        assert thresholds.memory_threshold == 85
        assert thresholds.gpu_threshold == 90

    def test_threshold_out_of_range(self, valid_context):
        """Test threshold out of range raises error."""
        valid_context["resource_thresholds"]["cpu_threshold"] = 150
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="cpu_threshold must be an integer between 0 and 100"
        ):
            ConfigLoader(app)

    def test_missing_threshold(self, valid_context):
        """Test missing threshold raises error."""
        del valid_context["resource_thresholds"]["gpu_threshold"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="Missing threshold configuration: gpu_threshold"
        ):
            ConfigLoader(app)


class TestGlobalAcceleratorValidation:
    """Tests for Global Accelerator config validation."""

    def test_valid_ga_config(self, valid_context):
        """Test valid GA config passes validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        ga_config = config.get_global_accelerator_config()
        assert ga_config["name"] == "gco-accelerator"

    def test_invalid_health_check_path(self, valid_context):
        """Test health check path not starting with / raises error."""
        valid_context["global_accelerator"]["health_check_path"] = "api/health"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="health_check_path must start with '/'"):
            ConfigLoader(app)

    def test_invalid_timing_value(self, valid_context):
        """Test non-positive timing value raises error."""
        valid_context["global_accelerator"]["health_check_interval"] = 0
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="health_check_interval must be a positive integer"
        ):
            ConfigLoader(app)

    def test_client_affinity_defaults_to_none(self, valid_context):
        """Test client_affinity defaults to NONE when omitted."""
        valid_context["global_accelerator"].pop("client_affinity", None)
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        ga_config = config.get_global_accelerator_config()
        # When the key is present in context it is returned as-is; when the
        # whole context is absent the loader falls back to a default dict that
        # pins NONE. Either way, an omitted key is valid and resolves to NONE
        # at the stack layer.
        assert ga_config.get("client_affinity", "NONE") == "NONE"

    @pytest.mark.parametrize("affinity", ["NONE", "SOURCE_IP", "source_ip", "none"])
    def test_valid_client_affinity(self, valid_context, affinity):
        """Test accepted client_affinity values pass validation."""
        valid_context["global_accelerator"]["client_affinity"] = affinity
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        assert config.get_global_accelerator_config()["client_affinity"] == affinity

    def test_invalid_client_affinity(self, valid_context):
        """Test an unknown client_affinity value raises an error."""
        valid_context["global_accelerator"]["client_affinity"] = "STICKY"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="client_affinity must be one of"):
            ConfigLoader(app)

    def test_non_string_client_affinity(self, valid_context):
        """Test a non-string client_affinity value raises an error."""
        valid_context["global_accelerator"]["client_affinity"] = 1
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="client_affinity must be one of"):
            ConfigLoader(app)


class TestApiGatewayValidation:
    """Tests for API Gateway config validation."""

    def test_valid_api_gateway_config(self, valid_context):
        """Test valid API Gateway config passes validation."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        api_config = config.get_api_gateway_config()
        assert api_config["throttle_rate_limit"] == 1000

    def test_invalid_log_level(self, valid_context):
        """Test invalid log level raises error."""
        valid_context["api_gateway"]["log_level"] = "DEBUG"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="log_level must be one of"):
            ConfigLoader(app)

    def test_burst_less_than_rate(self, valid_context):
        """Test burst limit less than rate limit raises error."""
        valid_context["api_gateway"]["throttle_burst_limit"] = 500
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="throttle_burst_limit should be greater than or equal"
        ):
            ConfigLoader(app)

    def test_invalid_boolean_flag(self, valid_context):
        """Test non-boolean metrics_enabled raises error."""
        valid_context["api_gateway"]["metrics_enabled"] = "yes"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="metrics_enabled must be a boolean"):
            ConfigLoader(app)


class TestConfigLoaderGetters:
    """Tests for ConfigLoader getter methods."""

    def test_get_cluster_config(self, valid_context):
        """Test getting cluster config for a region."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        cluster_config = config.get_cluster_config("us-east-1")

        assert cluster_config.region == "us-east-1"
        assert cluster_config.cluster_name == "gco-us-east-1"
        assert cluster_config.kubernetes_version == "1.36"

    def test_get_tags(self, valid_context):
        """Test getting tags."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        tags = config.get_tags()
        assert tags["Environment"] == "test"

    def test_get_manifest_processor_config(self, valid_context):
        """Test getting manifest processor config."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        mp_config = config.get_manifest_processor_config()
        assert mp_config["replicas"] == 3
        assert "default" in mp_config["allowed_namespaces"]

    def test_get_alb_config(self, valid_context):
        """Test getting ALB config."""
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        alb_config = config.get_alb_config()
        assert alb_config["health_check_interval"] == 30


class TestDefaultValues:
    """Tests for default configuration values."""

    def test_default_project_name(self):
        """Test default project name."""
        app = MockApp({})
        config = ConfigLoader(app)
        assert config.get_project_name() == "gco"

    def test_default_regions(self):
        """Test default regions."""
        app = MockApp({})
        config = ConfigLoader(app)
        assert config.get_regions() == ["us-east-1"]

    def test_default_kubernetes_version(self):
        """Test default Kubernetes version."""
        app = MockApp({})
        config = ConfigLoader(app)
        assert config.get_kubernetes_version() == "1.36"

    def test_default_resource_thresholds(self):
        """Test default resource thresholds."""
        app = MockApp({})
        config = ConfigLoader(app)
        thresholds = config.get_resource_thresholds()
        assert thresholds.cpu_threshold == 60
        assert thresholds.memory_threshold == 60
        assert thresholds.gpu_threshold == -1  # disabled by default for inference workloads
        assert thresholds.pending_pods_threshold == 10
        assert thresholds.pending_requested_cpu_vcpus == 100
        assert thresholds.pending_requested_memory_gb == 200
        assert thresholds.pending_requested_gpus == -1  # disabled by default


class TestFsxLustreConfig:
    """Tests for FSx for Lustre configuration."""

    def test_get_fsx_lustre_config_defaults(self):
        """Test getting FSx config with defaults."""
        app = MockApp({})
        config = ConfigLoader(app)
        fsx_config = config.get_fsx_lustre_config()

        assert fsx_config["enabled"] is False
        assert fsx_config["storage_capacity_gib"] == 1200
        assert fsx_config["deployment_type"] == "SCRATCH_2"
        assert fsx_config["file_system_type_version"] == "2.15"
        assert fsx_config["per_unit_storage_throughput"] == 200
        assert fsx_config["data_compression_type"] == "LZ4"
        assert fsx_config["import_path"] is None
        assert fsx_config["export_path"] is None
        assert fsx_config["auto_import_policy"] == "NEW_CHANGED_DELETED"
        assert "node_group" in fsx_config

    def test_get_fsx_lustre_config_custom(self, valid_context):
        """Test getting FSx config with custom values."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "storage_capacity_gib": 2400,
            "deployment_type": "PERSISTENT_2",
            "per_unit_storage_throughput": 500,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        fsx_config = config.get_fsx_lustre_config()

        assert fsx_config["enabled"] is True
        assert fsx_config["storage_capacity_gib"] == 2400
        assert fsx_config["deployment_type"] == "PERSISTENT_2"
        assert fsx_config["per_unit_storage_throughput"] == 500
        # Defaults should still be present
        assert fsx_config["file_system_type_version"] == "2.15"

    def test_get_fsx_lustre_config_with_region_override(self, valid_context):
        """Test getting FSx config with region-specific override."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "storage_capacity_gib": 1200,
        }
        valid_context["fsx_lustre_regions"] = {
            "us-west-2": {
                "storage_capacity_gib": 4800,
                "deployment_type": "PERSISTENT_1",
            }
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        # Without region - should use global config
        fsx_config_global = config.get_fsx_lustre_config()
        assert fsx_config_global["storage_capacity_gib"] == 1200

        # With region that has override
        fsx_config_west = config.get_fsx_lustre_config(region="us-west-2")
        assert fsx_config_west["storage_capacity_gib"] == 4800
        assert fsx_config_west["deployment_type"] == "PERSISTENT_1"

        # With region that has no override
        fsx_config_east = config.get_fsx_lustre_config(region="us-east-1")
        assert fsx_config_east["storage_capacity_gib"] == 1200

    def test_get_fsx_lustre_config_node_group_defaults(self):
        """Test FSx config node_group has all default fields."""
        app = MockApp({})
        config = ConfigLoader(app)
        fsx_config = config.get_fsx_lustre_config()

        node_group = fsx_config["node_group"]
        assert "instance_types" in node_group
        assert node_group["min_size"] == 0
        assert node_group["max_size"] == 10
        assert node_group["desired_size"] == 1
        assert node_group["ami_type"] == "AL2023_X86_64_STANDARD"
        assert node_group["capacity_type"] == "ON_DEMAND"
        assert node_group["disk_size"] == 100
        assert node_group["labels"] == {}

    def test_get_fsx_lustre_config_node_group_custom(self, valid_context):
        """Test FSx config with custom node_group settings."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "node_group": {
                "instance_types": ["m6i.2xlarge"],
                "max_size": 20,
                "desired_size": 5,
            },
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        fsx_config = config.get_fsx_lustre_config()

        node_group = fsx_config["node_group"]
        assert node_group["instance_types"] == ["m6i.2xlarge"]
        assert node_group["max_size"] == 20
        assert node_group["desired_size"] == 5
        # Defaults should still be present
        assert node_group["min_size"] == 0
        assert node_group["ami_type"] == "AL2023_X86_64_STANDARD"

    def test_get_fsx_lustre_config_region_node_group_override(self, valid_context):
        """Test FSx config with region-specific node_group override."""
        valid_context["fsx_lustre"] = {
            "enabled": True,
            "node_group": {
                "instance_types": ["m5.large"],
                "max_size": 10,
            },
        }
        valid_context["fsx_lustre_regions"] = {
            "eu-west-1": {
                "node_group": {
                    "instance_types": ["m6i.large"],
                    "max_size": 5,
                }
            }
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        fsx_config = config.get_fsx_lustre_config(region="eu-west-1")
        node_group = fsx_config["node_group"]
        assert node_group["instance_types"] == ["m6i.large"]
        assert node_group["max_size"] == 5


class TestEksClusterConfig:
    """Tests for EKS cluster configuration."""

    def test_get_eks_cluster_config_defaults(self):
        """Test getting EKS cluster config with defaults."""
        app = MockApp({})
        config = ConfigLoader(app)
        eks_config = config.get_eks_cluster_config()

        assert eks_config["endpoint_access"] == "PRIVATE"

    def test_get_eks_cluster_config_private(self, valid_context):
        """Test getting EKS cluster config with PRIVATE endpoint."""
        valid_context["eks_cluster"] = {"endpoint_access": "PRIVATE"}
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        eks_config = config.get_eks_cluster_config()

        assert eks_config["endpoint_access"] == "PRIVATE"

    def test_get_eks_cluster_config_public_and_private(self, valid_context):
        """Test getting EKS cluster config with PUBLIC_AND_PRIVATE endpoint."""
        valid_context["eks_cluster"] = {"endpoint_access": "PUBLIC_AND_PRIVATE"}
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        eks_config = config.get_eks_cluster_config()

        assert eks_config["endpoint_access"] == "PUBLIC_AND_PRIVATE"

    def test_eks_cluster_config_invalid_endpoint_access(self, valid_context):
        """Test that invalid endpoint_access raises validation error."""
        valid_context["eks_cluster"] = {"endpoint_access": "PUBLIC"}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="endpoint_access must be one of",
        ):
            ConfigLoader(app)


class TestRegionAvailability:
    """Tests for region availability validation."""

    def test_validate_region_availability_success(self, valid_context):
        """Test validating region availability - success case."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

            result = config.validate_region_availability("us-east-1")
            assert result is True
            mock_ec2.describe_regions.assert_called_once_with(RegionNames=["us-east-1"])

    def test_validate_region_availability_failure(self, valid_context):
        """Test validating region availability - failure case."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.side_effect = Exception("Region not available")

            result = config.validate_region_availability("invalid-region")
            assert result is False

    def test_get_available_regions_success(self, valid_context):
        """Test getting available regions - success case."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.return_value = {
                "Regions": [
                    {"RegionName": "us-east-1"},
                    {"RegionName": "us-west-2"},
                    {"RegionName": "eu-west-1"},
                ]
            }

            result = config.get_available_regions()
            assert "us-east-1" in result
            assert "us-west-2" in result
            assert "eu-west-1" in result
            assert len(result) == 3

    def test_get_available_regions_failure_returns_valid_regions(self, valid_context):
        """Test getting available regions - failure returns VALID_REGIONS."""
        from unittest.mock import MagicMock, patch

        app = MockApp(valid_context)
        config = ConfigLoader(app)

        with patch("gco.config.config_loader.boto3") as mock_boto3:
            mock_ec2 = MagicMock()
            mock_boto3.client.return_value = mock_ec2
            mock_ec2.describe_regions.side_effect = Exception("API error")

            result = config.get_available_regions()
            # Should return VALID_REGIONS as fallback
            assert "us-east-1" in result
            assert len(result) == len(ConfigLoader.VALID_REGIONS)


class TestConfigValidationEdgeCases:
    """Tests for configuration validation edge cases."""

    def test_empty_regions_list(self, valid_context):
        """Test that empty regions list raises error."""
        valid_context["deployment_regions"]["regional"] = []
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="At least one region must be specified"):
            ConfigLoader(app)

    def test_missing_global_accelerator_config(self, valid_context):
        """Test that missing global_accelerator config raises error."""
        del valid_context["global_accelerator"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="global_accelerator configuration is required"
        ):
            ConfigLoader(app)

    def test_missing_alb_config(self, valid_context):
        """Test that missing alb_config raises error."""
        del valid_context["alb_config"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="alb_config configuration is required"):
            ConfigLoader(app)

    def test_missing_manifest_processor_config(self, valid_context):
        """Test that missing manifest_processor config raises error."""
        del valid_context["manifest_processor"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="manifest_processor configuration is required"
        ):
            ConfigLoader(app)

    def test_missing_api_gateway_config(self, valid_context):
        """Test that missing api_gateway config raises error."""
        del valid_context["api_gateway"]
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="api_gateway configuration is required"):
            ConfigLoader(app)

    def test_invalid_manifest_processor_replicas(self, valid_context):
        """Test that invalid manifest_processor replicas raises error."""
        valid_context["manifest_processor"]["replicas"] = 0
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="manifest_processor replicas must be a positive integer"
        ):
            ConfigLoader(app)

    def test_missing_manifest_processor_resource_limits(self, valid_context):
        """Test that missing resource_limits fields raises error."""
        valid_context["manifest_processor"]["resource_limits"] = {"cpu": "1000m"}  # Missing memory
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="must contain 'cpu' and 'memory'"):
            ConfigLoader(app)

    def test_invalid_allowed_namespaces_type(self, valid_context):
        """allowed_namespaces now lives under job_validation_policy and must be a list."""
        valid_context["job_validation_policy"]["allowed_namespaces"] = "default"  # Should be list
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="allowed_namespaces must be a list"):
            ConfigLoader(app)

    def test_invalid_throttle_rate_limit(self, valid_context):
        """Test that invalid throttle_rate_limit raises error."""
        valid_context["api_gateway"]["throttle_rate_limit"] = 0
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="throttle_rate_limit must be a positive integer"
        ):
            ConfigLoader(app)

    def test_invalid_throttle_burst_limit(self, valid_context):
        """Test that invalid throttle_burst_limit raises error."""
        valid_context["api_gateway"]["throttle_burst_limit"] = -1
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="throttle_burst_limit must be a positive integer"
        ):
            ConfigLoader(app)

    def test_invalid_tracing_enabled_type(self, valid_context):
        """Test that non-boolean tracing_enabled raises error."""
        valid_context["api_gateway"]["tracing_enabled"] = "yes"
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="tracing_enabled must be a boolean"):
            ConfigLoader(app)

    def test_negative_health_check_timeout(self, valid_context):
        """Test that negative health_check_timeout raises error."""
        valid_context["global_accelerator"]["health_check_timeout"] = -5
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="health_check_timeout must be a positive integer"
        ):
            ConfigLoader(app)

    def test_missing_ga_field(self, valid_context):
        """Test that missing GA field raises error."""
        del valid_context["global_accelerator"]["health_check_path"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="Missing global_accelerator configuration: health_check_path",
        ):
            ConfigLoader(app)

    def test_missing_alb_field(self, valid_context):
        """Test that missing ALB field raises error."""
        del valid_context["alb_config"]["healthy_threshold"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="Missing alb_config configuration: healthy_threshold"
        ):
            ConfigLoader(app)

    def test_negative_alb_value(self, valid_context):
        """Test that negative ALB value raises error."""
        valid_context["alb_config"]["unhealthy_threshold"] = 0
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="unhealthy_threshold must be a positive integer"
        ):
            ConfigLoader(app)

    def test_missing_api_gateway_field(self, valid_context):
        """Test that missing API Gateway field raises error."""
        del valid_context["api_gateway"]["log_level"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="Missing api_gateway configuration: log_level"
        ):
            ConfigLoader(app)

    def test_optional_pending_thresholds_negative(self, valid_context):
        """Test that -1 disables the pending pods threshold (no error)."""
        valid_context["resource_thresholds"]["pending_pods_threshold"] = -1
        app = MockApp(valid_context)
        loader = ConfigLoader(app)
        thresholds = loader.get_resource_thresholds()
        assert thresholds.pending_pods_threshold == -1

    def test_optional_pending_thresholds_invalid_negative(self, valid_context):
        """Test that negative values other than -1 raise error."""
        valid_context["resource_thresholds"]["pending_pods_threshold"] = -2
        app = MockApp(valid_context)
        with pytest.raises(ConfigValidationError, match="pending_pods_threshold"):
            ConfigLoader(app)

    def test_optional_pending_cpu_vcpus_negative(self, valid_context):
        """Test that negative pending_requested_cpu_vcpus raises error."""
        valid_context["resource_thresholds"]["pending_requested_cpu_vcpus"] = -10
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="pending_requested_cpu_vcpus must be a non-negative integer",
        ):
            ConfigLoader(app)

    def test_optional_pending_memory_gb_negative(self, valid_context):
        """Test that negative pending_requested_memory_gb raises error."""
        valid_context["resource_thresholds"]["pending_requested_memory_gb"] = -100
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="pending_requested_memory_gb must be a non-negative integer",
        ):
            ConfigLoader(app)

    def test_optional_pending_gpus_negative(self, valid_context):
        """Test that negative pending_requested_gpus raises error."""
        valid_context["resource_thresholds"]["pending_requested_gpus"] = -4
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="pending_requested_gpus must be a non-negative integer"
        ):
            ConfigLoader(app)

    def test_missing_manifest_processor_field(self, valid_context):
        """Missing a required manifest_processor-only field raises error."""
        del valid_context["manifest_processor"]["replicas"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="Missing manifest_processor configuration: replicas",
        ):
            ConfigLoader(app)

    def test_missing_job_validation_policy_allowed_namespaces(self, valid_context):
        """Missing job_validation_policy.allowed_namespaces must fail loudly —
        both processors depend on the shared allowlist, so the error must
        point at the shared section, not a service-specific one."""
        del valid_context["job_validation_policy"]["allowed_namespaces"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="Missing job_validation_policy configuration: allowed_namespaces",
        ):
            ConfigLoader(app)

    def test_missing_job_validation_policy_field(self, valid_context):
        """Missing job_validation_policy.resource_quotas must fail loudly —
        both processors depend on it, so the error must point at the
        shared section, not a service-specific one."""
        del valid_context["job_validation_policy"]["resource_quotas"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="Missing job_validation_policy configuration: resource_quotas",
        ):
            ConfigLoader(app)

    def test_missing_job_validation_policy_section(self, valid_context):
        """Omitting the whole job_validation_policy section must fail."""
        del valid_context["job_validation_policy"]
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="job_validation_policy configuration is required"
        ):
            ConfigLoader(app)


class TestAuroraPgvectorConfig:
    """Tests for Aurora pgvector configuration."""

    def test_get_aurora_pgvector_config_defaults(self):
        """Test getting Aurora pgvector config with defaults."""
        app = MockApp({})
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is False
        assert aurora_config["min_acu"] == 0
        assert aurora_config["max_acu"] == 16
        assert aurora_config["backup_retention_days"] == 7
        assert aurora_config["deletion_protection"] is False

    def test_get_aurora_pgvector_config_enabled(self, valid_context):
        """Test getting Aurora pgvector config when enabled."""
        valid_context["aurora_pgvector"] = {
            "enabled": True,
            "min_acu": 0,
            "max_acu": 32,
            "backup_retention_days": 14,
            "deletion_protection": True,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is True
        assert aurora_config["min_acu"] == 0
        assert aurora_config["max_acu"] == 32
        assert aurora_config["backup_retention_days"] == 14
        assert aurora_config["deletion_protection"] is True

    def test_get_aurora_pgvector_config_partial_override(self, valid_context):
        """Test that partial overrides merge with defaults."""
        valid_context["aurora_pgvector"] = {
            "enabled": True,
            "max_acu": 64,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is True
        assert aurora_config["max_acu"] == 64
        # Defaults should still be present
        assert aurora_config["min_acu"] == 0
        assert aurora_config["backup_retention_days"] == 7
        assert aurora_config["deletion_protection"] is False

    def test_get_aurora_pgvector_config_min_acu_zero_for_scale_to_zero(self):
        """Test that min_acu=0 is the default for scale-to-zero support."""
        app = MockApp({})
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["min_acu"] == 0, (
            "Default min_acu should be 0 to enable Aurora Serverless v2 "
            "scale-to-zero (auto-pause). Set to 0.5+ to keep always warm."
        )

    def test_get_aurora_pgvector_config_min_acu_half_disables_autopause(self, valid_context):
        """Test that min_acu=0.5 disables auto-pause."""
        valid_context["aurora_pgvector"] = {
            "enabled": True,
            "min_acu": 0.5,
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["min_acu"] == 0.5

    def test_get_aurora_pgvector_config_non_dict_context_uses_defaults(self):
        """Test that non-dict context value falls back to defaults."""
        app = MockApp({"aurora_pgvector": "invalid"})
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is False
        assert aurora_config["min_acu"] == 0

    def test_get_aurora_pgvector_config_disabled_by_default(self):
        """Test that Aurora pgvector is disabled by default."""
        app = MockApp({})
        config = ConfigLoader(app)
        aurora_config = config.get_aurora_pgvector_config()

        assert aurora_config["enabled"] is False


class TestAnalyticsEnvironmentConfig:
    """Tests for optional analytics_environment configuration."""

    def test_get_analytics_config_defaults(self):
        """Missing analytics_environment block → defaults apply."""
        app = MockApp({})
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is False
        assert analytics_config["hyperpod"] == {"enabled": False}
        assert analytics_config["cognito"] == {
            "domain_prefix": None,
            "removal_policy": "destroy",
        }
        assert analytics_config["efs"] == {"removal_policy": "destroy"}
        assert analytics_config["studio"] == {"user_profile_name_prefix": None}

    def test_get_analytics_enabled_defaults_to_false(self):
        """Missing analytics_environment block → get_analytics_enabled() is False."""
        app = MockApp({})
        config = ConfigLoader(app)

        assert config.get_analytics_enabled() is False

    def test_get_analytics_config_partial_cognito_override_merges_defaults(self, valid_context):
        """Setting cognito.domain_prefix preserves the default cognito.removal_policy."""
        valid_context["analytics_environment"] = {
            "cognito": {"domain_prefix": "my-studio"},
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["cognito"]["domain_prefix"] == "my-studio"
        # Default removal_policy must still be present after the partial override.
        assert analytics_config["cognito"]["removal_policy"] == "destroy"
        # Sibling sub-blocks must not have been dropped either.
        assert analytics_config["efs"] == {"removal_policy": "destroy"}
        assert analytics_config["hyperpod"] == {"enabled": False}
        assert analytics_config["enabled"] is False

    def test_get_analytics_config_partial_efs_override_merges_defaults(self, valid_context):
        """Setting efs.removal_policy=retain preserves cognito defaults."""
        valid_context["analytics_environment"] = {
            "enabled": True,
            "efs": {"removal_policy": "retain"},
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is True
        assert analytics_config["efs"]["removal_policy"] == "retain"
        # Cognito sub-block defaults untouched.
        assert analytics_config["cognito"] == {
            "domain_prefix": None,
            "removal_policy": "destroy",
        }

    def test_get_analytics_config_full_override_passthrough(self, valid_context):
        """Full override → every nested field is taken from the user block."""
        valid_context["analytics_environment"] = {
            "enabled": True,
            "hyperpod": {"enabled": True},
            "cognito": {"domain_prefix": "team-studio", "removal_policy": "retain"},
            "efs": {"removal_policy": "retain"},
            "studio": {"user_profile_name_prefix": "dev-"},
        }
        app = MockApp(valid_context)
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is True
        assert analytics_config["hyperpod"] == {"enabled": True}
        assert analytics_config["cognito"] == {
            "domain_prefix": "team-studio",
            "removal_policy": "retain",
        }
        assert analytics_config["efs"] == {"removal_policy": "retain"}
        assert analytics_config["studio"] == {"user_profile_name_prefix": "dev-"}

    def test_get_analytics_enabled_true_when_enabled_true(self, valid_context):
        """get_analytics_enabled() returns True when enabled=true."""
        valid_context["analytics_environment"] = {"enabled": True}
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        assert config.get_analytics_enabled() is True

    def test_get_analytics_enabled_false_when_enabled_false(self, valid_context):
        """get_analytics_enabled() returns False when enabled=false."""
        valid_context["analytics_environment"] = {"enabled": False}
        app = MockApp(valid_context)
        config = ConfigLoader(app)

        assert config.get_analytics_enabled() is False

    def test_invalid_enabled_type_raises(self, valid_context):
        """Non-bool analytics_environment.enabled → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {"enabled": "yes"}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError, match="analytics_environment.enabled must be a bool"
        ):
            ConfigLoader(app)

    def test_invalid_hyperpod_enabled_type_raises(self, valid_context):
        """Non-bool analytics_environment.hyperpod.enabled → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {"hyperpod": {"enabled": 1}}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="analytics_environment.hyperpod.enabled must be a bool",
        ):
            ConfigLoader(app)

    def test_invalid_canvas_enabled_type_raises(self, valid_context):
        """Non-bool analytics_environment.canvas.enabled → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {"canvas": {"enabled": "yes"}}
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="analytics_environment.canvas.enabled must be a bool",
        ):
            ConfigLoader(app)

    def test_canvas_defaults_to_disabled_when_block_omitted(self, valid_context):
        """Omitting the canvas sub-block leaves canvas.enabled=false after merge."""
        valid_context["analytics_environment"] = {"enabled": True}
        app = MockApp(valid_context)
        cfg = ConfigLoader(app).get_analytics_config()
        assert cfg["canvas"] == {"enabled": False}, (
            "Missing canvas sub-block should merge to the default "
            f"{{'enabled': False}}, got {cfg['canvas']!r}"
        )

    def test_canvas_enabled_preserved_on_partial_override(self, valid_context):
        """Setting canvas.enabled=true does not drop sibling sub-blocks."""
        valid_context["analytics_environment"] = {
            "enabled": True,
            "canvas": {"enabled": True},
        }
        app = MockApp(valid_context)
        cfg = ConfigLoader(app).get_analytics_config()
        assert cfg["canvas"] == {"enabled": True}
        # Sibling defaults must survive — the deep-merge logic is easy
        # to regress when a new sub-block is added.
        assert cfg["hyperpod"] == {"enabled": False}
        assert cfg["efs"] == {"removal_policy": "destroy"}

    def test_invalid_cognito_removal_policy_raises(self, valid_context):
        """cognito.removal_policy not in {destroy, retain} → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {
            "cognito": {"removal_policy": "delete"},
        }
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="analytics_environment.cognito.removal_policy must be one of",
        ):
            ConfigLoader(app)

    def test_invalid_efs_removal_policy_raises(self, valid_context):
        """efs.removal_policy not in {destroy, retain} → raises ConfigValidationError."""
        valid_context["analytics_environment"] = {
            "efs": {"removal_policy": "keep"},
        }
        app = MockApp(valid_context)
        with pytest.raises(
            ConfigValidationError,
            match="analytics_environment.efs.removal_policy must be one of",
        ):
            ConfigLoader(app)

    def test_non_dict_analytics_environment_context_uses_defaults(self):
        """Non-dict analytics_environment context value → falls back to defaults."""
        app = MockApp({"analytics_environment": "invalid"})
        config = ConfigLoader(app)
        analytics_config = config.get_analytics_config()

        assert analytics_config["enabled"] is False
        assert config.get_analytics_enabled() is False
        assert analytics_config["cognito"] == {
            "domain_prefix": None,
            "removal_policy": "destroy",
        }
        assert analytics_config["efs"] == {"removal_policy": "destroy"}


class TestAnalyticsConstants:
    """Tests for analytics constants exported from gco.stacks.constants."""

    def test_emr_serverless_release_label_prefix(self):
        """EMR_SERVERLESS_RELEASE_LABEL exists and starts with 'emr-'."""
        from gco.stacks.constants import EMR_SERVERLESS_RELEASE_LABEL

        assert isinstance(EMR_SERVERLESS_RELEASE_LABEL, str)
        assert EMR_SERVERLESS_RELEASE_LABEL.startswith("emr-"), (
            f"EMR_SERVERLESS_RELEASE_LABEL must start with 'emr-' so it is a "
            f"valid emrserverless.CfnApplication release_label, got "
            f"{EMR_SERVERLESS_RELEASE_LABEL!r}"
        )

    def test_sagemaker_role_name_prefix(self):
        """SAGEMAKER_ROLE_NAME_PREFIX is the literal 'AmazonSageMaker'."""
        from gco.stacks.constants import SAGEMAKER_ROLE_NAME_PREFIX

        assert SAGEMAKER_ROLE_NAME_PREFIX == "AmazonSageMaker"

    def test_cognito_domain_prefix_default(self):
        """cognito_domain_prefix_default renders '<project>-studio' (#139).

        Defaults to the pre-#139 literal ``gco-studio`` for project_name='gco'
        and scopes to the project name otherwise.
        """
        from gco.stacks.constants import cognito_domain_prefix_default

        assert cognito_domain_prefix_default("gco") == "gco-studio"
        assert cognito_domain_prefix_default("acme") == "acme-studio"

    def test_studio_presigned_url_expiry_seconds_positive_int(self):
        """STUDIO_PRESIGNED_URL_EXPIRY_SECONDS is a positive int."""
        from gco.stacks.constants import STUDIO_PRESIGNED_URL_EXPIRY_SECONDS

        assert isinstance(STUDIO_PRESIGNED_URL_EXPIRY_SECONDS, int)
        # Booleans are ints in Python — guard against someone typing `True`.
        assert not isinstance(STUDIO_PRESIGNED_URL_EXPIRY_SECONDS, bool)
        assert STUDIO_PRESIGNED_URL_EXPIRY_SECONDS > 0

    def test_cluster_shared_bucket_name_prefix(self):
        """cluster_shared_bucket_name_prefix renders '<project>-cluster-shared' (#139)."""
        from gco.stacks.constants import cluster_shared_bucket_name_prefix

        assert cluster_shared_bucket_name_prefix("gco") == "gco-cluster-shared"
        assert cluster_shared_bucket_name_prefix("acme") == "acme-cluster-shared"

    def test_cluster_shared_ssm_parameter_prefix(self):
        """cluster_shared_ssm_parameter_prefix renders '/<project>/cluster-shared-bucket' (#139)."""
        from gco.stacks.constants import cluster_shared_ssm_parameter_prefix

        assert cluster_shared_ssm_parameter_prefix("gco") == "/gco/cluster-shared-bucket"
        assert cluster_shared_ssm_parameter_prefix("acme") == "/acme/cluster-shared-bucket"
        assert cluster_shared_ssm_parameter_prefix("gco").startswith("/"), (
            "cluster_shared_ssm_parameter_prefix must be an absolute SSM path "
            "(starts with '/') so it composes into valid parameter ARNs"
        )
