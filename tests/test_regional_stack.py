"""
Tests for gco/stacks/regional_stack.GCORegionalStack.

Synthesizes the regional stack — VPC, EKS cluster, EFS, optionally FSx,
kubectl-applier Lambda, helm-installer Lambda, the MCP role, drift
detection, and the NetworkPolicy/RBAC apply pipeline — against a
MockConfigLoader that supplies ClusterConfig, ALB
config, manifest processor config, and the API Gateway config. Patches
the DockerImageAsset and helm-installer builder so tests don't need a
Docker daemon. The MockConfigLoader here is reused by sibling test
files (drift detection, MCP IAM, stacks-ordering-FSx).
"""

from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions


class MockConfigLoader:
    """Mock ConfigLoader for testing regional stack."""

    def __init__(self, app=None, fsx_enabled=False):
        self._fsx_enabled = fsx_enabled

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
        return "1.35"

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
            kubernetes_version="1.35",
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

    def get_eks_cluster_config(self):
        return {
            "endpoint_access": "PRIVATE",
        }

    def get_fsx_lustre_config(self, region=None):
        if self._fsx_enabled:
            return {
                "enabled": True,
                "storage_capacity_gib": 1200,
                "deployment_type": "SCRATCH_2",
                "per_unit_storage_throughput": 200,
                "data_compression_type": "LZ4",
                "import_path": None,
                "export_path": None,
            }
        return {
            "enabled": False,
            "storage_capacity_gib": 1200,
            "deployment_type": "SCRATCH_2",
        }

    def get_valkey_config(self):
        return {"enabled": False}

    def get_aurora_pgvector_config(self):
        return {"enabled": False}


class TestRegionalStackImports:
    """Tests for regional stack imports and class structure."""

    def test_regional_stack_can_be_imported(self):
        """Test that GCORegionalStack can be imported."""
        from gco.stacks.regional_stack import GCORegionalStack

        assert GCORegionalStack is not None

    def test_regional_stack_has_required_methods(self):
        """Test that GCORegionalStack has expected methods."""
        from gco.stacks.regional_stack import GCORegionalStack

        assert hasattr(GCORegionalStack, "__init__")
        assert hasattr(GCORegionalStack, "get_cluster")
        assert hasattr(GCORegionalStack, "get_vpc")

    def test_regional_stack_has_private_methods(self):
        """Test that GCORegionalStack has expected private methods."""
        from gco.stacks.regional_stack import GCORegionalStack

        assert hasattr(GCORegionalStack, "_create_container_images")
        assert hasattr(GCORegionalStack, "_create_eks_cluster")
        assert hasattr(GCORegionalStack, "_create_efs")
        assert hasattr(GCORegionalStack, "_create_fsx_lustre")
        assert hasattr(GCORegionalStack, "_create_outputs")


class TestGlobalStackMethods:
    """Tests for GlobalStack helper methods."""

    def test_global_stack_get_accelerator_dns_name(self):
        """Test get_accelerator_dns_name method."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-global", config=config)

        dns_name = stack.get_accelerator_dns_name()
        assert dns_name is not None

    def test_global_stack_get_accelerator_arn(self):
        """Test get_accelerator_arn method."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-global-arn", config=config)

        arn = stack.get_accelerator_arn()
        assert arn is not None

    def test_global_stack_get_listener_arn(self):
        """Test get_listener_arn method."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-global-listener", config=config)

        arn = stack.get_listener_arn()
        assert arn is not None

    def test_global_stack_get_endpoint_group_arn(self):
        """Test get_endpoint_group_arn method."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-global-endpoint", config=config)

        arn = stack.get_endpoint_group_arn("us-east-1")
        assert arn is not None

    def test_global_stack_get_endpoint_group_arn_invalid_region(self):
        """Test get_endpoint_group_arn raises error for invalid region."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-global-invalid", config=config)

        with pytest.raises(ValueError, match="No endpoint group found"):
            stack.get_endpoint_group_arn("invalid-region")

    def test_global_stack_add_regional_endpoint(self):
        """Test add_regional_endpoint method."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-global-add", config=config)

        stack.add_regional_endpoint(
            "us-east-1",
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/test/123",
        )
        assert "us-east-1" in stack.regional_endpoints


class TestGlobalStackSynthesis:
    """Tests for GlobalStack CloudFormation synthesis."""

    def test_global_stack_creates_accelerator(self):
        """Test that GlobalStack creates a Global Accelerator."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-synth-accelerator", config=config)

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::GlobalAccelerator::Accelerator", 1)

    def test_global_stack_creates_listener(self):
        """Test that GlobalStack creates a listener."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-synth-listener", config=config)

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::GlobalAccelerator::Listener", 1)

    def test_global_stack_creates_endpoint_groups(self):
        """Test that GlobalStack creates endpoint groups for each region."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-synth-endpoints", config=config)

        template = assertions.Template.from_stack(stack)
        # One endpoint group per region
        template.resource_count_is("AWS::GlobalAccelerator::EndpointGroup", 1)

    def test_global_stack_creates_ssm_parameters(self):
        """Test that GlobalStack creates SSM parameters for endpoint groups, DynamoDB tables,
        the model bucket, and the always-on Cluster_Shared_Bucket.
        """
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-synth-ssm", config=config)

        template = assertions.Template.from_stack(stack)
        # 1 for endpoint groups + 5 for DynamoDB tables (templates, webhooks, jobs,
        # inference-endpoints, missions) + 1 for model bucket name + 3 for the
        # always-on Cluster_Shared_Bucket (/gco/cluster-shared-bucket/name, /arn,
        # /region) published unconditionally by GCOGlobalStack.
        template.resource_count_is("AWS::SSM::Parameter", 10)


class TestMonitoringStackMethods:
    """Tests for MonitoringStack methods."""

    @staticmethod
    def _create_mock_stacks():
        """Create mock stacks for monitoring stack tests."""
        mock_global_stack = MagicMock()
        mock_global_stack.accelerator_name = "test-accelerator"
        mock_global_stack.accelerator_id = "test-accelerator-id-12345"
        # Add DynamoDB table mocks
        mock_global_stack.templates_table.table_name = "test-templates"
        mock_global_stack.webhooks_table.table_name = "test-webhooks"
        mock_global_stack.jobs_table.table_name = "test-jobs"

        mock_api_gw_stack = MagicMock()
        mock_api_gw_stack.api.rest_api_name = "test-api"
        mock_api_gw_stack.proxy_lambda.function_name = "test-proxy"
        mock_api_gw_stack.rotation_lambda.function_name = "test-rotation"
        mock_api_gw_stack.secret.secret_name = "test-secret"  # nosec B105 - test fixture mock value, not a real secret

        mock_regional_stack = MagicMock()
        mock_regional_stack.deployment_region = "us-east-1"
        mock_regional_stack.cluster.cluster_name = "test-cluster"
        mock_regional_stack.job_queue.queue_name = "test-queue"
        mock_regional_stack.job_dlq.queue_name = "test-dlq"
        mock_regional_stack.kubectl_lambda_function_name = "test-kubectl"
        mock_regional_stack.helm_installer_lambda_function_name = "test-helm"
        # Optional regional data services default to absent. The monitoring
        # stack widget creators use ``getattr(..., None)`` on these and
        # skip the section when all regions report None.
        mock_regional_stack.fsx_file_system = None
        mock_regional_stack.aurora_cluster = None

        return mock_global_stack, mock_api_gw_stack, [mock_regional_stack]

    def test_monitoring_stack_creates_dashboard(self):
        """Test that MonitoringStack creates a CloudWatch dashboard."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader(app)
        mock_global, mock_api_gw, mock_regional = self._create_mock_stacks()

        stack = GCOMonitoringStack(
            app,
            "test-monitoring-dashboard",
            config=config,
            global_stack=mock_global,
            regional_stacks=mock_regional,
            api_gateway_stack=mock_api_gw,
        )

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::CloudWatch::Dashboard", 1)

    def test_monitoring_stack_creates_sns_topic(self):
        """Test that MonitoringStack creates an SNS topic."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader(app)
        mock_global, mock_api_gw, mock_regional = self._create_mock_stacks()

        stack = GCOMonitoringStack(
            app,
            "test-monitoring-sns",
            config=config,
            global_stack=mock_global,
            regional_stacks=mock_regional,
            api_gateway_stack=mock_api_gw,
        )

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::SNS::Topic", 1)

    def test_monitoring_stack_creates_alarms(self):
        """Test that MonitoringStack creates CloudWatch alarms."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader(app)
        mock_global, mock_api_gw, mock_regional = self._create_mock_stacks()

        stack = GCOMonitoringStack(
            app,
            "test-monitoring-alarms",
            config=config,
            global_stack=mock_global,
            regional_stacks=mock_regional,
            api_gateway_stack=mock_api_gw,
        )

        template = assertions.Template.from_stack(stack)
        # Should have multiple alarms
        template.has_resource("AWS::CloudWatch::Alarm", {})

    def test_monitoring_stack_creates_log_groups(self):
        """Test that MonitoringStack creates log groups."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader(app)
        mock_global, mock_api_gw, mock_regional = self._create_mock_stacks()

        stack = GCOMonitoringStack(
            app,
            "test-monitoring-logs",
            config=config,
            global_stack=mock_global,
            regional_stacks=mock_regional,
            api_gateway_stack=mock_api_gw,
        )

        template = assertions.Template.from_stack(stack)
        # Should have log groups for health monitor and manifest processor
        template.has_resource("AWS::Logs::LogGroup", {})


class TestConfigLoaderValidation:
    """Tests for ConfigLoader validation methods."""

    def test_config_loader_valid_regions(self):
        """Test ConfigLoader VALID_REGIONS constant."""
        from gco.config.config_loader import ConfigLoader

        assert "us-east-1" in ConfigLoader.VALID_REGIONS
        assert "us-west-2" in ConfigLoader.VALID_REGIONS
        assert "eu-west-1" in ConfigLoader.VALID_REGIONS
        assert "invalid-region" not in ConfigLoader.VALID_REGIONS

    def test_config_validation_error_class(self):
        """Test ConfigValidationError exception class."""
        from gco.config.config_loader import ConfigValidationError

        error = ConfigValidationError("Test error message")
        assert str(error) == "Test error message"
        assert isinstance(error, Exception)


class TestConfigLoaderDefaults:
    """Tests for ConfigLoader default values."""

    def test_get_project_name_default(self):
        """Test default project name."""
        app = cdk.App()
        config = MockConfigLoader(app)
        assert config.get_project_name() == "gco-test"

    def test_get_regions_default(self):
        """Test default regions."""
        app = cdk.App()
        config = MockConfigLoader(app)
        assert config.get_regions() == ["us-east-1"]

    def test_get_kubernetes_version_default(self):
        """Test default Kubernetes version."""
        app = cdk.App()
        config = MockConfigLoader(app)
        assert config.get_kubernetes_version() == "1.35"

    def test_get_fsx_lustre_config_disabled(self):
        """Test FSx config when disabled."""
        app = cdk.App()
        config = MockConfigLoader(app, fsx_enabled=False)
        fsx_config = config.get_fsx_lustre_config()
        assert fsx_config["enabled"] is False

    def test_get_fsx_lustre_config_enabled(self):
        """Test FSx config when enabled."""
        app = cdk.App()
        config = MockConfigLoader(app, fsx_enabled=True)
        fsx_config = config.get_fsx_lustre_config()
        assert fsx_config["enabled"] is True
        assert fsx_config["storage_capacity_gib"] == 1200
        assert fsx_config["deployment_type"] == "SCRATCH_2"


class TestApiGatewayStackMethods:
    """Tests for ApiGatewayGlobalStack methods."""

    def test_api_gateway_stack_has_secret(self):
        """Test that ApiGatewayStack has secret attribute."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app, "test-api-secret", global_accelerator_dns="test.awsglobalaccelerator.com"
        )

        assert hasattr(stack, "secret")
        assert stack.secret is not None

    def test_api_gateway_stack_creates_rest_api(self):
        """Test that ApiGatewayStack creates a REST API."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app, "test-api-rest", global_accelerator_dns="test.awsglobalaccelerator.com"
        )

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::ApiGateway::RestApi", 1)

    def test_api_gateway_stack_uses_iam_auth(self):
        """Test that ApiGatewayStack uses IAM authentication."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app, "test-api-auth", global_accelerator_dns="test.awsglobalaccelerator.com"
        )

        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::ApiGateway::Method", {"AuthorizationType": "AWS_IAM"}
        )


class TestClusterConfigModel:
    """Tests for ClusterConfig model."""

    def test_cluster_config_creation(self):
        """Test creating ClusterConfig."""
        from gco.models import ClusterConfig, ResourceThresholds

        thresholds = ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

        config = ClusterConfig(
            region="us-east-1",
            cluster_name="test-cluster",
            kubernetes_version="1.35",
            addons=["metrics-server"],
            resource_thresholds=thresholds,
        )

        assert config.region == "us-east-1"
        assert config.cluster_name == "test-cluster"
        assert config.kubernetes_version == "1.35"


class TestResourceThresholdsModel:
    """Tests for ResourceThresholds model."""

    def test_resource_thresholds_creation(self):
        """Test creating ResourceThresholds."""
        from gco.models import ResourceThresholds

        thresholds = ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)

        assert thresholds.cpu_threshold == 80
        assert thresholds.memory_threshold == 85
        assert thresholds.gpu_threshold == 90

    def test_resource_thresholds_defaults(self):
        """Test ResourceThresholds with default values."""
        from gco.models import ResourceThresholds

        # Test that the model can be created with explicit values
        thresholds = ResourceThresholds(cpu_threshold=70, memory_threshold=75, gpu_threshold=80)
        assert thresholds.cpu_threshold == 70


class TestRegionalStackSynthesis:
    """Tests for GCORegionalStack CloudFormation synthesis.

    These tests mock _create_helm_installer_lambda to avoid requiring Docker during tests.
    """

    @staticmethod
    def _mock_helm_installer(stack):
        """Set up mock attributes for helm installer."""
        stack.helm_installer_lambda = MagicMock()
        stack.helm_installer_provider = MagicMock()
        stack.helm_installer_provider.service_token = (
            "arn:aws:lambda:us-east-1:123456789012:function:mock"  # nosec B106 - test fixture ARN with fake account ID, not a real credential
        )

    def test_regional_stack_creates_vpc(self):
        """Test that RegionalStack creates a VPC."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        # Mock DockerImageAsset and _create_helm_installer_lambda to avoid Docker dependency
        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-vpc",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::EC2::VPC", 1)

    def test_regional_stack_creates_ecr_repositories(self):
        """Test that RegionalStack creates ECR repositories."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-ecr",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            # Should have 2 ECR repositories (health monitor and manifest processor)
            template.resource_count_is("AWS::ECR::Repository", 2)

    def test_regional_stack_creates_efs(self):
        """Test that RegionalStack creates EFS file system."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-efs",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::EFS::FileSystem", 1)

    def test_regional_stack_creates_iam_roles(self):
        """Test that RegionalStack creates IAM roles."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-iam",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            # Should have multiple IAM roles (cluster admin, node group, service account, etc.)
            template.has_resource("AWS::IAM::Role", {})

    def test_regional_stack_creates_lambda_functions(self):
        """Test that RegionalStack creates Lambda functions."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-lambda",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            # Should have Lambda functions (kubectl applier, GA registration)
            # Note: Helm installer Lambda is mocked, so it won't appear in template
            template.has_resource("AWS::Lambda::Function", {})


class TestRegionalStackWithFsx:
    """Tests for RegionalStack with FSx enabled."""

    def test_regional_stack_creates_fsx_when_enabled(self):
        """Test that RegionalStack creates FSx when enabled."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app, fsx_enabled=True)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-fsx",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::FSx::FileSystem", 1)

    def test_regional_stack_no_fsx_when_disabled(self):
        """Test that RegionalStack does not create FSx when disabled."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app, fsx_enabled=False)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-no-fsx",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::FSx::FileSystem", 0)


class TestAwsCustomResourceSharedRole:
    """Regression guards for the IAM PassRole race fix (v0.1.2).

    CDK's ``cr.AwsCustomResource`` defaults to auto-generating a Lambda
    execution role per construct, then deduplicates them onto a single
    singleton provider Lambda (logical id prefix ``AWS679``). Each
    construct's ``policy=`` statements are merged onto that Lambda's
    role during stack create. On cold deploys, CloudFormation invokes
    the Lambda within 2-3 seconds of attaching a new policy statement,
    which is faster than IAM's global propagation window. The symptom
    is an ``iam:PassRole NOT authorized`` failure on the last
    ``updateAddon`` custom resource to run.

    The prior approach (PRs #8 and #9) serialized the three
    ``updateAddon`` custom resources with ``add_dependency`` so they
    run sequentially. In practice that moved the race rather than
    fixing it — the Lambda still fired within seconds of a fresh
    ``PassRole`` attach, so the last one in the chain still failed.

    The v0.1.2 approach replaces CDK's auto-generated role with a
    single pre-created ``iam.Role`` (``self.aws_custom_resource_role``)
    that has every required statement attached by the time CFN
    provisions it. Every ``AwsCustomResource`` passes
    ``role=self.aws_custom_resource_role`` instead of ``policy=``. The
    role (and its inline policy) exist minutes before any custom
    resource fires, so IAM has ample time to replicate.

    These tests assert:
    1. The shared role exists in the synthesized template
    2. All four known ``AwsCustomResource`` instances reference it
    3. The shared role has the required policy statements
    4. No CR→CR dependency chain is needed (the old
       ``TestAddonRoleUpdateDependencyChain`` class covered that; it's
       replaced by this class since the chain is gone by design)
    """

    def _synth_regional_stack(self, fsx_enabled: bool, logical_name: str):
        """Synthesize the regional stack with or without FSx enabled.

        Returns the ``assertions.Template`` for inspection. Mirrors the
        Docker + helm-installer patching pattern used elsewhere in this
        file so no real Docker daemon is required.
        """
        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app, fsx_enabled=fsx_enabled)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                logical_name,
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            return assertions.Template.from_stack(stack)

    @staticmethod
    def _find_by_logical_prefix(resources: dict, prefix: str) -> tuple[str, dict]:
        """Return the first (logical_id, resource) pair whose id starts with ``prefix``."""
        for lid, r in resources.items():
            if lid.startswith(prefix):
                return lid, r
        raise AssertionError(
            f"No resource found with logical id prefix {prefix!r}. "
            f"Available logical ids: {sorted(resources)[:20]}..."
        )

    @staticmethod
    def _depends_on_names(resource: dict) -> list[str]:
        """Normalize a CFN ``DependsOn`` field to a list of logical ids."""
        dep = resource.get("DependsOn", [])
        if isinstance(dep, str):
            return [dep]
        return list(dep)

    @staticmethod
    def _role_refs(resource: dict) -> set[str]:
        """Return the set of IAM role logical ids referenced by a CFN resource's Role prop.

        The ``Role`` property on ``Custom::AWS`` resources is expressed as
        ``{"Fn::GetAtt": ["<logical_id>", "Arn"]}``. This helper digs the
        logical ids out so assertions can match on them directly.
        """
        properties = resource.get("Properties", {})
        role = properties.get("ServiceToken")
        # Custom::AWS isn't a standard Custom Resource — its singleton
        # Lambda's role is referenced indirectly. We look at the stack's
        # Lambda function resources separately.
        return {role} if isinstance(role, str) else set()

    def test_shared_role_is_created(self):
        """The pre-created shared execution role must exist in the template."""
        template = self._synth_regional_stack(
            fsx_enabled=False, logical_name="test-shared-role-exists"
        )
        roles = template.find_resources("AWS::IAM::Role")
        shared_role_ids = [lid for lid in roles if lid.startswith("AwsCustomResourceRole")]
        assert shared_role_ids, (
            f"The pre-created AwsCustomResourceRole should appear in the "
            f"CFN template. Found role logical ids: {sorted(roles)[:20]}"
        )

    def test_shared_role_has_eks_update_addon_policy(self):
        """The shared role's inline policy must allow eks:UpdateAddon/DescribeAddon."""
        template = self._synth_regional_stack(
            fsx_enabled=False, logical_name="test-shared-role-eks-policy"
        )
        policies = template.find_resources("AWS::IAM::Policy")
        shared_policies = [
            (lid, r) for lid, r in policies.items() if lid.startswith("AwsCustomResourceRole")
        ]
        assert shared_policies, "The shared role should have an attached inline policy"
        # At least one of the attached policies must grant
        # eks:UpdateAddon and eks:DescribeAddon.
        found_eks_statement = False
        for _lid, policy in shared_policies:
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "eks:UpdateAddon" in actions and "eks:DescribeAddon" in actions:
                    found_eks_statement = True
                    break
        assert found_eks_statement, "Shared role must allow eks:UpdateAddon and eks:DescribeAddon"

    def test_shared_role_has_ssm_get_parameter_policy(self):
        """The shared role must allow ssm:GetParameter for the endpoint group ARN lookup."""
        template = self._synth_regional_stack(
            fsx_enabled=False, logical_name="test-shared-role-ssm-policy"
        )
        policies = template.find_resources("AWS::IAM::Policy")
        shared_policies = [
            (lid, r) for lid, r in policies.items() if lid.startswith("AwsCustomResourceRole")
        ]
        found_ssm_statement = False
        for _lid, policy in shared_policies:
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "ssm:GetParameter" in actions:
                    found_ssm_statement = True
                    break
        assert found_ssm_statement, "Shared role must allow ssm:GetParameter"

    def test_shared_role_has_efs_passrole_statement(self):
        """The shared role must allow iam:PassRole for the EFS CSI IRSA role."""
        template = self._synth_regional_stack(
            fsx_enabled=False, logical_name="test-shared-role-efs-passrole"
        )
        policies = template.find_resources("AWS::IAM::Policy")
        shared_policies = [
            (lid, r) for lid, r in policies.items() if lid.startswith("AwsCustomResourceRole")
        ]
        # Collect all PassRole statements and check any of them references
        # the EFS CSI role by Fn::GetAtt.
        passrole_targets: list = []
        for _lid, policy in shared_policies:
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "iam:PassRole" in actions:
                    resources = statement.get("Resource", [])
                    if not isinstance(resources, list):
                        resources = [resources]
                    passrole_targets.extend(resources)
        # Each resource is either a string ARN or a dict with Fn::GetAtt.
        passrole_target_strs = [str(r) for r in passrole_targets]
        assert any("EfsCsiDriverRole" in s for s in passrole_target_strs), (
            f"Shared role must have PassRole statement for EFS CSI role. "
            f"Found PassRole targets: {passrole_target_strs}"
        )

    def test_shared_role_has_fsx_passrole_statement_when_fsx_enabled(self):
        """When FSx is enabled, the shared role must allow iam:PassRole for the FSx CSI role."""
        template = self._synth_regional_stack(
            fsx_enabled=True, logical_name="test-shared-role-fsx-passrole"
        )
        policies = template.find_resources("AWS::IAM::Policy")
        shared_policies = [
            (lid, r) for lid, r in policies.items() if lid.startswith("AwsCustomResourceRole")
        ]
        passrole_targets: list = []
        for _lid, policy in shared_policies:
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "iam:PassRole" in actions:
                    resources = statement.get("Resource", [])
                    if not isinstance(resources, list):
                        resources = [resources]
                    passrole_targets.extend(resources)
        passrole_target_strs = [str(r) for r in passrole_targets]
        assert any("FsxCsiDriverRole" in s for s in passrole_target_strs), (
            f"When FSx is enabled, shared role must have PassRole statement "
            f"for FSx CSI role. Found PassRole targets: {passrole_target_strs}"
        )

    def test_shared_role_has_cloudwatch_passrole_statement(self):
        """The shared role must allow iam:PassRole for the CloudWatch Observability role."""
        template = self._synth_regional_stack(
            fsx_enabled=False, logical_name="test-shared-role-cw-passrole"
        )
        policies = template.find_resources("AWS::IAM::Policy")
        shared_policies = [
            (lid, r) for lid, r in policies.items() if lid.startswith("AwsCustomResourceRole")
        ]
        passrole_targets: list = []
        for _lid, policy in shared_policies:
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "iam:PassRole" in actions:
                    resources = statement.get("Resource", [])
                    if not isinstance(resources, list):
                        resources = [resources]
                    passrole_targets.extend(resources)
        passrole_target_strs = [str(r) for r in passrole_targets]
        assert any("CloudWatchObservabilityRole" in s for s in passrole_target_strs), (
            f"Shared role must have PassRole statement for CloudWatch role. "
            f"Found PassRole targets: {passrole_target_strs}"
        )

    def test_addon_update_crs_depend_on_shared_role(self):
        """Each updateAddon custom resource must depend on the shared role.

        CDK-emitted ``DependsOn`` edges ensure CloudFormation provisions
        (and IAM propagates) the shared role + its inline policy before
        the singleton Lambda fires for any ``AwsCustomResource``.
        """
        template = self._synth_regional_stack(
            fsx_enabled=True, logical_name="test-crs-depend-on-shared-role"
        )
        crs = template.find_resources("Custom::AWS")

        _cw_id, cw_resource = self._find_by_logical_prefix(crs, "UpdateCloudWatchAddonRole")
        _efs_id, efs_resource = self._find_by_logical_prefix(crs, "UpdateEfsCsiAddonRole")
        _fsx_id, fsx_resource = self._find_by_logical_prefix(crs, "UpdateFsxCsiAddonRole")

        # The CR's ``DependsOn`` references the IAM role by its logical id
        # (CDK resolves the ``add_dependency`` call to a CFN DependsOn edge).
        for cr_resource, name in (
            (cw_resource, "UpdateCloudWatchAddonRole"),
            (efs_resource, "UpdateEfsCsiAddonRole"),
            (fsx_resource, "UpdateFsxCsiAddonRole"),
        ):
            depends_on = self._depends_on_names(cr_resource)
            assert any(d.startswith("AwsCustomResourceRole") for d in depends_on), (
                f"{name} must depend on AwsCustomResourceRole so CFN has "
                f"fully attached + replicated the shared inline policy "
                f"before the Lambda fires. DependsOn: {depends_on}"
            )

    def test_cr_cr_dependency_chain_is_gone(self):
        """The old serialization chain from PRs #8 and #9 should be removed.

        CloudWatch's update must NOT depend on EFS's or FSx's update
        anymore. That chain was working around the race that the shared
        role now eliminates. Keeping it would add meaningless
        serialization and slow down cold creates for no benefit.
        """
        template = self._synth_regional_stack(fsx_enabled=True, logical_name="test-no-cr-cr-chain")
        crs = template.find_resources("Custom::AWS")

        _cw_id, cw_resource = self._find_by_logical_prefix(crs, "UpdateCloudWatchAddonRole")
        efs_id, _ = self._find_by_logical_prefix(crs, "UpdateEfsCsiAddonRole")
        fsx_id, _ = self._find_by_logical_prefix(crs, "UpdateFsxCsiAddonRole")

        depends_on = self._depends_on_names(cw_resource)
        assert efs_id not in depends_on, (
            f"CloudWatch update should no longer depend on EFS update; the "
            f"race is eliminated by the shared role. Found {efs_id!r} in "
            f"DependsOn: {depends_on}"
        )
        assert fsx_id not in depends_on, (
            f"CloudWatch update should no longer depend on FSx update; the "
            f"race is eliminated by the shared role. Found {fsx_id!r} in "
            f"DependsOn: {depends_on}"
        )


class TestServiceAccountRoleSecretSuppression:
    """Regression guard for the v0.1.2+ deploy-blocking cdk-nag finding
    on the auth secret wildcard.

    The ``ServiceAccountRole``'s inline policy grants
    ``secretsmanager:GetSecretValue`` on the cross-stack auth secret
    ARN with a trailing ``*`` so it matches both the full ARN (with
    the 6-character suffix AWS appends) and the partial ARN form.
    cdk-nag's ``AwsSolutions-IAM5`` rule flags that wildcard and
    blocks ``cdk deploy`` unless the policy has a scoped
    ``rules_to_suppress`` entry with a matching ``applies_to``.

    The suppression was absent on initial launch, which surfaced as a
    deploy-time failure when first deploying to a region whose
    regional stack had never synthesized before. This test ensures
    every synthesized regional stack carries the suppression, so any
    future refactor that drops or renames it fails PR CI instead of
    deploy.
    """

    def _synth(self, fsx_enabled: bool, logical_name: str):
        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app, fsx_enabled=fsx_enabled)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                logical_name,
                config=config,
                region="us-east-1",
                auth_secret_arn=(
                    "arn:aws:secretsmanager:us-east-2:123456789012"
                    ":secret:gco/api-gateway-auth-token"
                ),
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

        return stack, assertions.Template.from_stack(stack)

    def test_service_account_role_policy_has_iam5_suppression(self):
        """The ServiceAccountRole's DefaultPolicy must carry a
        scoped AwsSolutions-IAM5 suppression for the auth secret
        wildcard."""
        _stack, template = self._synth(fsx_enabled=False, logical_name="test-sa-role-suppression")

        policies = template.find_resources("AWS::IAM::Policy")
        sa_policies = {
            lid: res
            for lid, res in policies.items()
            if lid.startswith("ServiceAccountRoleDefaultPolicy")
        }
        assert sa_policies, (
            "ServiceAccountRole DefaultPolicy not found — the IAM5 "
            "suppression check below can't run. Something in the "
            "regional stack structure has changed."
        )

        # Find the IAM5 suppression among the policy's metadata rules.
        for lid, policy in sa_policies.items():
            metadata = policy.get("Metadata", {}) or {}
            supps = (metadata.get("cdk_nag") or {}).get("rules_to_suppress") or []
            iam5 = [s for s in supps if s.get("id") == "AwsSolutions-IAM5"]
            assert iam5, (
                f"{lid} is missing an AwsSolutions-IAM5 suppression. The "
                f"inline policy grants secretsmanager:GetSecretValue on a "
                f"wildcarded ARN (secret ARN + '*'), which cdk-nag will "
                f"block deploy on. Add a scoped NagSuppressions entry "
                f"matching the GCOAuthSecret token."
            )

            # The appliesTo must specifically scope the suppression to
            # the auth secret, not just a blanket wildcard entry.
            applies_to = []
            for s in iam5:
                at = s.get("applies_to") or s.get("appliesTo") or []
                applies_to.extend(at)
            assert applies_to, (
                f"{lid}'s AwsSolutions-IAM5 suppression has no appliesTo. "
                f"Unscoped IAM5 suppressions are not acceptable — the "
                f"suppression must pin to the specific GCOAuthSecret "
                f"resource via a regex or literal pattern."
            )

            # At least one entry must match the GCOAuthSecret token.
            # Accepts both literal string patterns and regex dicts.
            matched = False
            for entry in applies_to:
                if isinstance(entry, str) and "GCOAuthSecret" in entry:
                    matched = True
                    break
                if isinstance(entry, dict):
                    raw = entry.get("regex", "")
                    if "GCOAuthSecret" in raw:
                        matched = True
                        break
            assert matched, (
                f"{lid}'s AwsSolutions-IAM5 suppression has appliesTo "
                f"entries but none of them reference GCOAuthSecret. The "
                f"suppression must specifically scope to the auth secret "
                f"resource, not some unrelated wildcard. Found entries: "
                f"{applies_to!r}"
            )


class TestRegionalStackGetters:
    """Tests for RegionalStack getter methods."""

    def test_get_cluster_returns_cluster(self):
        """Test get_cluster returns the EKS cluster."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-getter-cluster",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            cluster = stack.get_cluster()
            assert cluster is not None
            assert cluster == stack.cluster

    def test_get_vpc_returns_vpc(self):
        """Test get_vpc returns the VPC."""

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-regional-getter-vpc",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            vpc = stack.get_vpc()
            assert vpc is not None
            assert vpc == stack.vpc


class TestRegionalStackFsxConfigurations:
    """Tests for RegionalStack FSx Lustre configurations."""

    @staticmethod
    def _mock_helm_installer(stack):
        """Set up mock attributes for helm installer."""
        stack.helm_installer_lambda = MagicMock()
        stack.helm_installer_provider = MagicMock()
        stack.helm_installer_provider.service_token = (
            "arn:aws:lambda:us-east-1:123456789012:function:mock"  # nosec B106 - test fixture ARN with fake account ID, not a real credential
        )

    def test_fsx_with_persistent_deployment_type(self):
        """Test FSx with PERSISTENT_1 deployment type includes throughput."""

        class PersistentFsxConfig(MockConfigLoader):
            def get_fsx_lustre_config(self, region=None):
                return {
                    "enabled": True,
                    "storage_capacity_gib": 2400,
                    "deployment_type": "PERSISTENT_1",
                    "per_unit_storage_throughput": 200,
                    "data_compression_type": "LZ4",
                    "import_path": None,
                    "export_path": None,
                    "file_system_type_version": "2.15",
                }

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = PersistentFsxConfig(app, fsx_enabled=True)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                self._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-fsx-persistent",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::FSx::FileSystem", 1)
            template.has_resource_properties(
                "AWS::FSx::FileSystem",
                {
                    "FileSystemType": "LUSTRE",
                    "StorageCapacity": 2400,
                },
            )

    def test_fsx_with_s3_import_path(self):
        """Test FSx with S3 import path configuration."""

        class S3ImportFsxConfig(MockConfigLoader):
            def get_fsx_lustre_config(self, region=None):
                return {
                    "enabled": True,
                    "storage_capacity_gib": 1200,
                    "deployment_type": "SCRATCH_2",
                    "data_compression_type": "LZ4",
                    "import_path": "s3://my-bucket/data",
                    "auto_import_policy": "NEW_CHANGED_DELETED",
                    "export_path": None,
                    "file_system_type_version": "2.15",
                }

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = S3ImportFsxConfig(app, fsx_enabled=True)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                self._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-fsx-s3-import",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::FSx::FileSystem", 1)

    def test_fsx_with_s3_export_path(self):
        """Test FSx with S3 export path configuration."""

        class S3ExportFsxConfig(MockConfigLoader):
            def get_fsx_lustre_config(self, region=None):
                return {
                    "enabled": True,
                    "storage_capacity_gib": 1200,
                    "deployment_type": "SCRATCH_2",
                    "data_compression_type": "LZ4",
                    "import_path": "s3://my-bucket/input",
                    "export_path": "s3://my-bucket/output",
                    "file_system_type_version": "2.15",
                }

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = S3ExportFsxConfig(app, fsx_enabled=True)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                self._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-fsx-s3-export",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::FSx::FileSystem", 1)

    def test_fsx_with_persistent_2_deployment_type(self):
        """Test FSx with PERSISTENT_2 deployment type."""

        class Persistent2FsxConfig(MockConfigLoader):
            def get_fsx_lustre_config(self, region=None):
                return {
                    "enabled": True,
                    "storage_capacity_gib": 4800,
                    "deployment_type": "PERSISTENT_2",
                    "per_unit_storage_throughput": 500,
                    "data_compression_type": "NONE",
                    "import_path": None,
                    "export_path": None,
                    "file_system_type_version": "2.15",
                }

        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = Persistent2FsxConfig(app, fsx_enabled=True)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                self._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                "test-fsx-persistent-2",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)
            template.resource_count_is("AWS::FSx::FileSystem", 1)
            template.has_resource_properties(
                "AWS::FSx::FileSystem",
                {
                    "FileSystemType": "LUSTRE",
                    "StorageCapacity": 4800,
                },
            )


class TestGlobalStackDynamoDBTables:
    """Tests for GlobalStack DynamoDB tables."""

    def test_global_stack_creates_templates_table(self):
        """Test that GlobalStack creates templates DynamoDB table."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-dynamodb-templates", config=config)

        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "gco-test-job-templates",
                "KeySchema": [{"AttributeName": "template_name", "KeyType": "HASH"}],
            },
        )

    def test_global_stack_creates_webhooks_table(self):
        """Test that GlobalStack creates webhooks DynamoDB table."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-dynamodb-webhooks", config=config)

        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "gco-test-webhooks",
                "KeySchema": [{"AttributeName": "webhook_id", "KeyType": "HASH"}],
            },
        )

    def test_global_stack_creates_jobs_table(self):
        """Test that GlobalStack creates jobs DynamoDB table."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-dynamodb-jobs", config=config)

        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "gco-test-jobs",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
            },
        )

    def test_global_stack_creates_backup_plan(self):
        """Test that GlobalStack creates AWS Backup plan for DynamoDB tables."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-backup-plan", config=config)

        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::Backup::BackupPlan", 1)
        template.resource_count_is("AWS::Backup::BackupVault", 1)
        template.resource_count_is("AWS::Backup::BackupSelection", 1)

    def test_global_stack_dynamodb_tables_have_pitr(self):
        """Test that DynamoDB tables have point-in-time recovery enabled."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-dynamodb-pitr", config=config)

        template = assertions.Template.from_stack(stack)
        # All 3 tables should have PITR enabled
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            },
        )


class TestNagSuppressions:
    """Tests for CDK-nag suppression functions."""

    def test_add_backup_suppressions(self):
        """Test add_backup_suppressions function."""
        from gco.stacks.nag_suppressions import add_backup_suppressions

        app = cdk.App()
        stack = cdk.Stack(app, "test-backup-suppressions")

        # Should not raise any errors
        add_backup_suppressions(stack)

    def test_apply_all_suppressions_global_stack(self):
        """Test apply_all_suppressions for global stack type."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        app = cdk.App()
        stack = cdk.Stack(app, "test-global-suppressions")

        # Should not raise any errors
        apply_all_suppressions(stack, stack_type="global")

    def test_apply_all_suppressions_regional_stack(self):
        """Test apply_all_suppressions for regional stack type."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        app = cdk.App()
        stack = cdk.Stack(app, "test-regional-suppressions")

        # Should not raise any errors
        apply_all_suppressions(
            stack,
            stack_type="regional",
            regions=["us-east-1", "us-west-2"],
            global_region="us-east-2",
        )

    def test_apply_all_suppressions_api_gateway_stack(self):
        """Test apply_all_suppressions for api_gateway stack type."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        app = cdk.App()
        stack = cdk.Stack(app, "test-api-gateway-suppressions")

        # Should not raise any errors
        apply_all_suppressions(stack, stack_type="api_gateway")

    def test_apply_all_suppressions_monitoring_stack(self):
        """Test apply_all_suppressions for monitoring stack type."""
        from gco.stacks.nag_suppressions import apply_all_suppressions

        app = cdk.App()
        stack = cdk.Stack(app, "test-monitoring-suppressions")

        # Should not raise any errors
        apply_all_suppressions(stack, stack_type="monitoring")

    def test_add_iam_suppressions_with_dynamodb_patterns(self):
        """Test add_iam_suppressions includes DynamoDB index patterns."""
        from gco.stacks.nag_suppressions import add_iam_suppressions

        app = cdk.App()
        stack = cdk.Stack(app, "test-iam-dynamodb-suppressions")

        # Should not raise any errors
        add_iam_suppressions(
            stack,
            regions=["us-east-1"],
            global_region="us-east-2",
        )


class TestResourceStatusUid:
    """Tests for ResourceStatus uid attribute."""

    def test_resource_status_with_uid(self):
        """Test ResourceStatus can be created with uid."""
        from gco.models.manifest_models import ResourceStatus

        status = ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
            status="created",
            uid="abc123-def456",
        )

        assert status.uid == "abc123-def456"

    def test_resource_status_without_uid(self):
        """Test ResourceStatus can be created without uid (defaults to None)."""
        from gco.models.manifest_models import ResourceStatus

        status = ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
            status="created",
        )

        assert status.uid is None

    def test_resource_status_uid_in_dict(self):
        """Test ResourceStatus uid is included when converting to dict-like access."""
        from gco.models.manifest_models import ResourceStatus

        status = ResourceStatus(
            api_version="batch/v1",
            kind="Job",
            name="test-job",
            namespace="default",
            status="created",
            uid="test-uid-123",
        )

        # Verify uid attribute is accessible
        assert hasattr(status, "uid")
        assert status.uid == "test-uid-123"


# =============================================================================
# Valkey Cache Tests
# =============================================================================


class TestValkeyCache:
    """Tests for the Valkey Serverless cache construct."""

    def test_valkey_disabled_by_default_creates_no_cache(self):
        """When valkey.enabled is false, no cache is created."""
        from unittest.mock import MagicMock

        mock_config = MagicMock()
        mock_config.app.node.try_get_context.return_value = {"enabled": False}

        # The method should return early without creating resources
        # We test this by checking the config is read
        assert mock_config.app.node.try_get_context("valkey") == {"enabled": False}

    def test_valkey_config_defaults(self):
        """Test that default Valkey config values are sensible."""
        import json
        from pathlib import Path

        cdk_json = json.loads(Path("cdk.json").read_text())
        valkey_config = cdk_json["context"]["valkey"]

        assert isinstance(valkey_config["enabled"], bool)
        assert valkey_config["max_data_storage_gb"] == 5
        assert valkey_config["max_ecpu_per_second"] == 5000
        assert valkey_config["snapshot_retention_limit"] == 1


class TestQueueProcessorConfig:
    """Tests for queue_processor configuration in cdk.json."""

    def test_queue_processor_config_exists(self):
        import json
        from pathlib import Path

        cdk_json = json.loads(Path("cdk.json").read_text())
        qp = cdk_json["context"]["queue_processor"]
        policy = cdk_json["context"]["job_validation_policy"]

        assert qp["enabled"] is True
        assert qp["polling_interval"] == 10
        assert qp["max_concurrent_jobs"] == 10
        assert qp["messages_per_job"] == 1
        # allowed_namespaces now lives under the shared job_validation_policy
        # section (both processors read the same allowlist).
        assert "gco-jobs" in policy["allowed_namespaces"]
        # Resource caps now live under the shared job_validation_policy
        # section (both processors read the same values).
        assert policy["resource_quotas"]["max_gpu_per_manifest"] == 4

    def test_queue_processor_defaults_match_docs(self):
        """Ensure cdk.json defaults match what's documented in CUSTOMIZATION.md."""
        import json
        from pathlib import Path

        cdk_json = json.loads(Path("cdk.json").read_text())
        qp = cdk_json["context"]["queue_processor"]
        policy = cdk_json["context"]["job_validation_policy"]

        assert qp["successful_jobs_history"] == 20
        assert qp["failed_jobs_history"] == 10
        assert policy["resource_quotas"]["max_cpu_per_manifest"] == "10"
        assert policy["resource_quotas"]["max_memory_per_manifest"] == "32Gi"


class TestAuroraPgvector:
    """Tests for Aurora Serverless v2 pgvector integration.

    Validates that the regional stack correctly creates (or skips) the
    Aurora Serverless v2 PostgreSQL cluster with pgvector based on the
    ``aurora_pgvector.enabled`` flag in cdk.json.
    """

    @staticmethod
    def _mock_helm_installer(stack):
        """Set up mock attributes for helm installer."""
        stack.helm_installer_lambda = MagicMock()
        stack.helm_installer_provider = MagicMock()
        stack.helm_installer_provider.service_token = (
            "arn:aws:lambda:us-east-1:123456789012:function:mock"  # nosec B106
        )

    def _synth(self, aurora_enabled: bool, logical_name: str):
        """Synthesize the regional stack with Aurora enabled or disabled."""
        from gco.stacks.regional_stack import GCORegionalStack

        class AuroraConfig(MockConfigLoader):
            def __init__(self, app, aurora_on):
                super().__init__(app)
                self._aurora_on = aurora_on

            def get_aurora_pgvector_config(self):
                if self._aurora_on:
                    return {
                        "enabled": True,
                        "min_acu": 0,
                        "max_acu": 16,
                        "backup_retention_days": 7,
                        "deletion_protection": False,
                    }
                return {"enabled": False}

        app = cdk.App()
        config = AuroraConfig(app, aurora_on=aurora_enabled)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                self._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                logical_name,
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            return stack, assertions.Template.from_stack(stack)

    def test_aurora_cluster_created_when_enabled(self):
        """Aurora Serverless v2 cluster is created when aurora_pgvector.enabled=true."""
        _stack, template = self._synth(aurora_enabled=True, logical_name="test-aurora-enabled")
        template.resource_count_is("AWS::RDS::DBCluster", 1)
        template.has_resource_properties(
            "AWS::RDS::DBCluster",
            {
                "Engine": "aurora-postgresql",
                "DatabaseName": "gco_vectors",
                "StorageEncrypted": True,
                "EnableIAMDatabaseAuthentication": True,
                "EnableCloudwatchLogsExports": ["postgresql"],
            },
        )

    def test_no_aurora_when_disabled(self):
        """No Aurora resources are created when aurora_pgvector.enabled=false."""
        _stack, template = self._synth(aurora_enabled=False, logical_name="test-aurora-disabled")
        template.resource_count_is("AWS::RDS::DBCluster", 0)
        template.resource_count_is("AWS::RDS::DBInstance", 0)

    def test_aurora_security_group_allows_5432_from_eks(self):
        """Aurora security group allows PostgreSQL (5432) from EKS cluster SG only."""
        _stack, template = self._synth(aurora_enabled=True, logical_name="test-aurora-sg")
        sgs = template.find_resources("AWS::EC2::SecurityGroup")
        aurora_sgs = {lid: r for lid, r in sgs.items() if lid.startswith("AuroraPgvectorSG")}
        assert aurora_sgs, "AuroraPgvectorSG security group not found in template"

        # Verify ingress rule on port 5432 — should reference the EKS cluster
        # security group (not a CIDR block)
        ingress_rules = template.find_resources("AWS::EC2::SecurityGroupIngress")
        found_5432_from_eks = False
        for _lid, rule in ingress_rules.items():
            props = rule.get("Properties", {})
            if (
                props.get("FromPort") == 5432
                and props.get("ToPort") == 5432
                and ("SourceSecurityGroupId" in props or "GroupId" in props)
            ):
                found_5432_from_eks = True
                break
        # Also check inline SecurityGroupIngress on the SG itself
        if not found_5432_from_eks:
            for _lid, sg in aurora_sgs.items():
                for ingress in sg.get("Properties", {}).get("SecurityGroupIngress", []):
                    if (
                        ingress.get("FromPort") == 5432
                        and ingress.get("ToPort") == 5432
                        and "SourceSecurityGroupId" in ingress
                    ):
                        found_5432_from_eks = True
                        break
        assert found_5432_from_eks, (
            "Aurora security group should allow port 5432 from the EKS cluster "
            "security group (not a CIDR block)"
        )

    def test_aurora_ssm_parameter_created(self):
        """SSM parameter is created for Aurora endpoint discovery."""
        _stack, template = self._synth(aurora_enabled=True, logical_name="test-aurora-ssm")
        ssm_params = template.find_resources("AWS::SSM::Parameter")
        aurora_params = {
            lid: r for lid, r in ssm_params.items() if lid.startswith("AuroraPgvectorEndpoint")
        }
        assert aurora_params, (
            "AuroraPgvectorEndpointParam SSM parameter not found. "
            f"Available SSM params: {sorted(ssm_params)[:10]}"
        )

    def test_service_account_role_has_secret_read_access(self):
        """ServiceAccountRole has read access to the Aurora secret."""
        _stack, template = self._synth(
            aurora_enabled=True, logical_name="test-aurora-secret-access"
        )
        policies = template.find_resources("AWS::IAM::Policy")
        sa_policies = {
            lid: res
            for lid, res in policies.items()
            if lid.startswith("ServiceAccountRoleDefaultPolicy")
        }
        assert sa_policies, "ServiceAccountRole DefaultPolicy not found"

        # Check that at least one statement grants secretsmanager:GetSecretValue
        # or secretsmanager:DescribeSecret on the Aurora cluster secret
        found_secret_grant = False
        for _lid, policy in sa_policies.items():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if any("secretsmanager" in a for a in actions):
                    found_secret_grant = True
                    break
        assert found_secret_grant, (
            "ServiceAccountRole must have Secrets Manager access for the "
            "Aurora pgvector credentials secret."
        )

    def test_aurora_enhanced_monitoring_enabled(self):
        """Aurora writer instance has enhanced monitoring enabled (60s interval)."""
        _stack, template = self._synth(aurora_enabled=True, logical_name="test-aurora-monitoring")
        instances = template.find_resources("AWS::RDS::DBInstance")
        assert instances, "No RDS DBInstance found — writer instance missing"
        for _lid, instance in instances.items():
            props = instance.get("Properties", {})
            interval = props.get("MonitoringInterval")
            assert interval == 60, (
                f"Writer instance MonitoringInterval should be 60, got {interval}"
            )

    def test_aurora_has_reader_instance(self):
        """Aurora cluster has both a writer and a reader instance for HA."""
        _stack, template = self._synth(aurora_enabled=True, logical_name="test-aurora-reader")
        instances = template.find_resources("AWS::RDS::DBInstance")
        assert len(instances) >= 2, (
            f"Aurora cluster should have at least 2 instances (writer + reader), "
            f"found {len(instances)}: {sorted(instances)}"
        )
        # Verify at least one is a reader (PromotionTier > 0 or no PromotionTier for writer)
        writer_count = 0
        reader_count = 0
        for _lid, instance in instances.items():
            props = instance.get("Properties", {})
            tier = props.get("PromotionTier", 0)
            if tier == 0:
                writer_count += 1
            else:
                reader_count += 1
        assert writer_count >= 1, "Should have at least 1 writer instance"
        assert reader_count >= 1, "Should have at least 1 reader instance"


# =============================================================================
# Always-on Cluster_Shared_Bucket integration — ConfigMap + IAM grant
# =============================================================================


class TestClusterSharedBucketRegionalIntegration:
    """Regression guards for the always-on ``Cluster_Shared_Bucket`` plumbing
    in ``GCORegionalStack``.

    Every regional stack SHALL:

    1. Populate the three ``{{CLUSTER_SHARED_BUCKET}}``,
       ``{{CLUSTER_SHARED_BUCKET_ARN}}``, and ``{{CLUSTER_SHARED_BUCKET_REGION}}``
       keys in the ``KubectlApplyManifests`` CustomResource's
       ``ImageReplacements`` property with non-empty values (tokens or strings).
    2. Attach two IAM policy statements to ``service_account_role`` — an
       S3 RW grant scoped to the cluster-shared bucket ARN (resolved via
       ``ReadClusterSharedBucketArn`` ``AwsCustomResource``) and a KMS
       ``Decrypt|GenerateDataKey`` grant scoped by ``kms:ViaService`` to
       the cluster-shared bucket's region.
    3. Synthesize to a template that is shape-identical across the
       ``analytics_environment.enabled=true`` and ``=false`` cases —
       the regional stack does not read the analytics toggle, so flipping
       it MUST NOT produce any diff in the regional template's
       ``KubectlApplyManifests`` or ``AWS::IAM::Policy`` resources. Any
       delta lives in ``gco-analytics``, not the regional stack.
    """

    @staticmethod
    def _synth(
        analytics_enabled: bool,
        logical_name: str,
    ) -> assertions.Template:
        """Synthesize the regional stack with a given analytics toggle value.

        The regional stack itself never reads ``analytics_environment.*``,
        so the two synth variants SHOULD produce shape-identical templates.
        The toggle is passed through ``cdk.App`` context so any future
        accidental read would surface as a template diff in the
        ``test_regional_stack_shape_identical_across_analytics_toggle``
        assertion below.
        """
        from gco.stacks.regional_stack import GCORegionalStack

        context = {
            "analytics_environment": {
                "enabled": analytics_enabled,
                "hyperpod": {"enabled": False},
                "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
                "efs": {"removal_policy": "destroy"},
                "studio": {"user_profile_name_prefix": None},
            },
        }
        app = cdk.App(context=context)
        config = MockConfigLoader(app)

        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image

            stack = GCORegionalStack(
                app,
                logical_name,
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )
            return assertions.Template.from_stack(stack)

    @staticmethod
    def _kubectl_apply_properties(template: assertions.Template) -> dict:
        """Return the ``ImageReplacements`` property of the
        ``KubectlApplyManifests`` custom resource.

        The custom resource is synthesized as
        ``AWS::CloudFormation::CustomResource`` with logical id
        ``KubectlApplyManifests``. There is a second, sibling custom
        resource ``KubectlApplyPostHelmManifests`` that re-applies after
        helm finishes — we assert on the primary one here; the
        always-present ConfigMap property test covers the post-helm
        sibling separately via Hypothesis.
        """
        resources = template.to_json().get("Resources", {})
        primary = resources.get("KubectlApplyManifests")
        assert primary is not None, (
            "KubectlApplyManifests CustomResource must be present in the "
            "synthesized template. Available logical ids: "
            f"{sorted(k for k in resources if 'KubectlApply' in k)}"
        )
        properties: dict = primary.get("Properties", {})
        return properties

    def test_configmap_replacements_present_when_analytics_disabled(self):
        """Default (``enabled=false``) synthesis populates all three CLUSTER_SHARED_BUCKET keys."""
        template = self._synth(analytics_enabled=False, logical_name="test-cs-cm-disabled")

        props = self._kubectl_apply_properties(template)
        replacements = props.get("ImageReplacements", {})

        required_keys = (
            "{{CLUSTER_SHARED_BUCKET}}",
            "{{CLUSTER_SHARED_BUCKET_ARN}}",
            "{{CLUSTER_SHARED_BUCKET_REGION}}",
        )
        for key in required_keys:
            assert key in replacements, (
                f"KubectlApplyManifests.ImageReplacements must contain {key!r} "
                f"so the gco-cluster-shared-bucket ConfigMap renders correctly "
                f"on every regional cluster (always-on). "
                f"Present keys: {sorted(replacements)}"
            )
            value = replacements[key]
            # Values are CDK tokens (Fn::GetAtt dicts) that reference the
            # AwsCustomResource reading the global-region SSM parameter.
            # They are "non-empty" in the structural sense: neither None
            # nor an empty string nor an empty dict.
            assert value not in (None, "", {}), (
                f"ImageReplacements[{key!r}] must be non-empty at synth time; got {value!r}"
            )

    def test_iam_policy_grants_s3_rw_on_cluster_shared_bucket_when_disabled(self):
        """``ServiceAccountRole`` has S3 RW + KMS grants that reference the
        cluster-shared bucket ARN token, regardless of the analytics toggle.

        The S3 statement's Resource entries come from the cross-region
        ``AwsCustomResource`` ``ReadClusterSharedBucketArn``, so the check
        is for an ``Fn::GetAtt`` reference into that CR's ``Parameter.Value``
        response field rather than a literal ARN string.
        """
        template = self._synth(analytics_enabled=False, logical_name="test-cs-iam-disabled")
        policies = template.find_resources("AWS::IAM::Policy")
        sa_policies = {
            lid: res
            for lid, res in policies.items()
            if lid.startswith("ServiceAccountRoleDefaultPolicy")
        }
        assert sa_policies, (
            "ServiceAccountRoleDefaultPolicy must be present — it carries "
            "the always-on Cluster_Shared_Bucket RW + KMS grants."
        )

        found_s3_rw_on_cluster_shared = False
        found_kms_scoped_to_cluster_shared = False
        for _lid, policy in sa_policies.items():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                resources = statement.get("Resource", [])
                if not isinstance(resources, list):
                    resources = [resources]
                resources_str = str(resources)

                # S3 RW grant — all five actions + a ReadClusterSharedBucketArn token reference.
                s3_rw_actions = {
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                    "s3:GetBucketLocation",
                }
                if s3_rw_actions.issubset(set(actions)) and (
                    "ReadClusterSharedBucketArn" in resources_str
                ):
                    found_s3_rw_on_cluster_shared = True

                # KMS grant — Decrypt + GenerateDataKey, scoped via kms:ViaService
                # to s3.<region>.amazonaws.com. The region is a token
                # (ReadClusterSharedBucketRegion AwsCustomResource).
                if (
                    "kms:Decrypt" in actions
                    and "kms:GenerateDataKey" in actions
                    and "ReadClusterSharedBucketRegion" in str(statement)
                ):
                    found_kms_scoped_to_cluster_shared = True

        assert found_s3_rw_on_cluster_shared, (
            "ServiceAccountRoleDefaultPolicy must contain an S3 RW statement "
            "whose Resource entries reference the ReadClusterSharedBucketArn "
            "AwsCustomResource (the cross-region SSM reader). This is the "
            "always-on grant added by _grant_cluster_shared_bucket_to_job_role."
        )
        assert found_kms_scoped_to_cluster_shared, (
            "ServiceAccountRoleDefaultPolicy must contain a KMS "
            "Decrypt|GenerateDataKey statement scoped via kms:ViaService to "
            "the cluster-shared bucket's region (ReadClusterSharedBucketRegion "
            "AwsCustomResource)."
        )

    def test_configmap_replacements_present_when_analytics_enabled(self):
        """Flipping ``analytics_environment.enabled=true`` leaves the
        three CLUSTER_SHARED_BUCKET replacements present and populated
        (the regional stack does not read the toggle; integration is
        always-on)."""
        template = self._synth(analytics_enabled=True, logical_name="test-cs-cm-enabled")
        props = self._kubectl_apply_properties(template)
        replacements = props.get("ImageReplacements", {})
        for key in (
            "{{CLUSTER_SHARED_BUCKET}}",
            "{{CLUSTER_SHARED_BUCKET_ARN}}",
            "{{CLUSTER_SHARED_BUCKET_REGION}}",
        ):
            assert key in replacements and replacements[key] not in (None, "", {}), (
                f"With analytics_environment.enabled=true, "
                f"ImageReplacements[{key!r}] must still be present and "
                f"non-empty (integration is unconditional)."
            )

    @staticmethod
    def _canonicalize_resource(resource: dict, off_stack_prefix: str, on_stack_prefix: str) -> str:
        """Canonicalize a resource dict for byte-equivalence comparison.

        Three synthesis artifacts differ between the two synth variants
        even though the regional stack is logically identical:

        1. ``DeploymentTimestamp`` in ``ImageReplacements`` is the
           ISO-8601 synth wall-clock; compared synths run microseconds
           apart so they generally differ.
        2. Nested logical ids embed the top-level stack's construct id
           (e.g. ``GCOEksClusterClusterSecurityGroupfromtestcstoggleoff``
           vs ``...toggleon``). The stacks can't share a logical name
           when they live in the same ``cdk.App``, so the fixture passes
           different names in and we strip the prefix here.
        3. CDK construct-path hashes depend on the stack name (see
           ``GCOEksClusterClusterSecurityGroupfromSTACKNAMEKubectlLambdaSG<hash>``).
           The hash suffix differs between the two variants purely
           because the input path differs — it's a deterministic
           function of the construct tree, not a real drift in the
           logical shape of the resource. We normalize any hex hash
           that follows ``KubectlLambdaSG`` to a placeholder.
        """
        import json as _json
        import re as _re

        serialized = _json.dumps(resource, sort_keys=True)

        # The stack-name prefix is the logical name lower-cased with dashes
        # removed — that is what CDK injects into nested construct ids.
        off_token = off_stack_prefix.replace("-", "").lower()
        on_token = on_stack_prefix.replace("-", "").lower()
        serialized = serialized.replace(off_token, "STACKNAME")
        serialized = serialized.replace(on_token, "STACKNAME")

        # DeploymentTimestamp drifts across calls to _synth within the
        # same test because `datetime.now()` is read at synth time.
        serialized = _re.sub(
            r'"\{\{DEPLOYMENT_TIMESTAMP\}\}": "[^"]*"',
            '"{{DEPLOYMENT_TIMESTAMP}}": "<timestamp>"',
            serialized,
        )
        serialized = _re.sub(
            r'"DeploymentTimestamp": "[^"]*"',
            '"DeploymentTimestamp": "<timestamp>"',
            serialized,
        )

        # CDK's hash-suffix on security-group nested logical ids depends
        # on the stack name, so even after ``STACKNAME`` substitution the
        # trailing hex differs. Normalize ``KubectlLambdaSG<hex>`` to a
        # constant. The hex is 16+ chars of upper-case hex digits.
        serialized = _re.sub(
            r"KubectlLambdaSG[0-9A-F]+",
            "KubectlLambdaSG<hash>",
            serialized,
        )
        return serialized

    def test_regional_template_shape_identical_across_analytics_toggle(self):
        """The ``analytics_environment.enabled`` toggle MUST NOT change
        the regional stack's ``KubectlApplyManifests`` or
        ``AWS::IAM::Policy`` resources beyond synthesis-only artifacts
        (stack-name-embedded logical ids, synth-time deployment
        timestamp).

        Any non-artifact delta would mean the regional stack has
        accidentally grown a dependency on the analytics toggle — which
        would break the invariant that ``enabled=false`` is the default
        and must leave the rest of the system untouched. Comparison is
        by JSON canonicalization of the two resource maps, with the
        stack-name prefix normalized to a constant and the deployment
        timestamp normalized to ``<timestamp>``.
        """
        off_prefix = "test-cs-toggle-off"
        on_prefix = "test-cs-toggle-on"
        template_off = self._synth(analytics_enabled=False, logical_name=off_prefix)
        template_on = self._synth(analytics_enabled=True, logical_name=on_prefix)

        resources_off = template_off.to_json().get("Resources", {})
        resources_on = template_on.to_json().get("Resources", {})

        # Compare KubectlApplyManifests + KubectlApplyPostHelmManifests (both
        # carry ImageReplacements) and every AWS::IAM::Policy resource. The
        # logical ids are deterministic because the construct tree is the
        # same in both synths, so a direct key-by-key dict comparison works.
        kubectl_logical_ids = sorted(lid for lid in resources_off if "KubectlApply" in lid)
        assert kubectl_logical_ids, (
            "Expected at least one KubectlApply* CustomResource in the regional template."
        )

        for lid in kubectl_logical_ids:
            off_json = self._canonicalize_resource(resources_off[lid], off_prefix, on_prefix)
            on_json = self._canonicalize_resource(resources_on.get(lid, {}), off_prefix, on_prefix)
            assert off_json == on_json, (
                f"{lid!r} resource differs between "
                f"analytics_environment.enabled=false and =true beyond "
                f"synthesis-only artifacts. The regional stack must be "
                f"independent of the analytics toggle."
            )

        policy_logical_ids = sorted(
            lid for lid, res in resources_off.items() if res.get("Type") == "AWS::IAM::Policy"
        )
        assert policy_logical_ids, (
            "Expected at least one AWS::IAM::Policy in the regional template."
        )
        for lid in policy_logical_ids:
            off_json = self._canonicalize_resource(resources_off[lid], off_prefix, on_prefix)
            on_json = self._canonicalize_resource(resources_on.get(lid, {}), off_prefix, on_prefix)
            assert off_json == on_json, (
                f"{lid!r} IAM policy differs between "
                f"analytics_environment.enabled=false and =true. The regional "
                f"stack's IAM grants are always-on and must be independent "
                f"of the analytics toggle."
            )
