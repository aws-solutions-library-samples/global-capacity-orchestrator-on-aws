"""
Tests for the deployment_regions configuration block.

Verifies ConfigLoader enforces that deployment_regions is required
and that its sub-fields (regional, api_gateway, global, monitoring)
are loaded correctly from CDK context. Uses MockApp/MockNode stand-ins
and a shared base_context fixture containing every other required
field so only deployment_regions is exercised.
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
def base_context():
    """Create base configuration context without deployment_regions."""
    return {
        "project_name": "gco",
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


class TestDeploymentRegionsRequired:
    """Tests for deployment_regions being required."""

    def test_missing_deployment_regions_raises_error(self, base_context):
        """Test that missing deployment_regions raises ConfigValidationError."""
        app = MockApp(base_context)
        with pytest.raises(
            ConfigValidationError,
            match="Required configuration field 'deployment_regions' is missing",
        ):
            ConfigLoader(app)


class TestDeploymentRegionsDefaults:
    """Tests for deployment_regions default values."""

    def test_empty_deployment_regions_uses_defaults(self):
        """Test that empty deployment_regions dict uses default values."""
        app = MockApp({})  # Empty context skips validation
        config = ConfigLoader(app)

        regions = config.get_deployment_regions()
        assert regions["global"] == "us-east-2"
        assert regions["api_gateway"] == "us-east-2"
        assert regions["monitoring"] == "us-east-2"
        assert regions["regional"] == ["us-east-1"]

    def test_partial_deployment_regions_fills_defaults(self):
        """Test that partial deployment_regions fills in defaults."""
        app = MockApp({})
        config = ConfigLoader(app)

        # Manually set partial config
        config.app.node._context["deployment_regions"] = {"regional": ["us-west-2"]}

        regions = config.get_deployment_regions()
        assert regions["global"] == "us-east-2"  # Default
        assert regions["regional"] == ["us-west-2"]  # Custom


class TestDeploymentRegionsGetters:
    """Tests for individual region getter methods."""

    def test_get_global_region(self, base_context):
        """Test get_global_region returns correct value."""
        base_context["deployment_regions"] = {
            "global": "eu-west-1",
            "regional": ["us-east-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)

        assert config.get_global_region() == "eu-west-1"

    def test_get_api_gateway_region(self, base_context):
        """Test get_api_gateway_region returns correct value."""
        base_context["deployment_regions"] = {
            "api_gateway": "ap-southeast-1",
            "regional": ["us-east-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)

        assert config.get_api_gateway_region() == "ap-southeast-1"

    def test_get_monitoring_region(self, base_context):
        """Test get_monitoring_region returns correct value."""
        base_context["deployment_regions"] = {
            "monitoring": "ca-central-1",
            "regional": ["us-east-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)

        assert config.get_monitoring_region() == "ca-central-1"

    def test_get_regions_returns_regional_list(self, base_context):
        """Test get_regions returns the regional list."""
        base_context["deployment_regions"] = {
            "regional": ["us-east-1", "us-west-2", "eu-west-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)

        assert config.get_regions() == ["us-east-1", "us-west-2", "eu-west-1"]


class TestDeploymentRegionsValidation:
    """Tests for deployment_regions validation."""

    def test_invalid_global_region(self, base_context):
        """Test that invalid global region is allowed (not validated separately)."""
        # Note: Global region is not validated against VALID_REGIONS
        # Only regional list is validated
        base_context["deployment_regions"] = {
            "global": "invalid-region",
            "regional": ["us-east-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)
        assert config.get_global_region() == "invalid-region"

    def test_invalid_regional_region_raises_error(self, base_context):
        """Test that invalid regional region raises error."""
        base_context["deployment_regions"] = {
            "regional": ["us-east-1", "invalid-region"],
        }
        app = MockApp(base_context)
        with pytest.raises(ConfigValidationError, match="Invalid region 'invalid-region'"):
            ConfigLoader(app)

    def test_duplicate_regional_regions_raises_error(self, base_context):
        """Test that duplicate regional regions raises error."""
        base_context["deployment_regions"] = {
            "regional": ["us-east-1", "us-east-1"],
        }
        app = MockApp(base_context)
        with pytest.raises(ConfigValidationError, match="Duplicate regions"):
            ConfigLoader(app)

    def test_too_many_regional_regions_raises_error(self, base_context):
        """Test that more than 10 regional regions raises error."""
        base_context["deployment_regions"] = {
            "regional": [
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
            ],
        }
        app = MockApp(base_context)
        with pytest.raises(ConfigValidationError, match="Maximum of 10 regions"):
            ConfigLoader(app)

    def test_empty_regional_list_raises_error(self, base_context):
        """Test that empty regional list raises error."""
        base_context["deployment_regions"] = {
            "regional": [],
        }
        app = MockApp(base_context)
        with pytest.raises(ConfigValidationError, match="At least one region must be specified"):
            ConfigLoader(app)


class TestDeploymentRegionsMultiRegion:
    """Tests for multi-region deployment scenarios."""

    def test_different_regions_for_each_stack_type(self, base_context):
        """Test configuring different regions for each stack type."""
        base_context["deployment_regions"] = {
            "global": "us-east-2",
            "api_gateway": "us-west-2",
            "monitoring": "eu-west-1",
            "regional": ["ap-southeast-1", "ap-northeast-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)

        assert config.get_global_region() == "us-east-2"
        assert config.get_api_gateway_region() == "us-west-2"
        assert config.get_monitoring_region() == "eu-west-1"
        assert config.get_regions() == ["ap-southeast-1", "ap-northeast-1"]

    def test_all_stacks_in_same_region(self, base_context):
        """Test configuring all stacks in the same region."""
        base_context["deployment_regions"] = {
            "global": "us-east-1",
            "api_gateway": "us-east-1",
            "monitoring": "us-east-1",
            "regional": ["us-east-1"],
        }
        app = MockApp(base_context)
        config = ConfigLoader(app)

        assert config.get_global_region() == "us-east-1"
        assert config.get_api_gateway_region() == "us-east-1"
        assert config.get_monitoring_region() == "us-east-1"
        assert config.get_regions() == ["us-east-1"]
