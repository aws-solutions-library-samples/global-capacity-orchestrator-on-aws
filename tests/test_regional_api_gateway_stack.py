"""
Tests for gco/stacks/regional_api_gateway_stack.GCORegionalApiGatewayStack.

Synthesizes the per-region API Gateway stack against a mock ConfigLoader
that returns regional_api_enabled=true plus a throttle config, a stand-in
VPC, and a placeholder ALB DNS, then asserts against the CloudFormation
template: the REST API is created with REGIONAL endpoint configuration,
the VPC Lambda proxy exists with the expected function name/runtime/handler,
and the security group wiring is correct. No Docker or real AWS calls.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk import aws_ec2 as ec2


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


class TestRegionalApiGatewayStack:
    """Test cases for GCORegionalApiGatewayStack."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config loader."""
        config = MagicMock()
        config.get_project_name.return_value = "gco"
        config.get_global_region.return_value = "us-east-2"
        config.get_deployment_regions.return_value = {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-east-1"],
        }
        config.get_api_gateway_config.return_value = {
            "regional_api_enabled": True,
            "throttle_rate_limit": 37,
            "throttle_burst_limit": 73,
            "log_level": "ERROR",
            "metrics_enabled": False,
            "tracing_enabled": False,
        }
        config.get_backend_tls_config.return_value = {
            "trust_cache_ttl_seconds": 120,
            "trust_cache_max_stale_seconds": 900,
        }
        return config

    @pytest.fixture
    def app(self):
        """Create a CDK app."""
        return cdk.App()

    @pytest.fixture
    def vpc(self, app):
        """Create a mock VPC."""
        stack = cdk.Stack(app, "VpcStack", env=cdk.Environment(region="us-east-1"))
        return ec2.Vpc(stack, "TestVpc", max_azs=2)

    def test_regional_api_gateway_stack_creation(self, app, mock_config, vpc):
        """Test that the regional API gateway stack can be created."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify API Gateway is created
        template.has_resource_properties(
            "AWS::ApiGateway::RestApi",
            {
                "Name": "gco-regional-api-us-east-1",
                "EndpointConfiguration": {"Types": ["REGIONAL"]},
            },
        )
        template.has_resource_properties(
            "AWS::ApiGateway::Stage",
            {
                "MethodSettings": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
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
        assert _methods_for_child_resource(template, "inference", "{proxy+}") == {
            "GET",
            "HEAD",
            "POST",
        }
        inference_integrations = _integrations_for_child_resource(template, "inference", "{proxy+}")
        assert len(inference_integrations) == 3
        assert all(
            integration["ResponseTransferMode"] == "STREAM"
            and integration["TimeoutInMillis"] == 900000
            for integration in inference_integrations
        )
        control_integrations = _integrations_for_child_resource(template, "v1", "{proxy+}")
        assert control_integrations
        assert all("ResponseTransferMode" not in item for item in control_integrations)
        assert all(item["TimeoutInMillis"] == 29000 for item in control_integrations)

    def test_regional_api_gateway_has_lambda(self, app, mock_config, vpc):
        """Test that the regional API gateway has a VPC Lambda."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify Lambda function is created
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "FunctionName": "gco-regional-proxy-us-east-1",
                "Runtime": "python3.14",
                "Handler": "handler.lambda_handler",
                "Timeout": 29,
            },
        )
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "FunctionName": "gco-regional-inference-proxy-us-east-1",
                "Runtime": "nodejs22.x",
                "Handler": "index.handler",
                "Timeout": 900,
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "ROUTING_MODE": "regional",
                            "TARGET_REGION": "us-east-1",
                            "REGISTRY_REGION": "us-east-2",
                        }
                    )
                },
                "VpcConfig": assertions.Match.object_like(
                    {
                        "SecurityGroupIds": assertions.Match.any_value(),
                        "SubnetIds": assertions.Match.any_value(),
                    }
                ),
            },
        )
        inference_functions = [
            resource
            for resource in template.find_resources("AWS::Lambda::Function").values()
            if resource.get("Properties", {}).get("FunctionName")
            == "gco-regional-inference-proxy-us-east-1"
        ]
        assert len(inference_functions) == 1
        vpc_config = inference_functions[0]["Properties"]["VpcConfig"]
        assert vpc_config["SecurityGroupIds"]
        assert vpc_config["SubnetIds"]

    def test_regional_api_gateway_has_security_group(self, app, mock_config, vpc):
        """Test that the regional API gateway Lambda has a security group."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify security group is created
        template.has_resource_properties(
            "AWS::EC2::SecurityGroup",
            {
                "GroupDescription": "Security group for regional API proxy Lambdas",
            },
        )

    def test_regional_api_gateway_has_iam_role(self, app, mock_config, vpc):
        """Test that the regional API gateway Lambda has an IAM role."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify IAM role is created (without explicit name - CDK generates unique name)
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        {
                            "Action": "sts:AssumeRole",
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                        }
                    ],
                },
            },
        )

        # Registry and public-root reads are constrained to the two exact parameters.
        template.has_resource_properties(
            "AWS::IAM::Policy",
            {
                "PolicyDocument": {
                    "Statement": assertions.Match.array_with(
                        [
                            assertions.Match.object_like(
                                {
                                    "Action": "ssm:GetParameter",
                                    "Effect": "Allow",
                                    "Resource": [
                                        stack.resolve(
                                            f"arn:{stack.partition}:ssm:us-east-2:{stack.account}:"
                                            "parameter/gco/alb-hostname-us-east-1"
                                        ),
                                        stack.resolve(
                                            f"arn:{stack.partition}:ssm:us-east-2:{stack.account}:"
                                            "parameter/gco/backend-tls/root-ca.pem"
                                        ),
                                    ],
                                }
                            )
                        ]
                    )
                }
            },
        )

    def test_regional_api_gateway_has_log_groups(self, app, mock_config, vpc):
        """Test that the regional API gateway has CloudWatch log groups."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify log groups are created
        template.resource_count_is("AWS::Logs::LogGroup", 3)

    def test_regional_api_gateway_has_output(self, app, mock_config, vpc):
        """Test that the regional API gateway exports its endpoint."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify output is created
        template.has_output(
            "RegionalApiEndpoint",
            {
                "Description": "Regional API Gateway endpoint for us-east-1",
            },
        )

    def test_regional_api_gateway_lambda_environment(self, app, mock_config, vpc):
        """Test that the Lambda has correct environment variables."""
        from gco.stacks.regional_api_gateway_stack import GCORegionalApiGatewayStack

        stack = GCORegionalApiGatewayStack(
            app,
            "TestRegionalApiStack",
            config=mock_config,
            region="us-east-1",
            vpc=vpc,
            alb_dns_name="internal-test-alb.elb.amazonaws.com",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID, not a real secret
            aggregator_role_arn="arn:aws:iam::123456789012:role/gco-test-aggregator",
            env=cdk.Environment(region="us-east-1"),
        )

        template = assertions.Template.from_stack(stack)

        # Verify Lambda environment variables
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "ALB_ENDPOINT": "internal-test-alb.elb.amazonaws.com",
                            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN with fake account ID
                            "BACKEND_TLS_SERVER_NAME": "backend.gco.gco.internal",
                            "BACKEND_TLS_ROOT_CA_PARAMETER": "/gco/backend-tls/root-ca.pem",
                            "BACKEND_TLS_ROOT_CA_REGION": "us-east-2",
                            "BACKEND_TLS_CA_CACHE_TTL_SECONDS": "120",
                            "BACKEND_TLS_CA_MAX_STALE_SECONDS": "900",
                        }
                    )
                }
            },
        )


def _load_lambda_handler():
    """Load the regional API proxy Lambda handler module dynamically."""
    # Ensure proxy_utils is importable (it lives in lambda/proxy-shared/)
    proxy_shared_path = str(Path(__file__).parent.parent / "lambda" / "proxy-shared")
    if proxy_shared_path not in sys.path:
        sys.path.insert(0, proxy_shared_path)

    # proxy_utils creates boto3.client and urllib3.PoolManager at import time,
    # so we need to mock them before loading the handler
    from unittest.mock import patch

    with (
        patch("boto3.client"),
        patch("urllib3.PoolManager"),
        patch.dict(
            "os.environ",
            {
                "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                "SECRET_CACHE_TTL_SECONDS": "300",
                "PROXY_MAX_RETRIES": "3",
                "PROXY_RETRY_BACKOFF_BASE": "0",
                "ALB_ENDPOINT": "internal-test-alb.elb.amazonaws.com",
            },
        ),
    ):
        # Clear any cached modules
        sys.modules.pop("proxy_utils", None)
        sys.modules.pop("regional_api_proxy_handler", None)

        # Get the path to the handler file
        handler_path = Path(__file__).parent.parent / "lambda" / "regional-api-proxy" / "handler.py"

        # Load the module using importlib
        spec = importlib.util.spec_from_file_location("regional_api_proxy_handler", handler_path)
        if spec is None or spec.loader is None:
            pytest.skip("Could not load regional API proxy handler")

        module = importlib.util.module_from_spec(spec)
        sys.modules["regional_api_proxy_handler"] = module
        spec.loader.exec_module(module)
        return module


def _load_proxy_utils():
    """Load the shared proxy_utils module dynamically.

    Returns the module from sys.modules if already loaded (e.g., by the handler),
    to ensure patches affect the same module instance the handler uses.
    """
    if "proxy_utils" in sys.modules:
        return sys.modules["proxy_utils"]
    utils_path = Path(__file__).parent.parent / "lambda" / "regional-api-proxy" / "proxy_utils.py"
    spec = importlib.util.spec_from_file_location("proxy_utils", utils_path)
    if spec is None or spec.loader is None:
        pytest.skip("Could not load proxy_utils")
    module = importlib.util.module_from_spec(spec)
    sys.modules["proxy_utils"] = module
    spec.loader.exec_module(module)
    return module


class TestRegionalApiProxyHandler:
    """Test cases for the regional API proxy Lambda handler."""

    @pytest.fixture
    def handler_module(self):
        """Load the handler module (also loads proxy_utils into sys.modules)."""
        return _load_lambda_handler()

    @pytest.fixture
    def proxy_utils_module(self, handler_module):
        """Load the proxy_utils module for patching (must depend on handler_module)."""
        return _load_proxy_utils()

    def test_get_secret_token_caches_result(self, handler_module, proxy_utils_module):
        """Test that the secret token is cached."""
        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

        with patch.object(proxy_utils_module, "_secrets_client") as mock_secrets:
            mock_secrets.get_secret_value.return_value = {
                "SecretString": '{"token": "test-token-123"}'
            }

            with patch.dict(
                os.environ, {"SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"}
            ):
                # First call should fetch from Secrets Manager
                token1 = proxy_utils_module.get_secret_token()
                assert token1 == "test-token-123"
                assert mock_secrets.get_secret_value.call_count == 1

                # Second call should use cache
                token2 = proxy_utils_module.get_secret_token()
                assert token2 == "test-token-123"
                assert mock_secrets.get_secret_value.call_count == 1  # Still 1

        # Reset cache for other tests
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

    def test_lambda_handler_success(self, handler_module, proxy_utils_module):
        """Test successful request forwarding."""
        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

        with patch.object(proxy_utils_module, "_secrets_client") as mock_secrets:
            mock_secrets.get_secret_value.return_value = {"SecretString": '{"token": "test-token"}'}

            with patch.object(proxy_utils_module, "_http") as mock_http:
                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.headers = {"Content-Type": "application/json"}
                mock_response.data = b'{"status": "ok"}'
                mock_http.request.return_value = mock_response

                with patch.dict(
                    os.environ,
                    {
                        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                        "ALB_ENDPOINT": "internal-alb.elb.amazonaws.com",
                    },
                ):
                    event = {
                        "httpMethod": "GET",
                        "path": "/api/v1/health",
                        "queryStringParameters": None,
                        "headers": {"Content-Type": "application/json"},
                        "body": None,
                    }

                    result = handler_module.lambda_handler(event, None)

                    assert result["statusCode"] == 200
                    assert result["body"] == '{"status": "ok"}'

        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

    def test_lambda_handler_with_query_params(self, handler_module, proxy_utils_module):
        """Test request forwarding with query parameters."""
        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

        with patch.object(proxy_utils_module, "_secrets_client") as mock_secrets:
            mock_secrets.get_secret_value.return_value = {"SecretString": '{"token": "test-token"}'}

            with patch.object(proxy_utils_module, "_http") as mock_http:
                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.headers = {}
                mock_response.data = b'{"jobs": []}'
                mock_http.request.return_value = mock_response

                with patch.dict(
                    os.environ,
                    {
                        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                        "ALB_ENDPOINT": "internal-alb.elb.amazonaws.com",
                    },
                ):
                    event = {
                        "httpMethod": "GET",
                        "path": "/api/v1/jobs",
                        "queryStringParameters": {"namespace": "gco-jobs", "limit": "10"},
                        "headers": {},
                        "body": None,
                    }

                    handler_module.lambda_handler(event, None)

                    # Verify the URL includes query params
                    call_args = mock_http.request.call_args
                    url = (
                        call_args[1]["target_url"]
                        if "target_url" in (call_args[1] or {})
                        else call_args[0][1]
                    )
                    assert "namespace=gco-jobs" in url
                    assert "limit=10" in url

        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

    def test_lambda_handler_connection_error(self, handler_module, proxy_utils_module):
        """Test handling of connection errors."""
        import urllib3

        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

        with patch.object(proxy_utils_module, "_secrets_client") as mock_secrets:
            mock_secrets.get_secret_value.return_value = {"SecretString": '{"token": "test-token"}'}

            with patch.object(proxy_utils_module, "_http") as mock_http:
                mock_http.request.side_effect = urllib3.exceptions.MaxRetryError(
                    None, "https://test", "Connection refused"
                )

                with patch.dict(
                    os.environ,
                    {
                        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                        "ALB_ENDPOINT": "internal-alb.elb.amazonaws.com",
                    },
                ):
                    event = {
                        "httpMethod": "GET",
                        "path": "/api/v1/health",
                        "queryStringParameters": None,
                        "headers": {},
                        "body": None,
                    }

                    result = handler_module.lambda_handler(event, None)

                    assert result["statusCode"] == 503
                    assert "unavailable" in result["body"].lower()

        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

    def test_lambda_handler_removes_hop_by_hop_headers(self, handler_module, proxy_utils_module):
        """Test that hop-by-hop headers are removed from response."""
        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0

        with patch.object(proxy_utils_module, "_secrets_client") as mock_secrets:
            mock_secrets.get_secret_value.return_value = {"SecretString": '{"token": "test-token"}'}

            with patch.object(proxy_utils_module, "_http") as mock_http:
                mock_response = MagicMock()
                mock_response.status = 200
                mock_response.headers = {
                    "Content-Type": "application/json",
                    "Connection": "keep-alive",
                    "Transfer-Encoding": "chunked",
                }
                mock_response.data = b'{"status": "ok"}'
                mock_http.request.return_value = mock_response

                with patch.dict(
                    os.environ,
                    {
                        "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                        "ALB_ENDPOINT": "internal-alb.elb.amazonaws.com",
                    },
                ):
                    event = {
                        "httpMethod": "GET",
                        "path": "/api/v1/health",
                        "queryStringParameters": None,
                        "headers": {},
                        "body": None,
                    }

                    result = handler_module.lambda_handler(event, None)

                    # Hop-by-hop headers should be removed
                    assert "Connection" not in result["headers"]
                    assert "Transfer-Encoding" not in result["headers"]
                    assert "Content-Type" in result["headers"]

        # Reset cache
        proxy_utils_module._cached_secret = None
        proxy_utils_module._cache_timestamp = 0.0


class TestRegionalApiClientIntegration:
    """Test cases for the CLI's regional API client integration."""

    def test_set_use_regional_api(self):
        """Test setting regional API mode."""
        from cli.aws_client import GCOAWSClient

        client = GCOAWSClient()
        assert client._use_regional_api is False

        client.set_use_regional_api(True)
        assert client._use_regional_api is True

        client.set_use_regional_api(False)
        assert client._use_regional_api is False

    def test_get_regional_api_endpoint_not_found(self):
        """Test getting regional API endpoint when stack doesn't exist."""
        from cli.aws_client import GCOAWSClient

        client = GCOAWSClient()

        with patch.object(client, "_session") as mock_session:
            mock_cfn = MagicMock()
            # Create a proper exception class for ClientError
            mock_cfn.exceptions.ClientError = type("ClientError", (Exception,), {})
            mock_cfn.describe_stacks.side_effect = mock_cfn.exceptions.ClientError(
                "Stack not found"
            )
            mock_session.client.return_value = mock_cfn

            result = client.get_regional_api_endpoint("us-east-1")
            assert result is None

    def test_get_regional_api_endpoint_success(self):
        """Test getting regional API endpoint successfully."""
        from cli.aws_client import GCOAWSClient

        client = GCOAWSClient()

        with patch.object(client, "_session") as mock_session:
            mock_cfn = MagicMock()
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "RegionalApiEndpoint",
                                "OutputValue": "https://abc123.execute-api.us-east-1.amazonaws.com/prod/",
                            }
                        ]
                    }
                ]
            }
            mock_cfn.exceptions = MagicMock()
            mock_cfn.exceptions.ClientError = Exception
            mock_session.client.return_value = mock_cfn

            result = client.get_regional_api_endpoint("us-east-1", force_refresh=True)

            assert result is not None
            assert result.url == "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
            assert result.region == "us-east-1"
            assert result.is_regional is True

    def test_make_authenticated_request_uses_regional_api(self):
        """Test that authenticated requests use regional API when enabled."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        client = GCOAWSClient()
        client.set_use_regional_api(True)

        regional_endpoint = ApiEndpoint(
            url="https://regional.execute-api.us-east-1.amazonaws.com/prod",
            region="us-east-1",
            api_id="regional",
            is_regional=True,
        )

        with (
            patch.object(client, "get_regional_api_endpoint", return_value=regional_endpoint),
            patch.object(client, "get_api_endpoint") as mock_global,
            patch("cli.aws_client.requests") as mock_requests,
            patch.object(client, "_session") as mock_session,
        ):
            mock_creds = MagicMock()
            mock_creds.access_key = "test"  # nosec B105 - test fixture mock credential, not a real key
            mock_creds.secret_key = "test"  # nosec B105 - test fixture mock credential, not a real key
            mock_creds.token = None
            mock_session.get_credentials.return_value = mock_creds

            mock_response = MagicMock()
            mock_response.ok = True
            mock_requests.request.return_value = mock_response

            client.make_authenticated_request(
                method="GET",
                path="/api/v1/health",
                target_region="us-east-1",
            )

            # Should not call global endpoint
            mock_global.assert_not_called()

            # Should use regional endpoint URL
            call_args = mock_requests.request.call_args
            assert "regional.execute-api" in call_args.kwargs["url"]
