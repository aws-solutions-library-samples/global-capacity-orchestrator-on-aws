"""
Tests for cli/aws_client.py — the CLI's AWS/API client layer.

Covers the RegionalStack and ApiEndpoint dataclasses, the cached
GCOAWSClient (TTL-based endpoint and stack discovery with force-refresh
and invalidation), SigV4-signed request plumbing, and every higher-level
helper the CLI calls: regional job CRUD (list/get/logs/events/pods/
metrics/retry/delete/bulk-delete), global aggregation endpoints
(/api/v1/global/jobs|health|status and bulk-delete), ALB endpoint
discovery, retry/backoff on transient errors, and the regional-vs-API
Gateway routing toggle. Heavy use of boto3.Session and requests.request
mocking; every test stubs get_config to avoid reading real CDK context.
"""

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _cache_regional_endpoint(client: Any, region: str) -> None:
    """Install a fresh regional endpoint without CloudFormation discovery."""
    from cli.aws_client import ApiEndpoint

    client._regional_api_cache[region] = ApiEndpoint(
        url=f"https://regional.execute-api.{region}.amazonaws.com/prod",
        region=region,
        api_id="regional",
        is_regional=True,
    )
    client._cache_timestamp = time.time()


class TestRegionalStack:
    """Tests for RegionalStack dataclass."""

    def test_regional_stack_creation(self):
        """Test creating RegionalStack."""
        from cli.aws_client import RegionalStack

        stack = RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        )

        assert stack.region == "us-east-1"
        assert stack.stack_name == "gco-us-east-1"
        assert stack.status == "CREATE_COMPLETE"
        assert stack.api_endpoint is None
        assert stack.efs_file_system_id is None

    def test_regional_stack_with_file_systems(self):
        """Test RegionalStack with file system IDs."""
        from cli.aws_client import RegionalStack

        stack = RegionalStack(
            region="us-west-2",
            stack_name="gco-us-west-2",
            cluster_name="gco-us-west-2",
            status="CREATE_COMPLETE",
            efs_file_system_id="fs-12345678",
            fsx_file_system_id="fs-abcdef12",
        )

        assert stack.efs_file_system_id == "fs-12345678"
        assert stack.fsx_file_system_id == "fs-abcdef12"


class TestApiEndpoint:
    """Tests for ApiEndpoint dataclass."""

    def test_api_endpoint_creation(self):
        """Test creating ApiEndpoint."""
        from cli.aws_client import ApiEndpoint

        endpoint = ApiEndpoint(
            url="https://abc123.execute-api.us-east-2.amazonaws.com/prod",
            region="us-east-2",
            api_id="abc123",
        )

        assert endpoint.url.startswith("https://")
        assert endpoint.region == "us-east-2"
        assert endpoint.api_id == "abc123"


class TestGCOAWSClient:
    """Tests for GCOAWSClient class."""

    def test_client_initialization(self):
        """Test client initialization."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                api_gateway_region="us-east-2",
                cache_ttl_seconds=300,
            )
            client = GCOAWSClient()
            assert client.config is not None

    def test_cache_validity_no_timestamp(self):
        """Test cache validity when no timestamp set."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)
            client = GCOAWSClient()
            assert client._is_cache_valid() is False

    def test_cache_validity_valid(self):
        """Test cache validity when cache is fresh."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)
            client = GCOAWSClient()
            client._cache_timestamp = time.time()
            assert client._is_cache_valid() is True

    def test_cache_validity_expired(self):
        """Test cache validity when cache is expired."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)
            client = GCOAWSClient()
            client._cache_timestamp = time.time() - 400  # Expired
            assert client._is_cache_valid() is False

    def test_invalidate_cache(self):
        """Test cache invalidation."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)
            client = GCOAWSClient()
            client._cache_timestamp = time.time()
            client._api_endpoint_cache = MagicMock()
            client._regional_stacks_cache = {"us-east-1": MagicMock()}

            client._invalidate_cache()

            assert client._cache_timestamp is None
            assert client._api_endpoint_cache is None
            assert client._regional_stacks_cache is None


class TestGetAWSClient:
    """Tests for get_aws_client factory function."""

    def test_get_aws_client(self):
        """Test factory function returns GCOAWSClient."""
        from cli.aws_client import GCOAWSClient, get_aws_client

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)
            client = get_aws_client()
            assert isinstance(client, GCOAWSClient)

    def test_get_aws_client_with_config(self):
        """Test factory function with custom config."""
        from cli.aws_client import GCOAWSClient, get_aws_client

        custom_config = MagicMock(cache_ttl_seconds=600)
        client = get_aws_client(custom_config)
        assert isinstance(client, GCOAWSClient)
        assert client.config == custom_config


class TestGCOAWSClientApiEndpoint:
    """Tests for get_api_endpoint method."""

    def test_get_api_endpoint_success(self):
        """Test successful API endpoint retrieval."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                api_gateway_region="us-east-1",
                api_gateway_stack_name="gco-api-gateway",
                cache_ttl_seconds=300,
            )

            mock_cfn = MagicMock()
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "ApiEndpoint",
                                "OutputValue": "https://abc123.execute-api.us-east-1.amazonaws.com/prod/",
                            }
                        ]
                    }
                ]
            }

            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                endpoint = client.get_api_endpoint()

                assert endpoint.url == "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
                assert endpoint.api_id == "abc123"

    def test_get_api_endpoint_cached(self):
        """Test API endpoint returns cached value."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            client = GCOAWSClient()
            client._api_endpoint_cache = ApiEndpoint(
                url="https://cached.execute-api.us-east-1.amazonaws.com/prod",
                region="us-east-1",
                api_id="cached",
            )
            client._cache_timestamp = time.time()

            endpoint = client.get_api_endpoint()

            assert endpoint.api_id == "cached"

    def test_get_api_endpoint_no_output(self):
        """Test API endpoint raises error when output not found."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                api_gateway_region="us-east-1",
                api_gateway_stack_name="gco-api-gateway",
                cache_ttl_seconds=300,
            )

            mock_cfn = MagicMock()
            mock_cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}

            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()

                with pytest.raises(RuntimeError, match="Failed to get API endpoint"):
                    client.get_api_endpoint()


class TestGCOAWSClientRegionalStacks:
    """Tests for discover_regional_stacks method."""

    def test_get_regional_stack(self):
        """Test getting a specific regional stack."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            client = GCOAWSClient()
            client._regional_stacks_cache = {
                "us-east-1": RegionalStack(
                    region="us-east-1",
                    stack_name="gco-us-east-1",
                    cluster_name="gco-us-east-1",
                    status="CREATE_COMPLETE",
                )
            }
            client._cache_timestamp = time.time()

            stack = client.get_regional_stack("us-east-1")
            assert stack is not None
            assert stack.region == "us-east-1"

    def test_get_regional_stack_not_found(self):
        """Test getting a non-existent regional stack."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with patch("boto3.Session"):
                client = GCOAWSClient()
                client._regional_stacks_cache = {}
                client._cache_timestamp = time.time()

                stack = client.get_regional_stack("eu-central-1")
                assert stack is None


class TestGCOAWSClientRequests:
    """Tests for authenticated request methods."""

    def test_make_authenticated_request(self):
        """Test making authenticated request."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request("GET", "/api/v1/health")

                assert response.status_code == 200
                mock_request.assert_called_once()
                assert mock_request.call_args.kwargs["stream"] is False

    def test_make_authenticated_request_streams_response_body(self):
        """Streaming requests leave the body unbuffered and allow regional idle gaps."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_session.return_value.get_credentials.return_value = MagicMock()
                mock_request.return_value = MagicMock(status_code=200)

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request(
                    "POST",
                    "/inference/ep/v1/completions",
                    body={"prompt": "hello", "stream": True},
                    stream=True,
                )

                assert response.status_code == 200
                assert mock_request.call_args.kwargs["stream"] is True
                assert mock_request.call_args.kwargs["timeout"] == (10, 310)

    def test_submit_manifests(self):
        """Test submitting manifests."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.ok = True
                mock_response.status_code = 200
                mock_response.json.return_value = {"success": True}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.submit_manifests(
                    manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
                    namespace="default",
                )

                assert result["success"] is True

    def test_submit_manifests_error_with_resources(self):
        """Test submitting manifests with error response containing resource details."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.status_code = 400
                mock_response.reason = "Bad Request"
                mock_response.json.return_value = {
                    "success": False,
                    "resources": [
                        {
                            "name": "my-job",
                            "status": "failed",
                            "message": 'jobs.batch "my-job" already exists',
                        }
                    ],
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                with pytest.raises(RuntimeError, match="my-job.*already exists"):
                    client.submit_manifests(
                        manifests=[{"apiVersion": "batch/v1", "kind": "Job"}],
                        namespace="default",
                    )

    def test_submit_manifests_error_with_message(self):
        """Test submitting manifests with simple error message."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.status_code = 401
                mock_response.reason = "Unauthorized"
                mock_response.json.return_value = {"message": "Invalid credentials"}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                with pytest.raises(RuntimeError, match="Invalid credentials"):
                    client.submit_manifests(
                        manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
                        namespace="default",
                    )

    def test_get_jobs(self):
        """Test getting jobs."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = [{"name": "job1", "status": "running"}]
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                jobs = client.get_jobs(namespace="default")

                assert len(jobs) == 1
                assert jobs[0]["name"] == "job1"

    def test_get_job_details(self):
        """Test getting job details."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"name": "test-job", "status": "completed"}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                details = client.get_job_details("test-job", "default")

                assert details["name"] == "test-job"

    def test_get_job_logs(self):
        """Test getting job logs."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"logs": "Log line 1\nLog line 2"}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                logs = client.get_job_logs("test-job", "default")

                assert "Log line 1" in logs

    def test_delete_job(self):
        """Test deleting a job."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"deleted": True}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.delete_job("test-job", "default")

                assert result["deleted"] is True


class TestGCOAWSClientDiscoverRegionalStacks:
    """Tests for discover_regional_stacks method."""

    def test_discover_regional_stacks_success(self):
        """Test discovering regional stacks."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_regions.return_value = {
                    "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
                }

                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {"OutputKey": "ClusterName", "OutputValue": "gco-us-east-1"},
                                {"OutputKey": "EfsFileSystemId", "OutputValue": "fs-12345"},
                            ],
                            "CreationTime": "2024-01-01T00:00:00Z",
                        }
                    ]
                }

                def client_factory(service, region_name=None):
                    if service == "ec2":
                        return mock_ec2
                    return mock_cfn

                mock_session.return_value.client.side_effect = client_factory

                client = GCOAWSClient()
                stacks = client.discover_regional_stacks()

                assert "us-east-1" in stacks
                assert stacks["us-east-1"].cluster_name == "gco-us-east-1"

    def test_discover_regional_stacks_cached(self):
        """Test discover_regional_stacks returns cached value."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            client = GCOAWSClient()
            client._regional_stacks_cache = {
                "us-east-1": RegionalStack(
                    region="us-east-1",
                    stack_name="gco-us-east-1",
                    cluster_name="gco-us-east-1",
                    status="CREATE_COMPLETE",
                )
            }
            client._cache_timestamp = time.time()

            stacks = client.discover_regional_stacks()

            assert "us-east-1" in stacks

    def test_discover_regional_stacks_force_refresh(self):
        """Test discover_regional_stacks with force_refresh."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {"OutputKey": "ClusterName", "OutputValue": "gco-us-east-1"},
                            ],
                        }
                    ]
                }

                def client_factory(service, region_name=None):
                    if service == "ec2":
                        return mock_ec2
                    return mock_cfn

                mock_session.return_value.client.side_effect = client_factory

                client = GCOAWSClient()
                # Set old cache
                client._regional_stacks_cache = {
                    "us-west-2": RegionalStack(
                        region="us-west-2",
                        stack_name="old",
                        cluster_name="old",
                        status="CREATE_COMPLETE",
                    )
                }
                client._cache_timestamp = time.time()

                stacks = client.discover_regional_stacks(force_refresh=True)

                # Should have new data, not old cache
                assert "us-east-1" in stacks

    def test_discover_regional_stacks_stack_not_found(self):
        """Test discover_regional_stacks when stack doesn't exist."""
        from botocore.exceptions import ClientError

        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_ec2 = MagicMock()
                mock_ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.side_effect = ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
                mock_cfn.exceptions = MagicMock()
                mock_cfn.exceptions.ClientError = ClientError

                def client_factory(service, region_name=None):
                    if service == "ec2":
                        return mock_ec2
                    return mock_cfn

                mock_session.return_value.client.side_effect = client_factory

                client = GCOAWSClient()
                stacks = client.discover_regional_stacks()

                # Should return empty dict when no stacks found
                assert stacks == {}


class TestGCOAWSClientGetRegionalAlbEndpoint:
    """Tests for get_regional_alb_endpoint method."""

    def test_get_regional_alb_endpoint_success(self):
        """Test getting regional ALB endpoint."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "Outputs": [
                                {
                                    "OutputKey": "AlbDnsName",
                                    "OutputValue": "gco-alb-123.us-east-1.elb.amazonaws.com",
                                }
                            ]
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                client._regional_stacks_cache = {
                    "us-east-1": RegionalStack(
                        region="us-east-1",
                        stack_name="gco-us-east-1",
                        cluster_name="gco-us-east-1",
                        status="CREATE_COMPLETE",
                    )
                }
                client._cache_timestamp = time.time()

                alb_endpoint = client.get_regional_alb_endpoint("us-east-1")

                assert alb_endpoint == "gco-alb-123.us-east-1.elb.amazonaws.com"

    def test_get_regional_alb_endpoint_no_stack(self):
        """Test getting regional ALB endpoint when no stack exists."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                client._regional_stacks_cache = {}
                client._cache_timestamp = time.time()

                alb_endpoint = client.get_regional_alb_endpoint("us-east-1")

                assert alb_endpoint is None

    def test_get_regional_alb_endpoint_load_balancer_dns_name(self):
        """Test getting regional ALB endpoint with LoadBalancerDnsName output."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "Outputs": [
                                {
                                    "OutputKey": "LoadBalancerDnsName",
                                    "OutputValue": "gco-lb-456.us-east-1.elb.amazonaws.com",
                                }
                            ]
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                client._regional_stacks_cache = {
                    "us-east-1": RegionalStack(
                        region="us-east-1",
                        stack_name="gco-us-east-1",
                        cluster_name="gco-us-east-1",
                        status="CREATE_COMPLETE",
                    )
                }
                client._cache_timestamp = time.time()

                alb_endpoint = client.get_regional_alb_endpoint("us-east-1")

                assert alb_endpoint == "gco-lb-456.us-east-1.elb.amazonaws.com"

    def test_get_regional_alb_endpoint_error(self):
        """Test getting regional ALB endpoint when API fails."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.side_effect = Exception("API Error")
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                client._regional_stacks_cache = {
                    "us-east-1": RegionalStack(
                        region="us-east-1",
                        stack_name="gco-us-east-1",
                        cluster_name="gco-us-east-1",
                        status="CREATE_COMPLETE",
                    )
                }
                client._cache_timestamp = time.time()

                alb_endpoint = client.get_regional_alb_endpoint("us-east-1")

                assert alb_endpoint is None


class TestGCOAWSClientMakeAuthenticatedRequest:
    """Tests for make_authenticated_request method."""

    def test_make_authenticated_request_with_body(self):
        """Test making authenticated request with body."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request(
                    "POST", "/api/v1/manifests", body={"manifests": []}
                )

                assert response.status_code == 200

    def test_make_authenticated_request_with_target_region(self):
        """Test that an exact region uses its regional API without routing headers."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-west-2")

                response = client.make_authenticated_request(
                    "GET", "/api/v1/jobs", target_region="us-west-2"
                )

                assert response.status_code == 200
                call_kwargs = mock_request.call_args[1]
                assert "regional.execute-api.us-west-2.amazonaws.com" in call_kwargs["url"]
                assert "X-GCO-Target-Region" not in call_kwargs["headers"]

    def test_regional_mode_defaults_omitted_region_to_configured_default(self):
        """Supported unpinned calls use the default regional bridge."""
        from cli.aws_client import GCOAWSClient
        from cli.config import GCOConfig

        config = GCOConfig(
            default_region="eu-west-1",
            use_regional_api=True,
        )
        with (
            patch("boto3.Session") as mock_session,
            patch("requests.request") as mock_request,
            patch("cli.aws_client.SigV4Auth"),
        ):
            mock_session.return_value.get_credentials.return_value = MagicMock()
            mock_response = MagicMock(status_code=200)
            mock_request.return_value = mock_response

            client = GCOAWSClient(config)
            _cache_regional_endpoint(client, "eu-west-1")

            response = client.make_authenticated_request("GET", "/api/v1/jobs")

        assert response is mock_response
        call_kwargs = mock_request.call_args.kwargs
        assert "regional.execute-api.eu-west-1.amazonaws.com" in call_kwargs["url"]
        assert "X-GCO-Target-Region" not in call_kwargs["headers"]

    def test_regional_mode_default_region_missing_bridge_fails_closed(self):
        """An omitted region never falls back to the global API."""
        from cli.aws_client import GCOAWSClient
        from cli.config import GCOConfig

        config = GCOConfig(default_region="eu-west-1", use_regional_api=True)
        with patch("boto3.Session"):
            client = GCOAWSClient(config)
            client.get_regional_api_endpoint = MagicMock(return_value=None)
            client.get_api_endpoint = MagicMock()

            with pytest.raises(
                RuntimeError,
                match="Regional API endpoint is not deployed in eu-west-1",
            ):
                client.make_authenticated_request("GET", "/api/v1/jobs")

        client.get_regional_api_endpoint.assert_called_once_with("eu-west-1")
        client.get_api_endpoint.assert_not_called()

    @pytest.mark.parametrize("target_region", ["", "   "])
    def test_regional_mode_rejects_blank_explicit_region(self, target_region):
        """A blank explicit pin cannot escape strict mode through the global API."""
        from cli.aws_client import GCOAWSClient
        from cli.config import GCOConfig

        config = GCOConfig(default_region="eu-west-1", use_regional_api=True)
        with patch("boto3.Session"):
            client = GCOAWSClient(config)
            client.get_regional_api_endpoint = MagicMock()
            client.get_api_endpoint = MagicMock()

            with pytest.raises(ValueError, match="requires a non-empty.*AWS region"):
                client.make_authenticated_request(
                    "GET",
                    "/api/v1/jobs",
                    target_region=target_region,
                )

        client.get_regional_api_endpoint.assert_not_called()
        client.get_api_endpoint.assert_not_called()

    def test_regional_mode_rejects_blank_default_region(self):
        """An omitted pin also fails closed when the configured default is blank."""
        from cli.aws_client import GCOAWSClient
        from cli.config import GCOConfig

        config = GCOConfig(default_region="", use_regional_api=True)
        with patch("boto3.Session"):
            client = GCOAWSClient(config)
            client.get_regional_api_endpoint = MagicMock()
            client.get_api_endpoint = MagicMock()

            with pytest.raises(ValueError, match="requires a non-empty.*AWS region"):
                client.make_authenticated_request("GET", "/api/v1/jobs")

        client.get_regional_api_endpoint.assert_not_called()
        client.get_api_endpoint.assert_not_called()

    def test_regional_mode_rejects_global_only_operation(self):
        """Global aggregation helpers fail clearly in strict regional mode."""
        from cli.aws_client import GCOAWSClient
        from cli.config import GCOConfig

        config = GCOConfig(default_region="eu-west-1", use_regional_api=True)
        with patch("boto3.Session"):
            client = GCOAWSClient(config)
            client.get_regional_api_endpoint = MagicMock()

            with pytest.raises(
                ValueError,
                match="Global API operations are unavailable in regional API mode",
            ):
                client.get_global_health()

        client.get_regional_api_endpoint.assert_not_called()


class TestGCOAWSClientGetJobs:
    """Tests for get_jobs method."""

    def test_get_jobs_with_all_filters(self):
        """Test getting jobs with all filters."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = []
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-west-2")

                jobs = client.get_jobs(region="us-west-2", namespace="gco-jobs", status="running")

                assert jobs == []
                # Verify query string was built correctly
                call_args = mock_request.call_args
                assert "namespace=gco-jobs" in call_args[1]["url"]
                assert "status=running" in call_args[1]["url"]


# =============================================================================
# Tests for Global Aggregation Methods
# =============================================================================


class TestGCOAWSClientGlobalJobs:
    """Tests for get_global_jobs method."""

    def test_get_global_jobs_success(self):
        """Test getting jobs across all regions."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "jobs": [
                        {"name": "job1", "region": "us-east-1"},
                        {"name": "job2", "region": "us-west-2"},
                    ],
                    "total": 2,
                    "regions_queried": ["us-east-1", "us-west-2"],
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.get_global_jobs(namespace="default", status="running", limit=100)

                assert result["total"] == 2
                assert len(result["jobs"]) == 2
                # Verify correct endpoint was called
                call_args = mock_request.call_args
                assert "/api/v1/global/jobs" in call_args[1]["url"]

    def test_get_global_jobs_with_filters(self):
        """Test getting global jobs with all filters."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"jobs": [], "total": 0}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                client.get_global_jobs(namespace="gco-jobs", status="failed", limit=25)

                call_args = mock_request.call_args
                url = call_args[1]["url"]
                assert "namespace=gco-jobs" in url
                assert "status=failed" in url
                assert "limit=25" in url


class TestGCOAWSClientGlobalHealth:
    """Tests for get_global_health method."""

    def test_get_global_health_success(self):
        """Test getting health status across all regions."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "overall_status": "healthy",
                    "regions": {
                        "us-east-1": {"status": "healthy", "cluster": "gco-us-east-1"},
                        "us-west-2": {"status": "healthy", "cluster": "gco-us-west-2"},
                    },
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.get_global_health()

                assert result["overall_status"] == "healthy"
                assert "us-east-1" in result["regions"]
                call_args = mock_request.call_args
                assert "/api/v1/global/health" in call_args[1]["url"]


class TestGCOAWSClientGlobalStatus:
    """Tests for get_global_status method."""

    def test_get_global_status_success(self):
        """Test getting cluster status across all regions."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "clusters": [
                        {"region": "us-east-1", "name": "gco-us-east-1", "status": "ACTIVE"},
                        {"region": "us-west-2", "name": "gco-us-west-2", "status": "ACTIVE"},
                    ],
                    "total_clusters": 2,
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.get_global_status()

                assert result["total_clusters"] == 2
                call_args = mock_request.call_args
                assert "/api/v1/global/status" in call_args[1]["url"]


class TestGCOAWSClientBulkDeleteGlobal:
    """Tests for bulk_delete_global method."""

    def test_bulk_delete_global_dry_run(self):
        """Test bulk delete across all regions with dry run."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "dry_run": True,
                    "would_delete": [
                        {"name": "old-job-1", "region": "us-east-1"},
                        {"name": "old-job-2", "region": "us-west-2"},
                    ],
                    "total": 2,
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.bulk_delete_global(
                    namespace="default",
                    status="failed",
                    older_than_days=7,
                    label_selector="team=ml",
                    dry_run=True,
                )

                assert result["dry_run"] is True
                assert result["total"] == 2
                request_body = json.loads(mock_request.call_args.kwargs["data"])
                assert request_body["label_selector"] == "team=ml"

    def test_bulk_delete_global_execute(self):
        """Test bulk delete across all regions with actual deletion."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "dry_run": False,
                    "deleted": [
                        {"name": "old-job-1", "region": "us-east-1", "status": "deleted"},
                    ],
                    "total": 1,
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.bulk_delete_global(status="completed", dry_run=False)

                assert result["dry_run"] is False
                assert result["total"] == 1


# =============================================================================
# Tests for Regional Job Operations (New API Endpoints)
# =============================================================================


class TestGCOAWSClientJobEvents:
    """Tests for get_job_events method."""

    def test_get_job_events_success(self):
        """Test getting Kubernetes events for a job."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "job_name": "test-job",
                    "namespace": "default",
                    "events": [
                        {
                            "type": "Normal",
                            "reason": "SuccessfulCreate",
                            "message": "Created pod: test-job-abc123",
                            "timestamp": "2024-01-15T10:30:00Z",
                        },
                        {
                            "type": "Normal",
                            "reason": "Completed",
                            "message": "Job completed",
                            "timestamp": "2024-01-15T10:35:00Z",
                        },
                    ],
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-east-1")

                result = client.get_job_events("test-job", "default", "us-east-1")

                assert result["job_name"] == "test-job"
                assert len(result["events"]) == 2
                call_args = mock_request.call_args
                assert "/api/v1/jobs/default/test-job/events" in call_args[1]["url"]


class TestGCOAWSClientJobPods:
    """Tests for get_job_pods method."""

    def test_get_job_pods_success(self):
        """Test getting pods for a job."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "job_name": "test-job",
                    "namespace": "default",
                    "pods": [
                        {
                            "name": "test-job-abc123",
                            "status": "Succeeded",
                            "node": "ip-10-0-1-100.ec2.internal",
                            "containers": [{"name": "main", "state": "terminated", "exit_code": 0}],
                        }
                    ],
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-east-1")

                result = client.get_job_pods("test-job", "default", "us-east-1")

                assert result["job_name"] == "test-job"
                assert len(result["pods"]) == 1
                assert result["pods"][0]["status"] == "Succeeded"


class TestGCOAWSClientJobMetrics:
    """Tests for get_job_metrics method."""

    def test_get_job_metrics_success(self):
        """Test getting resource metrics for a job."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "job_name": "test-job",
                    "namespace": "default",
                    "pods": [
                        {
                            "name": "test-job-abc123",
                            "containers": [
                                {
                                    "name": "main",
                                    "cpu_usage": "250m",
                                    "memory_usage": "512Mi",
                                }
                            ],
                        }
                    ],
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-east-1")

                result = client.get_job_metrics("test-job", "default", "us-east-1")

                assert result["job_name"] == "test-job"
                assert result["pods"][0]["containers"][0]["cpu_usage"] == "250m"


class TestGCOAWSClientRetryJob:
    """Tests for retry_job method."""

    def test_retry_job_success(self):
        """Test retrying a failed job."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "original_job": "failed-job",
                    "new_job": "failed-job-retry-abc123",
                    "namespace": "default",
                    "status": "created",
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-east-1")

                result = client.retry_job("failed-job", "default", "us-east-1")

                assert result["original_job"] == "failed-job"
                assert "retry" in result["new_job"]
                call_args = mock_request.call_args
                assert "/api/v1/jobs/default/failed-job/retry" in call_args[1]["url"]
                assert call_args[1]["method"] == "POST"


class TestGCOAWSClientBulkDeleteJobs:
    """Tests for bulk_delete_jobs method (regional)."""

    def test_bulk_delete_jobs_dry_run(self):
        """Test bulk delete jobs in a region with dry run."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "dry_run": True,
                    "would_delete": [
                        {"name": "old-job-1", "namespace": "default"},
                        {"name": "old-job-2", "namespace": "default"},
                    ],
                    "total": 2,
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-east-1")

                result = client.bulk_delete_jobs(
                    namespace="default",
                    status="completed",
                    older_than_days=30,
                    region="us-east-1",
                    dry_run=True,
                )

                assert result["dry_run"] is True
                assert result["total"] == 2

    def test_bulk_delete_jobs_with_label_selector(self):
        """Test bulk delete jobs with label selector."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"dry_run": True, "would_delete": [], "total": 0}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-west-2")

                client.bulk_delete_jobs(
                    label_selector="app=test,env=dev",
                    region="us-west-2",
                    dry_run=True,
                )

                call_args = mock_request.call_args
                # Verify the body contains label_selector
                import json

                body = json.loads(call_args[1]["data"])
                assert body["label_selector"] == "app=test,env=dev"


class TestGCOAWSClientGetHealth:
    """Tests for get_health method (regional)."""

    def test_get_health_success(self):
        """Test getting health status for a specific region."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "status": "healthy",
                    "cluster": "gco-us-east-1",
                    "region": "us-east-1",
                    "components": {
                        "api": "healthy",
                        "scheduler": "healthy",
                        "nodes": "healthy",
                    },
                }
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()
                _cache_regional_endpoint(client, "us-east-1")

                result = client.get_health("us-east-1")

                assert result["status"] == "healthy"
                assert result["region"] == "us-east-1"
                call_args = mock_request.call_args
                assert "/api/v1/health" in call_args[1]["url"]
                assert "regional.execute-api.us-east-1.amazonaws.com" in call_args[1]["url"]
                assert "X-GCO-Target-Region" not in call_args[1]["headers"]


class TestGCOAWSClientRetryLogic:
    """Tests for retry logic in make_authenticated_request."""

    def test_retry_on_503(self):
        """Test that 503 responses trigger retries."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
                patch("time.sleep"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                # First two calls return 503, third succeeds
                mock_503 = MagicMock(status_code=503)
                mock_200 = MagicMock(status_code=200)
                mock_request.side_effect = [mock_503, mock_503, mock_200]

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request("GET", "/api/v1/health")
                assert response.status_code == 200
                assert mock_request.call_count == 3

    def test_no_retry_on_400(self):
        """Test that 400 responses are not retried."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock(status_code=400)
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request("GET", "/api/v1/health")
                assert response.status_code == 400
                assert mock_request.call_count == 1

    def test_retry_exhausted_returns_last_response(self):
        """Test that exhausted retries return the last response."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
                patch("time.sleep"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_429 = MagicMock(status_code=429)
                mock_request.return_value = mock_429

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request("GET", "/api/v1/health")
                assert response.status_code == 429
                assert mock_request.call_count == 3

    def test_403_retries_with_fresh_credentials(self):
        """Test that 403 triggers a credential refresh and retry."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                # First call returns 403 (expired creds), second succeeds
                mock_403 = MagicMock(status_code=403)
                mock_200 = MagicMock(status_code=200)
                mock_request.side_effect = [mock_403, mock_200]

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request("GET", "/api/v1/health")
                assert response.status_code == 200
                # Should have created a new session for the retry
                assert mock_session.call_count >= 2

    def test_403_only_retries_once(self):
        """Test that 403 retry only happens once, not infinitely."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                # Both calls return 403
                mock_403 = MagicMock(status_code=403)
                mock_request.return_value = mock_403

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                response = client.make_authenticated_request("GET", "/api/v1/health")
                # Should return 403 after one retry, not loop forever
                assert response.status_code == 403

    def test_no_credentials_raises_runtime_error(self):
        """Test that missing credentials raises a clear error."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_session.return_value.get_credentials.return_value = None

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                with pytest.raises(RuntimeError, match="No AWS credentials found"):
                    client.make_authenticated_request("GET", "/api/v1/health")


class TestGCOAWSClientCallApi:
    """Tests for call_api method with improved error handling."""

    def test_call_api_success(self):
        """Test successful call_api."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.ok = True
                mock_response.json.return_value = {"data": "test"}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                result = client.call_api("GET", "/api/v1/test")
                assert result["data"] == "test"

    def test_call_api_error_with_message(self):
        """Test call_api raises RuntimeError with error message from response."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.status_code = 404
                mock_response.reason = "Not Found"
                mock_response.json.return_value = {"message": "Resource not found"}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                with pytest.raises(RuntimeError, match="Resource not found"):
                    client.call_api("GET", "/api/v1/test")

    def test_call_api_url_encodes_params(self):
        """Test that call_api URL-encodes query parameters."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(cache_ttl_seconds=300)

            with (
                patch("boto3.Session") as mock_session,
                patch("requests.request") as mock_request,
                patch("cli.aws_client.SigV4Auth"),
            ):
                mock_credentials = MagicMock()
                mock_session.return_value.get_credentials.return_value = mock_credentials

                mock_response = MagicMock()
                mock_response.ok = True
                mock_response.json.return_value = {}
                mock_request.return_value = mock_response

                client = GCOAWSClient()
                client._api_endpoint_cache = ApiEndpoint(
                    url="https://api.example.com/prod",
                    region="us-east-1",
                    api_id="test",
                )
                client._cache_timestamp = time.time()

                client.call_api(
                    "GET",
                    "/api/v1/test",
                    params={"key": "value with spaces", "label": "app=nginx"},
                )

                call_url = mock_request.call_args[1]["url"]
                assert "value%20with%20spaces" in call_url
                assert "app%3Dnginx" in call_url


class TestGCOAWSClientOptimizedDiscovery:
    """Tests for optimized region discovery."""

    def test_discover_checks_configured_regions_first(self):
        """Test that discover_regional_stacks checks configured regions first."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {"OutputKey": "ClusterName", "OutputValue": "gco-us-east-1"},
                            ],
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()

                with patch.object(client, "_get_configured_regions", return_value=["us-east-1"]):
                    stacks = client.discover_regional_stacks()

                    assert "us-east-1" in stacks
                    # Should NOT have called ec2.describe_regions since configured regions found stacks
                    for call in mock_session.return_value.client.call_args_list:
                        assert call[0][0] != "ec2"


# =========================================================================
# Additional coverage tests targeting uncovered lines
# =========================================================================


class TestGCOAWSClientGetRegionalApiEndpoint:
    """Tests for get_regional_api_endpoint covering lines 117, 133, 145-146."""

    def test_get_regional_api_endpoint_success(self):
        """Test successful regional API endpoint discovery (line 117+)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {
                                    "OutputKey": "RegionalApiEndpoint",
                                    "OutputValue": "https://abc123.execute-api.us-east-1.amazonaws.com/prod/",
                                },
                            ],
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                endpoint = client.get_regional_api_endpoint("us-east-1")

                assert endpoint is not None
                assert endpoint.url == "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
                assert endpoint.is_regional is True
                assert endpoint.api_id == "abc123"

    def test_get_regional_api_endpoint_cached(self):
        """Test that cached regional endpoint is returned (line 117 cache hit)."""
        from cli.aws_client import ApiEndpoint, GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()
                cached_endpoint = ApiEndpoint(
                    url="https://cached.execute-api.us-east-1.amazonaws.com/prod",
                    region="us-east-1",
                    api_id="cached",
                    is_regional=True,
                )
                client._regional_api_cache["us-east-1"] = cached_endpoint
                client._cache_timestamp = time.time()

                result = client.get_regional_api_endpoint("us-east-1")
                assert result == cached_endpoint

    def test_get_regional_api_endpoint_no_output(self):
        """Test regional endpoint when stack has no RegionalApiEndpoint output (line 133)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {
                                    "OutputKey": "SomeOtherOutput",
                                    "OutputValue": "some-value",
                                },
                            ],
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                result = client.get_regional_api_endpoint("us-east-1")
                assert result is None

    def test_get_regional_api_endpoint_client_error(self):
        """Test regional endpoint when stack doesn't exist (line 145)."""
        from botocore.exceptions import ClientError

        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.exceptions.ClientError = ClientError
                mock_cfn.describe_stacks.side_effect = ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack not found"}},
                    "DescribeStacks",
                )
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                result = client.get_regional_api_endpoint("us-east-1")
                assert result is None

    def test_get_regional_api_endpoint_generic_exception(self):
        """Test regional endpoint when unexpected error occurs (line 146)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.exceptions.ClientError = Exception
                mock_cfn.describe_stacks.side_effect = RuntimeError("Unexpected error")
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                result = client.get_regional_api_endpoint("us-east-1")
                assert result is None


class TestGCOAWSClientDiscoverRegionalStacksFallback:
    """Tests for discover_regional_stacks fallback to all regions (lines 229-231)."""

    def test_discover_falls_back_to_all_regions(self):
        """Test fallback to scanning all AWS regions when configured regions have no stacks (lines 229-231)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn_configured = MagicMock()
                mock_cfn_configured.describe_stacks.side_effect = Exception("not found")
                mock_cfn_configured.exceptions.ClientError = Exception

                mock_ec2 = MagicMock()
                mock_ec2.describe_regions.return_value = {
                    "Regions": [
                        {"RegionName": "us-east-1"},
                        {"RegionName": "eu-west-1"},
                    ]
                }

                def client_factory(service, region_name=None):
                    if service == "ec2":
                        return mock_ec2
                    return mock_cfn_configured

                mock_session.return_value.client.side_effect = client_factory

                client = GCOAWSClient()

                with (
                    patch.object(client, "_get_configured_regions", return_value=["us-east-1"]),
                    patch.object(client, "_probe_regional_stack") as mock_probe,
                ):
                    # Configured region returns None, fallback region returns a stack
                    mock_probe.side_effect = lambda r: (
                        MagicMock(region=r) if r == "eu-west-1" else None
                    )
                    stacks = client.discover_regional_stacks()
                    assert "eu-west-1" in stacks


class TestGCOAWSClientProbeRegionalStack:
    """Tests for _probe_regional_stack covering lines 277-278."""

    def test_probe_regional_stack_client_error(self):
        """Test _probe_regional_stack returns None on ClientError (line 277)."""
        from botocore.exceptions import ClientError

        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.exceptions.ClientError = ClientError
                mock_cfn.describe_stacks.side_effect = ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack not found"}},
                    "DescribeStacks",
                )
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                result = client._probe_regional_stack("us-east-1")
                assert result is None

    def test_probe_regional_stack_generic_exception(self):
        """Test _probe_regional_stack returns None on generic exception (line 278)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_session.return_value.client.side_effect = RuntimeError("Connection error")

                client = GCOAWSClient()
                result = client._probe_regional_stack("us-east-1")
                assert result is None


class TestGCOAWSClientCallApiErrorPaths:
    """Tests for call_api error handling covering lines 333, 336-339."""

    def test_call_api_error_with_error_key(self):
        """Test call_api extracts 'error' key from response (line 336)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.status_code = 500
                mock_response.reason = "Internal Server Error"
                mock_response.json.return_value = {"error": "Something went wrong"}

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Something went wrong"),
                ):
                    client.call_api("GET", "/api/v1/test", region="us-east-1")

    def test_call_api_error_with_detail_key(self):
        """Test call_api extracts 'detail' key from response (line 339)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.status_code = 400
                mock_response.reason = "Bad Request"
                mock_response.json.return_value = {"detail": "Invalid parameters"}

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Invalid parameters"),
                ):
                    client.call_api("GET", "/api/v1/test", region="us-east-1")

    def test_call_api_error_json_decode_error(self):
        """Test call_api falls back to response.text on JSON decode error (line 333)."""
        import json

        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.status_code = 502
                mock_response.reason = "Bad Gateway"
                mock_response.json.side_effect = json.JSONDecodeError("err", "doc", 0)
                mock_response.text = "Bad Gateway HTML response"

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Bad Gateway HTML response"),
                ):
                    client.call_api("GET", "/api/v1/test", region="us-east-1")


class TestGCOAWSClientGetJobLogsErrorPaths2:
    """Tests for get_job_logs error handling covering lines 569-574."""

    def test_get_job_logs_error_with_detail_key(self):
        """Test get_job_logs extracts detail from error response (lines 569-574)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.reason = "Not Found"
                mock_response.json.return_value = {"detail": "Job logs not found"}

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Job logs not found"),
                ):
                    client.get_job_logs("test-job", "default", "us-east-1")

    def test_get_job_logs_error_json_parse_failure(self):
        """Test get_job_logs falls back to response.text when JSON parsing fails (line 573)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.reason = "Internal Server Error"
                mock_response.json.side_effect = ValueError("No JSON")
                mock_response.text = "Server error text"

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Server error text"),
                ):
                    client.get_job_logs("test-job", "default", "us-east-1")

    def test_get_job_logs_error_falls_back_to_reason(self):
        """Test get_job_logs falls back to reason when text is empty."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.reason = "Internal Server Error"
                mock_response.json.side_effect = ValueError("No JSON")
                mock_response.text = ""

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Internal Server Error"),
                ):
                    client.get_job_logs("test-job", "default", "us-east-1")


class TestGCOAWSClientGetRegionalAlbEndpointCoverage:
    """Tests for get_regional_alb_endpoint covering line 612."""

    def test_get_regional_alb_endpoint_no_alb_output(self):
        """Test get_regional_alb_endpoint when no ALB output exists (line 612)."""
        from cli.aws_client import GCOAWSClient, RegionalStack

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session") as mock_session:
                mock_cfn = MagicMock()
                mock_cfn.describe_stacks.return_value = {
                    "Stacks": [
                        {
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {"OutputKey": "ClusterName", "OutputValue": "test-cluster"},
                            ],
                        }
                    ]
                }
                mock_session.return_value.client.return_value = mock_cfn

                client = GCOAWSClient()
                test_stack = RegionalStack(
                    region="us-east-1",
                    stack_name="gco-us-east-1",
                    cluster_name="test-cluster",
                    status="CREATE_COMPLETE",
                )

                with patch.object(client, "get_regional_stack", return_value=test_stack):
                    result = client.get_regional_alb_endpoint("us-east-1")
                    # No AlbDnsName or LoadBalancerDnsName in outputs
                    assert result is None


class TestGCOAWSClientGetPodLogs:
    """Tests for get_pod_logs covering lines 792-806."""

    def test_get_pod_logs_success(self):
        """Test successful pod logs retrieval."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()
                mock_response.json.return_value = {"logs": "line1\nline2\nline3"}

                with patch.object(client, "make_authenticated_request", return_value=mock_response):
                    result = client.get_pod_logs(
                        job_name="test-job",
                        pod_name="test-pod-abc",
                        namespace="default",
                        region="us-east-1",
                    )
                    assert result == {"logs": "line1\nline2\nline3"}

    def test_get_pod_logs_with_container(self):
        """Test pod logs with container parameter (line 795)."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.raise_for_status = MagicMock()
                mock_response.json.return_value = {"logs": "container logs"}

                with patch.object(
                    client, "make_authenticated_request", return_value=mock_response
                ) as mock_req:
                    result = client.get_pod_logs(
                        job_name="test-job",
                        pod_name="test-pod-abc",
                        namespace="default",
                        region="us-east-1",
                        tail_lines=50,
                        container="sidecar",
                    )
                    assert result == {"logs": "container logs"}
                    # Verify the path includes container param
                    call_kwargs = mock_req.call_args
                    assert "container=sidecar" in call_kwargs.kwargs.get(
                        "path", call_kwargs[1].get("path", "")
                    )

    def test_get_pod_logs_error(self):
        """Test pod logs when API returns error."""
        import requests

        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
                    "404 Not Found"
                )

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(requests.exceptions.HTTPError),
                ):
                    client.get_pod_logs(
                        job_name="test-job",
                        pod_name="nonexistent-pod",
                        namespace="default",
                        region="us-east-1",
                    )


class TestGCOAWSClientGetJobLogsErrorPaths:
    """Tests for get_job_logs error handling covering lines 569-574."""

    def test_get_job_logs_error_with_detail(self):
        """Test get_job_logs extracts detail from error response."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.reason = "Not Found"
                mock_response.json.return_value = {"detail": "Job logs not available"}

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Job logs not available"),
                ):
                    client.get_job_logs("test-job", "default", "us-east-1")

    def test_get_job_logs_error_no_detail(self):
        """Test get_job_logs falls back to reason when no detail key."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()

                mock_response = MagicMock()
                mock_response.ok = False
                mock_response.reason = "Internal Server Error"
                mock_response.json.return_value = {"other_key": "value"}

                with (
                    patch.object(client, "make_authenticated_request", return_value=mock_response),
                    pytest.raises(RuntimeError, match="Internal Server Error"),
                ):
                    client.get_job_logs("test-job", "default", "us-east-1")


class TestGCOAWSClientSetUseRegionalApi:
    """Tests for set_use_regional_api and regional routing."""

    def test_set_use_regional_api(self):
        """Test setting regional API flag."""
        from cli.aws_client import GCOAWSClient

        with patch("cli.aws_client.get_config") as mock_config:
            mock_config.return_value = MagicMock(
                regional_stack_prefix="gco",
                project_name="gco",
                cache_ttl_seconds=300,
            )

            with patch("boto3.Session"):
                client = GCOAWSClient()
                assert client._use_regional_api is False

                client.set_use_regional_api(True)
                assert client._use_regional_api is True

                client.set_use_regional_api(False)
                assert client._use_regional_api is False
