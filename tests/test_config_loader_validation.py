"""
Tests for gco/config/config_loader.py validation rules.

Exercises the ConfigLoader validator across its defensive branches:
the no-op path when no context is provided (validation skipped),
missing required fields, empty regional region list, too many regions,
and other field-level constraints. Uses real cdk.App instances with
context= dicts so the CDK Node wiring is part of the test rather than
mocked out, which complements the MockApp-based test_config_loader.py.
"""

from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest


class TestConfigLoaderValidation:
    """Tests for ConfigLoader validation."""

    def test_config_loader_skips_validation_without_context(self):
        """Test that ConfigLoader skips validation when no context is present."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        # Should not raise - validation is skipped when project_name is None
        config = ConfigLoader(app)
        assert config is not None

    def test_config_loader_validates_required_fields(self):
        """Test that ConfigLoader validates required fields."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(context={"project_name": "test"})  # Missing other required fields

        with pytest.raises(ConfigValidationError, match="Required configuration field"):
            ConfigLoader(app)

    def test_config_loader_validates_empty_regions(self):
        """Test that ConfigLoader validates empty regions list."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        # Empty list is treated as falsy, so it triggers "Required configuration field" error
        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": []},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="At least one region must be specified"):
            ConfigLoader(app)

    def test_config_loader_validates_too_many_regions(self):
        """Test that ConfigLoader validates maximum regions."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        regions = [
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
        ]  # 11 regions

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": regions},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="Maximum of 10 regions"):
            ConfigLoader(app)

    def test_config_loader_validates_invalid_region(self):
        """Test that ConfigLoader validates invalid regions."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["invalid-region"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="Invalid region"):
            ConfigLoader(app)

    def test_config_loader_validates_duplicate_regions(self):
        """Test that ConfigLoader validates duplicate regions."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1", "us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="Duplicate regions"):
            ConfigLoader(app)

    def test_config_loader_validates_invalid_threshold(self):
        """Test that ConfigLoader validates invalid threshold values."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 150,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },  # Invalid: > 100
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="must be an integer between 0 and 100"):
            ConfigLoader(app)

    def test_config_loader_validates_missing_threshold(self):
        """Test that ConfigLoader validates missing threshold."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                },  # Missing gpu_threshold
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="Missing threshold configuration"):
            ConfigLoader(app)

    def test_config_loader_validates_invalid_health_check_path(self):
        """Test that ConfigLoader validates health check path."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "health",
                },  # Missing leading /
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="health_check_path must start with"):
            ConfigLoader(app)

    def test_config_loader_validates_invalid_throttle_burst(self):
        """Test that ConfigLoader validates throttle_burst_limit < throttle_rate_limit."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 200,
                    "throttle_burst_limit": 100,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },  # burst < rate
            }
        )

        with pytest.raises(
            ConfigValidationError, match="throttle_burst_limit should be greater than or equal"
        ):
            ConfigLoader(app)

    def test_config_loader_validates_invalid_log_level(self):
        """Test that ConfigLoader validates invalid log level."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "DEBUG",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },  # Invalid log level
            }
        )

        with pytest.raises(ConfigValidationError, match="log_level must be one of"):
            ConfigLoader(app)

    def test_config_loader_validates_non_boolean_metrics(self):
        """Test that ConfigLoader validates non-boolean metrics_enabled."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 1,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": "yes",
                    "tracing_enabled": True,
                },  # String instead of bool
            }
        )

        with pytest.raises(ConfigValidationError, match="metrics_enabled must be a boolean"):
            ConfigLoader(app)

    def test_config_loader_validates_invalid_replicas(self):
        """Test that ConfigLoader validates invalid replicas."""
        from gco.config.config_loader import ConfigLoader, ConfigValidationError

        app = cdk.App(
            context={
                "project_name": "test",
                "deployment_regions": {"regional": ["us-east-1"]},
                "kubernetes_version": "1.36",
                "resource_thresholds": {
                    "cpu_threshold": 80,
                    "memory_threshold": 85,
                    "gpu_threshold": 90,
                },
                "global_accelerator": {
                    "name": "test",
                    "health_check_grace_period": 30,
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "health_check_path": "/health",
                },
                "alb_config": {
                    "health_check_interval": 30,
                    "health_check_timeout": 5,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 2,
                },
                "manifest_processor": {
                    "image": "test",
                    "replicas": 0,
                    "resource_limits": {"cpu": "1", "memory": "1Gi"},
                },  # Invalid replicas
                "job_validation_policy": {
                    "allowed_namespaces": ["default"],
                    "resource_quotas": {},
                },
                "api_gateway": {
                    "throttle_rate_limit": 100,
                    "throttle_burst_limit": 200,
                    "log_level": "INFO",
                    "metrics_enabled": True,
                    "tracing_enabled": True,
                },
            }
        )

        with pytest.raises(ConfigValidationError, match="replicas must be a positive integer"):
            ConfigLoader(app)


class TestConfigLoaderRegionAvailability:
    """Tests for ConfigLoader region availability methods."""

    def test_validate_region_availability_success(self):
        """Test validate_region_availability returns True for valid region."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        config = ConfigLoader(app)

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

            result = config.validate_region_availability("us-east-1")
            assert result is True

    def test_validate_region_availability_failure(self):
        """Test validate_region_availability returns False on error."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        config = ConfigLoader(app)

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_regions.side_effect = Exception("API Error")

            result = config.validate_region_availability("us-east-1")
            assert result is False

    def test_get_available_regions_success(self):
        """Test get_available_regions returns list of regions."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        config = ConfigLoader(app)

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_regions.return_value = {
                "Regions": [
                    {"RegionName": "us-east-1"},
                    {"RegionName": "us-west-2"},
                ]
            }

            result = config.get_available_regions()
            assert "us-east-1" in result
            assert "us-west-2" in result

    def test_get_available_regions_fallback(self):
        """Test get_available_regions falls back to VALID_REGIONS on error."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        config = ConfigLoader(app)

        with patch("boto3.client") as mock_client:
            mock_ec2 = MagicMock()
            mock_client.return_value = mock_ec2
            mock_ec2.describe_regions.side_effect = Exception("API Error")

            result = config.get_available_regions()
            assert len(result) > 0
            assert "us-east-1" in result


class TestConfigLoaderGetters:
    """Tests for ConfigLoader getter methods with defaults."""

    def test_get_tags_default(self):
        """Test get_tags returns empty dict by default."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        config = ConfigLoader(app)

        tags = config.get_tags()
        assert tags == {}

    def test_get_fsx_lustre_config_default(self):
        """Test get_fsx_lustre_config returns defaults."""
        from gco.config.config_loader import ConfigLoader

        app = cdk.App()
        config = ConfigLoader(app)

        fsx_config = config.get_fsx_lustre_config()
        assert fsx_config["enabled"] is False
        assert fsx_config["storage_capacity_gib"] == 1200
        assert fsx_config["deployment_type"] == "SCRATCH_2"
