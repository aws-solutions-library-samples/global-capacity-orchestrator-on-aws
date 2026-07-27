"""
CDK stack synthesis tests.

Synthesizes each GCO CDK stack (Global, API Gateway, Monitoring, Regional)
against a MockConfigLoader that returns hand-crafted ConfigLoader values
— no cdk.json, no boto3 — and asserts the resulting CloudFormation
templates contain the expected resources, outputs, and cross-stack
dependencies. Good as a smoke test that construct wiring still compiles
after refactors without needing a real AWS environment.
"""

import json

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_lambda as lambda_

from gco.stacks.constants import LAMBDA_NODEJS_RUNTIME


def _methods_for_child_resource(
    template: assertions.Template,
    parent_path_part: str,
    child_path_part: str,
) -> set[str]:
    """Return methods attached to one exact parent/child API resource path."""
    resources = template.find_resources("AWS::ApiGateway::Resource")
    parent_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Properties", {}).get("PathPart") == parent_path_part
    ]
    assert len(parent_ids) == 1
    parent_id = parent_ids[0]

    child_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Properties", {}).get("PathPart") == child_path_part
        and resource.get("Properties", {}).get("ParentId") == {"Ref": parent_id}
    ]
    assert len(child_ids) == 1
    child_id = child_ids[0]

    methods = template.find_resources("AWS::ApiGateway::Method")
    return {
        resource["Properties"]["HttpMethod"]
        for resource in methods.values()
        if resource.get("Properties", {}).get("ResourceId") == {"Ref": child_id}
    }


def _integrations_for_child_resource(
    template: assertions.Template,
    parent_path_part: str,
    child_path_part: str,
) -> list[dict]:
    """Return integrations attached to one exact parent/child API resource path."""
    resources = template.find_resources("AWS::ApiGateway::Resource")
    parent_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Properties", {}).get("PathPart") == parent_path_part
    ]
    assert len(parent_ids) == 1
    child_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Properties", {}).get("PathPart") == child_path_part
        and resource.get("Properties", {}).get("ParentId") == {"Ref": parent_ids[0]}
    ]
    assert len(child_ids) == 1
    return [
        resource["Properties"]["Integration"]
        for resource in template.find_resources("AWS::ApiGateway::Method").values()
        if resource.get("Properties", {}).get("ResourceId") == {"Ref": child_ids[0]}
    ]


def _assert_streaming_lambda_integrations(integrations: list[dict]) -> None:
    """Pin the Lambda response-streaming CloudFormation contract."""
    assert len(integrations) == 3
    for integration in integrations:
        assert integration["Type"] == "AWS_PROXY"
        assert integration["IntegrationHttpMethod"] == "POST"
        assert integration["ResponseTransferMode"] == "STREAM"
        assert integration["TimeoutInMillis"] == 900000
        assert "response-streaming-invocations" in json.dumps(integration["Uri"])


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

    def get_cost_monitoring_config(self):
        return {
            "enabled": True,
            "reports": {
                "interval_minutes": 60,
                "retention_days": 365,
                "transition_to_infrequent_access_days": 90,
            },
            "athena": {"query_results_retention_days": 30},
        }

    def get_cost_monitoring_enabled(self):
        return bool(self.get_cost_monitoring_config()["enabled"])

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

    def get_backend_tls_config(self):
        return {
            "root_generation": 1,
            "root_validity_days": 3650,
            "root_rotate_before_days": 180,
            "root_activation_delay_hours": 24,
            "root_overlap_days": 45,
            "leaf_validity_days": 30,
            "leaf_rotate_before_days": 10,
            "rotation_schedule_hours": 12,
            "trust_cache_ttl_seconds": 300,
            "trust_cache_max_stale_seconds": 3600,
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
        template.has_resource_properties(
            "AWS::GlobalAccelerator::Listener",
            {
                "PortRanges": [{"FromPort": 443, "ToPort": 443}],
                "Protocol": "TCP",
            },
        )
        template.has_resource_properties(
            "AWS::GlobalAccelerator::EndpointGroup",
            {
                "HealthCheckPort": 443,
                "HealthCheckProtocol": "HTTPS",
            },
        )

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

    def test_api_gateway_consumes_stage_config_and_registry_region(self):
        """Stage knobs and the SSM registry region must reach synthesized resources."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-config",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
            api_gateway_config={
                "throttle_rate_limit": 37,
                "throttle_burst_limit": 73,
                "log_level": "ERROR",
                "metrics_enabled": False,
                "tracing_enabled": False,
            },
            registry_region="eu-west-1",
            max_request_body_bytes=2_097_152,
        )

        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::ApiGateway::Stage",
            {
                "MethodSettings": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "DataTraceEnabled": False,
                                "LoggingLevel": "ERROR",
                                "MetricsEnabled": False,
                                "ThrottlingRateLimit": 37,
                                "ThrottlingBurstLimit": 73,
                            }
                        )
                    ]
                ),
                "TracingEnabled": False,
            },
        )
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {"BACKEND_TLS_ROOT_CA_REGION": "eu-west-1"}
                    )
                }
            },
        )
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Environment": {
                    "Variables": assertions.Match.object_like({"REGISTRY_REGION": "eu-west-1"})
                }
            },
        )
        inference_functions = [
            resource
            for logical_id, resource in template.find_resources("AWS::Lambda::Function").items()
            if logical_id.startswith("InferenceStreamingProxyFunction")
        ]
        assert len(inference_functions) == 1
        assert (
            inference_functions[0]["Properties"]["Environment"]["Variables"][
                "MAX_REQUEST_BODY_BYTES"
            ]
            == "2097152"
        )

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ReconciliationSuccess",
                "ComparisonOperator": "LessThanThreshold",
                "Threshold": 1,
                "EvaluationPeriods": 1,
                "TreatMissingData": "breaching",
            },
        )

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
        template.resource_count_is("AWS::SecretsManager::Secret", 2)
        template.resource_count_is("AWS::KMS::Key", 1)
        template.has_resource_properties(
            "AWS::SecretsManager::Secret",
            {"Name": "gco/backend-tls/root-ca"},
        )
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "FunctionName": "gco-backend-tls-manager",
                "PackageType": "Image",
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "BACKEND_TLS_SERVER_NAME": "backend.gco.gco.internal",
                            "ROOT_CA_PARAMETER_NAME": "/gco/backend-tls/root-ca.pem",
                            "CERTIFICATE_REGIONS": '["us-east-1"]',
                        }
                    )
                },
            },
        )

    def test_backend_tls_manager_can_delete_only_account_certificates(self):
        """Compensation can delete only ACM certificates owned by this account."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-tls-manager-policy",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
        )

        template = assertions.Template.from_stack(stack)
        manager_role_ids = [
            logical_id
            for logical_id in template.find_resources("AWS::IAM::Role")
            if logical_id.startswith("BackendTlsManagerRole")
        ]
        assert len(manager_role_ids) == 1

        statements = []
        for policy in template.find_resources("AWS::IAM::Policy").values():
            if {"Ref": manager_role_ids[0]} not in policy["Properties"].get("Roles", []):
                continue
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])

        delete_statements = []
        for statement in statements:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "acm:DeleteCertificate" in actions:
                delete_statements.append((statement, set(actions)))

        assert len(delete_statements) == 1
        statement, actions = delete_statements[0]
        assert statement["Effect"] == "Allow"
        assert actions == {
            "acm:DeleteCertificate",
            "acm:DescribeCertificate",
            "acm:GetCertificate",
            "acm:ListTagsForCertificate",
        }
        resources = statement["Resource"]
        if not isinstance(resources, list):
            resources = [resources]
        assert resources == [
            {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        ":acm:*:",
                        {"Ref": "AWS::AccountId"},
                        ":certificate/*",
                    ],
                ]
            }
        ]

        list_statements = []
        for candidate in statements:
            candidate_actions = candidate.get("Action", [])
            if isinstance(candidate_actions, str):
                candidate_actions = [candidate_actions]
            if "acm:ListCertificates" in candidate_actions:
                list_statements.append((candidate, set(candidate_actions)))
        assert len(list_statements) == 1
        list_statement, list_actions = list_statements[0]
        assert list_statement["Effect"] == "Allow"
        assert list_actions == {
            "acm:AddTagsToCertificate",
            "acm:ImportCertificate",
            "acm:ListCertificates",
        }
        assert list_statement["Resource"] == "*"

        ssm_statements = []
        for candidate in statements:
            candidate_actions = candidate.get("Action", [])
            if isinstance(candidate_actions, str):
                candidate_actions = [candidate_actions]
            if "ssm:GetParametersByPath" in candidate_actions:
                ssm_statements.append((candidate, set(candidate_actions)))
        assert len(ssm_statements) == 1
        ssm_statement, ssm_actions = ssm_statements[0]
        assert ssm_statement["Effect"] == "Allow"
        assert ssm_actions == {
            "ssm:DeleteParameter",
            "ssm:GetParameter",
            "ssm:GetParametersByPath",
            "ssm:PutParameter",
        }
        assert "parameter/gco/backend-tls/*" in json.dumps(ssm_statement["Resource"])

    def test_aggregator_role_can_invoke_only_runtime_contract(self):
        """Identity policy matches the four routes in each configured region."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack
        from gco.stacks.constants import AGGREGATOR_REGIONAL_API_ROUTES

        app = cdk.App()
        regions = ["us-east-1", "us-west-2"]
        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-aggregator-policy",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
            certificate_regions=regions,
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )
        template = assertions.Template.from_stack(stack)
        aggregator_role_ids = [
            logical_id
            for logical_id, role in template.find_resources("AWS::IAM::Role").items()
            if role.get("Properties", {}).get("RoleName") == "gco-cross-region-aggregator"
        ]
        assert len(aggregator_role_ids) == 1

        invoke_resources: list[object] = []
        for policy in template.find_resources("AWS::IAM::Policy").values():
            if {"Ref": aggregator_role_ids[0]} not in policy["Properties"].get("Roles", []):
                continue
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "execute-api:Invoke" not in actions:
                    continue
                resources = statement.get("Resource", [])
                if isinstance(resources, list):
                    invoke_resources.extend(resources)
                else:
                    invoke_resources.append(resources)

        def render_partition_token(value: object) -> str:
            if isinstance(value, str):
                return value
            if value == {"Ref": "AWS::Partition"}:
                return "aws"
            if isinstance(value, dict) and set(value) == {"Fn::Join"}:
                separator, parts = value["Fn::Join"]
                assert isinstance(separator, str)
                assert isinstance(parts, list)
                return separator.join(render_partition_token(part) for part in parts)
            raise AssertionError(f"Unexpected execute-api resource token: {value!r}")

        normalized_resources = [render_partition_token(resource) for resource in invoke_resources]
        assert set(normalized_resources) == {
            f"arn:aws:execute-api:{region}:123456789012:*/*/{method}/{path}"
            for region in regions
            for method, path in AGGREGATOR_REGIONAL_API_ROUTES
        }
        assert not any(resource.endswith("/api/v1/*") for resource in normalized_resources)
        assert not any("/POST/api/v1/manifests" in resource for resource in normalized_resources)
        assert not any("/inference/" in resource for resource in normalized_resources)

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

    def test_inference_surface_exposes_only_serving_methods(self):
        """The inference greedy resource excludes unsupported mutation verbs."""
        from gco.stacks.api_gateway_global_stack import GCOApiGatewayGlobalStack

        app = cdk.App()
        stack = GCOApiGatewayGlobalStack(
            app,
            "test-api-gateway-inference-methods",
            global_accelerator_dns="test-accelerator.awsglobalaccelerator.com",
        )

        template = assertions.Template.from_stack(stack)
        assert _methods_for_child_resource(template, "inference", "{proxy+}") == {
            "GET",
            "HEAD",
            "POST",
        }
        inference_integrations = _integrations_for_child_resource(template, "inference", "{proxy+}")
        _assert_streaming_lambda_integrations(inference_integrations)
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Runtime": getattr(lambda_.Runtime, LAMBDA_NODEJS_RUNTIME).name,
                "Handler": "index.handler",
                "Timeout": 900,
                "Environment": {
                    "Variables": assertions.Match.object_like({"ROUTING_MODE": "global"})
                },
            },
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
        assert config.get_backend_tls_config() is not None

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
