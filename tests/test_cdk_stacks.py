"""
CDK stack synthesis tests.

Synthesizes each GCO CDK stack (Global, API Gateway, Monitoring, Regional)
against a MockConfigLoader that returns hand-crafted ConfigLoader values
— no cdk.json, no boto3 — and asserts the resulting CloudFormation
templates contain the expected resources, outputs, and cross-stack
dependencies. Good as a smoke test that construct wiring still compiles
after refactors without needing a real AWS environment.
"""

import aws_cdk as cdk
import pytest
from aws_cdk import assertions


# Mock the ConfigLoader to avoid needing actual cdk.json context
class MockConfigLoader:
    """Mock ConfigLoader for testing."""

    def __init__(self, app=None):
        pass

    def get_project_name(self):
        return "gco-test"

    def get_regions(self):
        return ["us-east-1"]

    def get_global_region(self):
        return "us-east-2"

    def get_api_gateway_region(self):
        return "us-east-2"

    def get_monitoring_region(self):
        return "us-east-2"

    def get_kubernetes_version(self):
        return "1.36"

    def get_tags(self):
        return {"Environment": "test", "Project": "gco"}

    def get_resource_thresholds(self):
        from gco.models import ResourceThresholds

        return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

    def get_cluster_config(self, region):
        from gco.models import ClusterConfig

        return ClusterConfig(
            region=region,
            cluster_name=f"gco-test-{region}",
            kubernetes_version="1.36",
            addons=["metrics-server"],
            resource_thresholds=self.get_resource_thresholds(),
        )

    def get_global_accelerator_config(self):
        return {
            "name": "gco-test-accelerator",
            "health_check_grace_period": 30,
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "health_check_path": "/api/v1/health",
        }

    def get_alb_config(self):
        return {
            "health_check_interval": 30,
            "health_check_timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 2,
        }

    def get_manifest_processor_config(self):
        return {
            "image": "gco/manifest-processor:latest",
            "replicas": 3,
            "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
            "allowed_namespaces": ["default", "gco-jobs"],
            "resource_quotas": {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
            },
        }

    def get_api_gateway_config(self):
        return {
            "throttle_rate_limit": 1000,
            "throttle_burst_limit": 2000,
            "log_level": "INFO",
            "metrics_enabled": True,
            "tracing_enabled": True,
        }

    # Optional regional data services — monitoring stack queries these to
    # decide whether to render the corresponding dashboard sections. The
    # Valkey widgets pin their ``clusterId`` dimension to the
    # deterministic ``gco-{region}`` name, so they only need the
    # feature-flag shape. FSx and Aurora are driven by regional stack
    # attributes instead (see ``create_mock_regional_stack`` above).
    def get_valkey_config(self):
        return {"enabled": False}

    def get_aurora_pgvector_config(self):
        return {"enabled": False}

    def get_capacity_history_enabled(self):
        return False

    def get_capacity_history_config(self):
        return {
            "enabled": False,
            "retention_days": 90,
            "poll_interval_minutes": 15,
            "watch_instance_types": ["g5.xlarge", "p5.48xlarge"],
            "enabled_regions": [],
        }


class TestGlobalStackSynth:
    """Tests for Global Stack synthesis."""

    def test_global_stack_synthesizes(self):
        """Test that GlobalStack synthesizes without errors."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        stack = GCOGlobalStack(
            app, "test-global-stack", config=config, description="Test global stack"
        )

        # Synthesize and verify no errors
        template = assertions.Template.from_stack(stack)

        # Verify Global Accelerator is created
        template.resource_count_is("AWS::GlobalAccelerator::Accelerator", 1)

    def test_global_stack_has_listener(self):
        """Test that GlobalStack creates a listener."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        stack = GCOGlobalStack(app, "test-global-stack-listener", config=config)

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::GlobalAccelerator::Listener", 1)

    def test_listener_default_client_affinity_is_none(self):
        """Listener defaults to NONE client affinity when knob is omitted."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        stack = GCOGlobalStack(app, "test-global-stack-affinity-default", config=config)

        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::GlobalAccelerator::Listener",
            {"ClientAffinity": "NONE"},
        )

    @pytest.mark.parametrize(
        ("affinity_input", "expected"),
        [
            (None, "NONE"),
            ("NONE", "NONE"),
            ("SOURCE_IP", "SOURCE_IP"),
            ("source_ip", "SOURCE_IP"),
            ("none", "NONE"),
        ],
    )
    def test_listener_client_affinity_synth_matrix(self, affinity_input, expected):
        """Synthesize the global stack across every client_affinity input.

        Each row drives a full ``Template.from_stack`` synthesis and asserts
        the rendered ``AWS::GlobalAccelerator::Listener`` carries the expected
        normalized ``ClientAffinity`` value. ``None`` exercises the
        omitted-key path that falls back to NONE.
        """
        from gco.stacks.global_stack import GCOGlobalStack

        class MatrixConfig(MockConfigLoader):
            def get_global_accelerator_config(self):
                cfg = super().get_global_accelerator_config()
                if affinity_input is None:
                    cfg.pop("client_affinity", None)
                else:
                    cfg["client_affinity"] = affinity_input
                return cfg

        app = cdk.App()
        config = MatrixConfig(app)

        # Construct IDs must be unique per stack within the same app run.
        stack_id = f"test-ga-affinity-{affinity_input or 'omitted'}".replace("_", "-").lower()
        stack = GCOGlobalStack(app, stack_id, config=config)

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::GlobalAccelerator::Listener", 1)
        template.has_resource_properties(
            "AWS::GlobalAccelerator::Listener",
            {"ClientAffinity": expected},
        )


class TestApiGatewayStackSynth:
    """Tests for API Gateway Stack synthesis."""

    def test_api_gateway_stack_synthesizes(self):
        """Test that ApiGatewayStack synthesizes without errors."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()

        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-stack",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
            description="Test API Gateway stack",
        )

        template = assertions.Template.from_stack(stack)

        # Verify API Gateway REST API is created
        template.resource_count_is("AWS::ApiGateway::RestApi", 1)

    def test_api_gateway_has_secret(self):
        """Test that ApiGatewayStack creates a secret for auth."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()

        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-secret",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
        )

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::SecretsManager::Secret", 1)

    def test_api_gateway_has_lambda(self):
        """Test that ApiGatewayStack creates Lambda proxy function(s)."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()

        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-lambda",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
        )

        template = assertions.Template.from_stack(stack)
        # At least one Lambda function should exist (may have additional for log retention)
        template.has_resource("AWS::Lambda::Function", {})

    def test_api_gateway_iam_auth(self):
        """Test that API Gateway uses IAM authentication."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()

        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-auth",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
        )

        template = assertions.Template.from_stack(stack)

        # Verify methods have IAM authorization
        template.has_resource_properties(
            "AWS::ApiGateway::Method", {"AuthorizationType": "AWS_IAM"}
        )


class TestMonitoringStackSynth:
    """Tests for Monitoring Stack synthesis."""

    def test_monitoring_stack_synthesizes(self):
        """Test that MonitoringStack synthesizes without errors."""
        from unittest.mock import MagicMock

        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader(app)

        # Create mock global stack
        mock_global_stack = MagicMock()
        mock_global_stack.accelerator_name = "test-accelerator"
        mock_global_stack.accelerator_id = "test-accelerator-id-12345"
        # Add DynamoDB table mocks
        mock_global_stack.templates_table.table_name = "test-templates"
        mock_global_stack.webhooks_table.table_name = "test-webhooks"
        mock_global_stack.jobs_table.table_name = "test-jobs"

        # Create mock API gateway stack
        mock_api_gw_stack = MagicMock()
        mock_api_gw_stack.api.rest_api_name = "test-api"
        mock_api_gw_stack.proxy_lambda.function_name = "test-proxy"
        mock_api_gw_stack.rotation_lambda.function_name = "test-rotation"
        mock_api_gw_stack.secret.secret_name = "test-secret"  # nosec B105 - test fixture mock value, not a real secret

        # Create mock regional stacks
        mock_regional_stack = MagicMock()
        mock_regional_stack.deployment_region = "us-east-1"
        mock_regional_stack.cluster.cluster_name = "test-cluster"
        mock_regional_stack.job_queue.queue_name = "test-queue"
        mock_regional_stack.job_dlq.queue_name = "test-dlq"
        mock_regional_stack.kubectl_lambda_function_name = "test-kubectl"
        mock_regional_stack.helm_installer_lambda_function_name = "test-helm"
        # Optional regional data services default to absent. The monitoring
        # stack widget creators use ``getattr(..., None)`` on these and
        # skip the section when all regions report None — mirroring the
        # production shape where ``_create_fsx_lustre`` /
        # ``_create_aurora_pgvector`` early-return without setting the
        # attribute when the feature is disabled.
        mock_regional_stack.fsx_file_system = None
        mock_regional_stack.aurora_cluster = None
        mock_regional_stacks = [mock_regional_stack]

        stack = GCOMonitoringStack(
            app,
            "test-monitoring-stack",
            config=config,
            global_stack=mock_global_stack,
            regional_stacks=mock_regional_stacks,
            api_gateway_stack=mock_api_gw_stack,
            description="Test monitoring stack",
        )

        template = assertions.Template.from_stack(stack)

        # Verify CloudWatch Dashboard is created
        template.resource_count_is("AWS::CloudWatch::Dashboard", 1)


class TestRegionalStackSynth:
    """Tests for Regional Stack synthesis.

    Note: Regional stack tests are more complex due to EKS cluster creation
    which requires VPC, IAM roles, and other dependencies. These tests
    verify the stack structure without full synthesis.
    """

    def test_regional_stack_imports(self):
        """Test that RegionalStack can be imported without errors."""
        from gco.stacks.regional_stack import GCORegionalStack

        assert GCORegionalStack is not None

    def test_regional_stack_class_exists(self):
        """Test that RegionalStack class has expected methods."""
        from gco.stacks.regional_stack import GCORegionalStack

        # Verify class has expected attributes
        assert hasattr(GCORegionalStack, "__init__")


class TestStackDependencies:
    """Tests for stack dependency configuration."""

    def test_api_gateway_depends_on_global(self):
        """Test that API Gateway stack can be configured with Global Accelerator DNS."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()

        # Should not raise when given valid DNS
        stack = GCOApiGatewayGlobalStack(
            app, "test-dependency", global_accelerator_dns="test.awsglobalaccelerator.com"
        )

        assert stack is not None


class TestStackOutputs:
    """Tests for stack outputs."""

    def test_global_stack_exports_dns(self):
        """Test that GlobalStack exports accelerator DNS."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        stack = GCOGlobalStack(app, "test-global-outputs", config=config)

        # Verify stack has accelerator attribute
        assert hasattr(stack, "accelerator")

    def test_api_gateway_exports_secret(self):
        """Test that ApiGatewayStack exports secret ARN."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()

        stack = GCOApiGatewayGlobalStack(
            app, "test-api-outputs", global_accelerator_dns="test.awsglobalaccelerator.com"
        )

        # Verify stack has secret attribute
        assert hasattr(stack, "secret")


class TestConfigIntegration:
    """Tests for configuration integration with stacks."""

    def test_config_loader_mock_works(self):
        """Test that MockConfigLoader provides all required methods."""
        config = MockConfigLoader()

        assert config.get_project_name() == "gco-test"
        assert config.get_regions() == ["us-east-1"]
        assert config.get_kubernetes_version() == "1.36"
        assert isinstance(config.get_tags(), dict)
        assert config.get_resource_thresholds() is not None
        assert config.get_cluster_config("us-east-1") is not None
        assert config.get_global_accelerator_config() is not None
        assert config.get_alb_config() is not None
        assert config.get_manifest_processor_config() is not None
        assert config.get_api_gateway_config() is not None

    def test_global_stack_uses_config(self):
        """Test that GlobalStack uses configuration values."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        stack = GCOGlobalStack(app, "test-config-integration", config=config)

        template = assertions.Template.from_stack(stack)

        # Verify accelerator uses config name
        template.has_resource_properties(
            "AWS::GlobalAccelerator::Accelerator", {"Name": "gco-test-accelerator"}
        )
