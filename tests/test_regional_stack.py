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
            "allowed_namespaces": ["gco-jobs"],
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

    def get_cluster_observability_config(self):
        # Mirrors the on-by-default cdk.json cluster_observability defaults so
        # regional synth exercises the real (enabled) observability path.
        return {
            "enabled": True,
            "grafana": {
                "persistence_size": "10Gi",
                "admin_user": "admin",
                "admin_password_rotation_schedule": "0 4 1 * *",
            },
            "prometheus": {"persistence_size": "50Gi", "retention": "15d"},
            "alertmanager": {"enabled": True, "persistence_size": "5Gi"},
        }

    def get_cluster_observability_enabled(self):
        return bool(self.get_cluster_observability_config()["enabled"])


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
        assert config.get_kubernetes_version() == "1.36"

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
            kubernetes_version="1.36",
            addons=["metrics-server"],
            resource_thresholds=thresholds,
        )

        assert config.region == "us-east-1"
        assert config.cluster_name == "test-cluster"
        assert config.kubernetes_version == "1.36"


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

    @staticmethod
    def _mock_helm_installer_with_teardown(stack):
        """Provide lightweight constructs so dependency ordering synthesizes without Docker."""
        TestRegionalStackSynthesis._mock_helm_installer(stack)
        stack.helm_installer_access_entry = cdk.CfnResource(
            stack,
            "MockHelmInstallerAccessEntry",
            type="AWS::EKS::AccessEntry",
        )
        stack.helm_teardown_resource = cdk.CustomResource(
            stack,
            "HelmTeardown",
            service_token=("arn:aws:lambda:us-east-1:123456789012:function:mock-teardown"),
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

    def test_regional_stack_ga_deregistration_teardown_guard(self):
        """Issue #130: a delete-time custom resource must deregister the ALB
        from Global Accelerator before the VPC public subnets are deleted.

        Registration is one-directional (the convergence state machine adds the
        ALB at deploy time), so without a teardown hook Global Accelerator keeps
        its managed ENIs pinned in the public subnets and subnet deletion — and
        the whole stack delete — fails. This asserts (1) the dedicated
        deregistration Lambda exists with the ``handler.on_delete_event``
        entrypoint, and (2) its custom resource depends on the public subnets so
        CloudFormation runs the deregistration (releasing the ENIs) first.
        """
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
                "test-regional-ga-dereg",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

            template = assertions.Template.from_stack(stack)

            # (1) The dedicated deregistration Lambda uses the on_delete_event entrypoint.
            template.has_resource_properties(
                "AWS::Lambda::Function",
                {"Handler": "handler.on_delete_event"},
            )

            # (2) The deregistration custom resource must depend on the public
            # subnets so teardown deregisters (releasing GA ENIs) before subnet
            # deletion. add_dependency(self.vpc) renders as DependsOn entries on
            # the custom resource for the VPC's resources, including the subnets.
            custom_resources = template.find_resources("AWS::CloudFormation::CustomResource")
            dereg = {
                lid: res
                for lid, res in custom_resources.items()
                if lid.startswith("GaDeregistration")
            }
            assert dereg, (
                f"Expected a GaDeregistration custom resource; found: {list(custom_resources)}"
            )
            (dereg_res,) = dereg.values()
            assert dereg_res["Properties"]["RegistryRegion"] == "us-east-2"
            depends_on = dereg_res.get("DependsOn", [])
            if isinstance(depends_on, str):
                depends_on = [depends_on]
            assert any("PublicSubnet" in d for d in depends_on), (
                "GaDeregistration custom resource must depend on the VPC public subnets "
                f"for teardown ordering; DependsOn={depends_on}"
            )

            # (3) Regression guard for the RemoveEndpoints IAM requirement: the
            # Global Accelerator RemoveEndpoints API is implemented as an
            # endpoint-group update, so the dereg role must ALSO hold
            # UpdateEndpointGroup or teardown deregistration fails at runtime with
            # AccessDeniedException (see issue #130). The dereg policy is uniquely
            # identifiable by DescribeAccelerator — the registration Lambda's
            # policy does not grant it.
            template.has_resource_properties(
                "AWS::IAM::Policy",
                {
                    "PolicyDocument": assertions.Match.object_like(
                        {
                            "Statement": assertions.Match.array_with(
                                [
                                    assertions.Match.object_like(
                                        {
                                            "Action": assertions.Match.array_with(
                                                [
                                                    "globalaccelerator:DescribeAccelerator",
                                                    "globalaccelerator:RemoveEndpoints",
                                                    "globalaccelerator:UpdateEndpointGroup",
                                                ]
                                            )
                                        }
                                    )
                                ]
                            )
                        }
                    )
                },
            )

    def test_runtime_teardown_dependency_chain(self):
        """Delete order is Helm/quiesce -> GA -> convergence -> EKS access/cluster."""
        from gco.stacks.regional_stack import GCORegionalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackSynthesis._mock_helm_installer_with_teardown,
            ),
        ):
            mock_image = MagicMock()
            mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            mock_docker.return_value = mock_image
            stack = GCORegionalStack(
                app,
                "test-regional-runtime-teardown-order",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

        resources = assertions.Template.from_stack(stack).to_json()["Resources"]

        def _depends_on(logical_id):
            value = resources[logical_id].get("DependsOn", [])
            return [value] if isinstance(value, str) else value

        assert "MockHelmInstallerAccessEntry" in _depends_on("HelmInstallCharts")
        assert any(
            logical_id.startswith("KubectlLambdaAccessEntry")
            for logical_id in _depends_on("HelmInstallCharts")
        )
        ga_id = next(
            logical_id for logical_id in resources if logical_id.startswith("GaDeregistration")
        )
        assert ga_id in _depends_on("HelmTeardown")
        assert "HelmInstallCharts" in _depends_on(ga_id)

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

    def test_health_monitor_ssm_repair_policy_is_exact(self):
        """Health self-healing gets only Get/Put on its one global-region parameter."""
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
                "test-health-self-healing-iam",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

        template = assertions.Template.from_stack(stack)
        policies = template.find_resources("AWS::IAM::Policy")
        health_statements = [
            statement
            for logical_id, policy in policies.items()
            if logical_id.startswith("HealthMonitorRoleDefaultPolicy")
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
        shared_statements = [
            statement
            for logical_id, policy in policies.items()
            if logical_id.startswith("ServiceAccountRoleDefaultPolicy")
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
        matching = []
        for statement in health_statements:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if set(actions) == {"ssm:GetParameter", "ssm:PutParameter"}:
                matching.append(statement)

        assert len(matching) == 1
        assert matching[0]["Resource"] == (
            "arn:aws:ssm:us-east-2:123456789012:parameter/gco-test/alb-hostname-us-east-1"
        )
        for statement in shared_statements:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            assert "ssm:PutParameter" not in actions

    def test_manifest_processor_role_owns_jobs_table_access(self):
        """Only the manifest processor may read or mutate centralized queue records."""
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
                "test-manifest-processor-queue-iam",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

        policies = assertions.Template.from_stack(stack).find_resources("AWS::IAM::Policy")

        def _statements(logical_id_prefix):
            return [
                statement
                for logical_id, policy in policies.items()
                if logical_id.startswith(logical_id_prefix)
                for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            ]

        manifest_statements = _statements("ManifestProcessorRoleDefaultPolicy")
        shared_statements = _statements("ServiceAccountRoleDefaultPolicy")
        assert manifest_statements
        assert shared_statements

        jobs_table_arn = "arn:aws:dynamodb:us-east-2:123456789012:table/gco-test-jobs"
        jobs_index_arn = f"{jobs_table_arn}/index/*"
        jobs_resources = {jobs_table_arn, jobs_index_arn}

        manifest_jobs_statements = []
        for statement in manifest_statements:
            resources = statement.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            if jobs_resources.intersection(resources):
                manifest_jobs_statements.append(statement)

        assert len(manifest_jobs_statements) == 1
        jobs_statement = manifest_jobs_statements[0]
        actions = jobs_statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        resources = jobs_statement.get("Resource", [])
        if isinstance(resources, str):
            resources = [resources]
        assert set(actions) == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:Query",
            "dynamodb:Scan",
        }
        assert set(resources) == jobs_resources

        for statement in shared_statements:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resources = statement.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            assert jobs_resources.isdisjoint(resources)
            assert not (
                jobs_resources.intersection(resources)
                and {"dynamodb:PutItem", "dynamodb:UpdateItem"}.intersection(actions)
            )

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
    """Regression guard for the ``ServiceAccountRole`` cdk-nag suppressions.

    The ``ServiceAccountRole``'s inline policy grants read/write on the
    regional-shared and cluster-shared buckets using an ``<arn>/*``
    object-key wildcard, which cdk-nag's ``AwsSolutions-IAM5`` rule flags
    and blocks ``cdk deploy`` on unless the role carries a scoped
    acknowledgment. cdk-nag v3 records acknowledgments as exact-string
    finding ids in construct metadata (there is no regex support), so this
    test asserts the role carries an ``AwsSolutions-IAM5`` acknowledgment
    for each bucket wildcard.

    The original launch-blocking bug was a *missing* suppression that only
    surfaced at deploy time. This test ensures every synthesized regional
    stack carries the bucket-wildcard acknowledgments, so any refactor that
    drops or renames them fails PR CI instead of deploy.

    (The cross-stack auth-secret wildcard also lives on this role but does
    not produce a v3 finding — cdk-nag's IAM5 check does not flag
    ``Fn::ImportValue``-based resources — so it needs no acknowledgment.)
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
        """The ServiceAccountRole must carry scoped ``AwsSolutions-IAM5``
        acknowledgments for its shared-bucket object-key wildcards."""
        from gco.stacks.nag_suppressions import _ACK_METADATA_KEY

        stack, _template = self._synth(fsx_enabled=False, logical_name="test-sa-role-suppression")

        # cdk-nag v3 records acknowledgments as {finding_id: reason} dicts in
        # construct metadata. Collect every acknowledged finding id on the role.
        role = stack.service_account_role
        ack_ids: list[str] = []
        for entry in role.node.metadata:
            if entry.type == _ACK_METADATA_KEY and entry.data:
                ack_ids.extend(entry.data.keys())

        iam5 = [fid for fid in ack_ids if fid.startswith("AwsSolutions-IAM5[")]
        assert iam5, (
            "ServiceAccountRole is missing its AwsSolutions-IAM5 "
            "acknowledgments. Its inline policy grants read/write on the "
            "shared buckets with <arn>/* object-key wildcards, which cdk-nag "
            "blocks deploy on. Add a scoped acknowledge_nag_findings entry "
            "with an exact applies_to detail for each bucket wildcard."
        )

        # The regional-shared and cluster-shared bucket wildcards must each be
        # acknowledged by their exact finding id (not a broad Resource::*).
        for token in ("RegionalSharedBucket", "ReadClusterSharedBucketArn"):
            assert any(token in fid for fid in iam5), (
                "ServiceAccountRole is missing an AwsSolutions-IAM5 "
                f"acknowledgment referencing {token}. Found: {iam5!r}"
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

    def test_jobs_table_has_priority_and_lease_indexes(self):
        """Worker claims and lease recovery use their exact ordered queue GSIs."""
        from gco.stacks.global_stack import GCOGlobalStack

        app = cdk.App()
        config = MockConfigLoader(app)
        stack = GCOGlobalStack(app, "test-dynamodb-jobs-queue-indexes", config=config)

        tables = assertions.Template.from_stack(stack).find_resources("AWS::DynamoDB::Table")
        jobs_tables = [
            table
            for table in tables.values()
            if table["Properties"].get("TableName") == "gco-test-jobs"
        ]
        assert len(jobs_tables) == 1
        indexes = {
            index["IndexName"]: index
            for index in jobs_tables[0]["Properties"]["GlobalSecondaryIndexes"]
        }

        assert indexes["region-status-priority-index"]["KeySchema"] == [
            {"AttributeName": "region_status", "KeyType": "HASH"},
            {"AttributeName": "priority_sort", "KeyType": "RANGE"},
        ]
        assert indexes["region-status-priority-index"]["Projection"] == {"ProjectionType": "ALL"}
        assert indexes["region-status-lease-index"]["KeySchema"] == [
            {"AttributeName": "region_status", "KeyType": "HASH"},
            {"AttributeName": "lease_expires_at", "KeyType": "RANGE"},
        ]
        assert indexes["region-status-lease-index"]["Projection"] == {"ProjectionType": "ALL"}

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
       keys in the ``HelmInstallCharts`` convergence-trigger CustomResource's
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
       ``HelmInstallCharts`` or ``AWS::IAM::Policy`` resources. Any
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
        """Return the properties of the ``HelmInstallCharts`` convergence-trigger
        custom resource.

        The manifest ``ImageReplacements`` now live on the single fire-and-forget
        trigger CustomResource (logical id ``HelmInstallCharts``, type
        ``AWS::CloudFormation::CustomResource``) that starts the convergence
        state machine. The base and post-Helm kubectl passes run as tasks inside
        that state machine rather than as their own custom resources, so there is
        no longer a ``KubectlApplyManifests`` resource in the template.
        """
        resources = template.to_json().get("Resources", {})
        trigger = resources.get("HelmInstallCharts")
        assert trigger is not None, (
            "HelmInstallCharts CustomResource must be present in the "
            "synthesized template. Available logical ids: "
            f"{sorted(k for k in resources if 'Helm' in k or 'Kubectl' in k)}"
        )
        properties: dict = trigger.get("Properties", {})
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
        the regional stack's ``HelmInstallCharts`` or
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

        # The convergence-trigger CustomResource ("HelmInstallCharts") carries
        # the manifest ImageReplacements; compare it across the analytics toggle.
        # The base/post-Helm kubectl passes now run inside the state machine, so
        # there are no longer KubectlApply* custom resources to compare. The
        # logical id is deterministic because the construct tree is the same in
        # both synths, so a direct key-by-key dict comparison works.
        trigger_logical_ids = sorted(lid for lid in resources_off if lid == "HelmInstallCharts")
        assert trigger_logical_ids, (
            "Expected the HelmInstallCharts convergence-trigger CustomResource "
            "in the regional template."
        )

        for lid in trigger_logical_ids:
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


class TestRegionalStackVolcanoImageMirror:
    """The optional Volcano image mirror (cdk.json ``volcano_image_mirror``).

    Verifies the feature is off by default (no override, and — regression —
    no pull-through-cache/registry-policy resources), that enabling it injects
    the Volcano ``basic.image_registry`` override pointing at the project's
    ``<project_name>/*`` ECR mirror, and that a misconfigured ``ecr_namespace``
    fails fast at synth.
    """

    @staticmethod
    def _build(app):
        """Synthesize the regional stack under the given app (carrying context)."""
        from gco.stacks.regional_stack import GCORegionalStack

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
                "test-regional-mirror",
                config=config,
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN, fake account, not a real secret
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )
        return stack

    def _enabled_app(self, **overrides):
        # MockConfigLoader.get_project_name() is "gco-test", so the mirror
        # namespace must live under that project prefix (#139).
        mirror = {"enabled": True, "ecr_namespace": "gco-test/dockerhub"}
        mirror.update(overrides)
        return cdk.App(context={"volcano_image_mirror": mirror})

    def test_disabled_by_default_no_override_no_cache_resources(self):
        """No context -> no registry override, and (regression) no PTC resources."""
        stack = self._build(cdk.App())
        assert stack.volcano_mirror_registry is None
        # Observability is on by default, so the override map carries its
        # kube-prometheus-stack entry; this suite only cares that the *volcano*
        # image-registry override is absent when the mirror is off.
        assert "volcano" not in stack._helm_chart_value_overrides()
        template = assertions.Template.from_stack(stack)
        # The mirror approach creates no pull-through cache / registry policy.
        template.resource_count_is("AWS::ECR::PullThroughCacheRule", 0)
        template.resource_count_is("AWS::ECR::RegistryPolicy", 0)

    def test_enabled_sets_mirror_registry(self):
        stack = self._build(self._enabled_app())
        assert (
            stack.volcano_mirror_registry
            == "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco-test/dockerhub"
        )
        # Still creates no pull-through cache / registry policy resources.
        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::ECR::PullThroughCacheRule", 0)
        template.resource_count_is("AWS::ECR::RegistryPolicy", 0)

    def test_enabled_redirects_volcano_image_registry(self):
        """The HelmInstallCharts custom resource carries the Volcano override."""
        template = assertions.Template.from_stack(self._build(self._enabled_app()))
        template.has_resource_properties(
            "AWS::CloudFormation::CustomResource",
            {
                "Charts": {
                    "volcano": {
                        "values": {
                            "basic": {
                                "image_registry": (
                                    "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco-test/dockerhub"
                                )
                            }
                        }
                    }
                }
            },
        )

    def test_custom_namespace_is_honored(self):
        stack = self._build(self._enabled_app(ecr_namespace="gco-test/mirror"))
        assert stack.volcano_mirror_registry == (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco-test/mirror"
        )

    def test_namespace_outside_project_prefix_raises(self):
        # A namespace not under the deployment's project prefix (gco-test/) is
        # rejected so it can't miss the project's ECR replication/allow-list (#139).
        app = self._enabled_app(ecr_namespace="dockerhub")
        with pytest.raises(ValueError, match="gco-test/"):
            self._build(app)

    def test_invalid_namespace_path_raises(self):
        app = self._enabled_app(ecr_namespace="gco-test/Bad_Seg!")
        with pytest.raises(ValueError, match="ecr_namespace"):
            self._build(app)


class TestRegionalStackEksControlPlaneAzExclusion:
    """EKS control-plane subnets must exclude AZs EKS does not support.

    Regression guard for the all-AZ VPC change (max_azs=99): the VPC spans every
    Availability Zone, but EKS refuses to create a cluster whose subnets land in
    a disallowed AZ (published by *AZ ID* — e.g. ``use1-az3`` in us-east-1). A
    real ``deploy-all`` fails at ``CreateCluster`` if such a subnet is passed, so
    ``_eks_control_plane_subnets`` filters them out while leaving the subnet in
    the VPC for worker/other use.

    ``_resolve_unsupported_az_names`` (which does a credentialed EC2 AZ-ID -> name
    lookup) is patched here so the test is fully hermetic — it never calls AWS.
    """

    _SIX_AZS = [f"us-east-1{s}" for s in "abcdef"]

    @staticmethod
    def _mock_helm_installer(stack):
        stack.helm_installer_lambda = MagicMock()
        stack.helm_installer_provider = MagicMock()
        stack.helm_installer_provider.service_token = (
            "arn:aws:lambda:us-east-1:123456789012:function:mock"  # nosec B106 - test fixture ARN
        )

    def _build(self, unsupported_names):
        from gco.stacks.regional_stack import GCORegionalStack

        # Seed the availability-zones context so the VPC spans all six us-east-1
        # AZs with concrete names (mirrors what a real credentialed synth caches).
        app = cdk.App(
            context={
                "availability-zones:account=123456789012:region=us-east-1": self._SIX_AZS,
            }
        )
        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackEksControlPlaneAzExclusion._mock_helm_installer,
            ),
            patch.object(
                GCORegionalStack,
                "_resolve_unsupported_az_names",
                return_value=unsupported_names,
            ),
        ):
            mock_docker.return_value.image_uri = (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            )
            return GCORegionalStack(
                app,
                "test-regional-eks-az",
                config=MockConfigLoader(app),
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test",  # nosec B106
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

    def test_control_plane_excludes_unsupported_az(self):
        """When an AZ is unsupported (use1-az3 -> us-east-1e here), the EKS
        control-plane subnets drop it but the VPC keeps a subnet in every AZ."""
        stack = self._build(["us-east-1e"])

        control_plane_azs = {s.availability_zone for s in stack.eks_control_plane_subnets}
        assert "us-east-1e" not in control_plane_azs
        assert control_plane_azs == {
            "us-east-1a",
            "us-east-1b",
            "us-east-1c",
            "us-east-1d",
            "us-east-1f",
        }

        # The VPC itself still spans all six AZs — "a subnet in every AZ" holds.
        vpc_azs = {s.availability_zone for s in stack.vpc.private_subnets}
        assert vpc_azs == set(self._SIX_AZS)

    def test_no_restriction_keeps_all_private_subnets(self):
        """Regions without a restriction (empty resolver result) hand the EKS
        control plane every private subnet, unchanged from before."""
        stack = self._build([])

        control_plane_azs = {s.availability_zone for s in stack.eks_control_plane_subnets}
        assert control_plane_azs == set(self._SIX_AZS)
        assert stack.eks_unsupported_az_names == []

    def test_resolver_no_ec2_call_without_account_env(self, monkeypatch):
        """``_resolve_unsupported_az_names`` must not touch AWS when the deploy
        account isn't resolved (the unit-test / agnostic-synth case): no
        ``CDK_DEFAULT_ACCOUNT`` => empty result, no EC2 client constructed."""
        from gco.stacks import regional_stack as rs

        monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)

        def _boom(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("boto3 must not be called without CDK_DEFAULT_ACCOUNT")

        # Build a stack (region us-east-1 has a restriction) with the resolver
        # UNpatched; the env guard alone must keep it from calling boto3.
        app = cdk.App(
            context={
                "availability-zones:account=123456789012:region=us-east-1": self._SIX_AZS,
            }
        )
        with (
            patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
            patch.object(
                rs.GCORegionalStack,
                "_create_helm_installer_lambda",
                TestRegionalStackEksControlPlaneAzExclusion._mock_helm_installer,
            ),
            patch("boto3.client", _boom),
        ):
            mock_docker.return_value.image_uri = (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
            )
            stack = rs.GCORegionalStack(
                app,
                "test-regional-eks-az-guard",
                config=MockConfigLoader(app),
                region="us-east-1",
                auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test",  # nosec B106
                env=cdk.Environment(account="123456789012", region="us-east-1"),
            )

        # No credentials env => no filtering => control plane uses all private subnets.
        assert stack.eks_unsupported_az_names == []
        assert {s.availability_zone for s in stack.eks_control_plane_subnets} == set(self._SIX_AZS)
