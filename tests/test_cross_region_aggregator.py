"""
Tests for the cross-region aggregator Lambda (lambda/cross-region-aggregator).

Covers CloudFormation-based regional API discovery, per-region SigV4
requests over AWS-managed HTTPS, and the higher-level aggregate_* helpers
that merge job lists, health status, metrics, and bulk-delete results
across every discovered region. Also drives the API Gateway Lambda
handler surface.

The ``lambda/`` directory isn't on the normal import path, so the
handler is loaded by file path under a unique ``sys.modules`` name
via :func:`tests._lambda_imports.load_lambda_module`. That avoids
the ``sys.path.insert('lambda/foo') + import handler`` pattern used
elsewhere, which would otherwise collide with other Lambda handler
tests running in the same pytest session.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

handler = load_lambda_module("cross-region-aggregator")


@pytest.fixture(autouse=True)
def _reset_endpoints_cache():
    """Reset endpoint state and stub SigV4 signing for transport-focused tests."""
    handler._cached_endpoints = None
    handler._endpoints_cache_time = time.monotonic()
    with (
        patch.dict("os.environ", {"AWS_URL_SUFFIX": "amazonaws.com"}),
        patch.object(
            handler,
            "_sigv4_headers",
            return_value={"Authorization": "AWS4-HMAC-SHA256 test-signature"},
        ),
    ):
        yield


class TestGetRegionalEndpoints:
    """Tests for fail-closed regional API stack discovery."""

    def test_get_regional_endpoints_success(self):
        endpoints_by_region = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod/",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod/",
        }

        def client(service_name, region_name=None):
            assert service_name == "cloudformation"
            mock_cfn = MagicMock()
            mock_cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "RegionalApiEndpoint",
                                "OutputValue": endpoints_by_region[region_name],
                            }
                        ]
                    }
                ]
            }
            return mock_cfn

        with (
            patch.dict(
                "os.environ",
                {
                    "PROJECT_NAME": "gco",
                    "TARGET_REGIONS": json.dumps(["us-east-1", "us-west-2"]),
                },
            ),
            patch.object(handler.boto3, "client", side_effect=client),
        ):
            endpoints = handler.get_regional_endpoints()

        assert endpoints == {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }

    def test_missing_regional_bridge_fails_closed(self):
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {"Stacks": []}

        with (
            patch.dict(
                "os.environ",
                {"PROJECT_NAME": "gco", "TARGET_REGIONS": '["us-east-1"]'},
            ),
            patch.object(handler.boto3, "client", return_value=mock_cfn),
            pytest.raises(RuntimeError, match="regional API bridges are unavailable"),
        ):
            handler.get_regional_endpoints()

    def test_invalid_regional_bridge_url_fails_closed(self):
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Outputs": [
                        {
                            "OutputKey": "RegionalApiEndpoint",
                            "OutputValue": "http://internal-alb.example.com",
                        }
                    ]
                }
            ]
        }

        with (
            patch.dict(
                "os.environ",
                {"PROJECT_NAME": "gco", "TARGET_REGIONS": '["us-east-1"]'},
            ),
            patch.object(handler.boto3, "client", return_value=mock_cfn),
            pytest.raises(RuntimeError, match="regional API bridges are unavailable"),
        ):
            handler.get_regional_endpoints()

    def test_partition_specific_url_suffixes_and_eusc_region_are_supported(self):
        cases = (
            ("amazonaws.com.cn", "cn-north-1"),
            ("c2s.ic.gov", "us-iso-east-1"),
            ("cloud.adc-e.uk", "us-isof-south-1"),
            ("amazonaws.eu", "eusc-de-east-1"),
        )
        for suffix, region in cases:
            endpoint = f"https://abc123.execute-api.{region}.{suffix}/prod"
            with patch.dict("os.environ", {"AWS_URL_SUFFIX": suffix}):
                assert handler._normalize_regional_api_url(endpoint, region) == endpoint

    def test_get_regional_endpoints_cached(self):
        handler._cached_endpoints = {
            "us-east-1": "https://cached1.execute-api.us-east-1.amazonaws.com/prod",
        }
        handler._endpoints_cache_time = time.monotonic()

        with patch.object(handler.boto3, "client") as mock_client:
            endpoints = handler.get_regional_endpoints()

        assert endpoints == {
            "us-east-1": "https://cached1.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_client.assert_not_called()


class TestQueryRegion:
    """Tests for query_region function."""

    def test_query_region_success(self):
        """Test successful region query."""

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"jobs": [], "count": 0}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
                "GET",
            )

            assert result["_region"] == "us-east-1"
            assert result["_status"] == "success"

    def test_query_region_uses_sigv4(self):
        """The aggregator signs the exact execute-api request with its IAM role."""
        mock_response = MagicMock(status=200, data=b"{}")
        mock_http = MagicMock()
        mock_http.request.return_value = mock_response
        signed_headers = {
            "Authorization": "AWS4-HMAC-SHA256 Credential=test",
            "X-Amz-Date": "20260715T120000Z",
        }

        with (
            patch.object(handler, "http", mock_http),
            patch.object(handler, "_sigv4_headers", return_value=signed_headers) as mock_sign,
        ):
            handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
                "GET",
                query_params={"namespace": "gco-jobs"},
            )

        requested_url = mock_http.request.call_args.args[1]
        assert requested_url == (
            "https://abc123.execute-api.us-east-1.amazonaws.com/prod/api/v1/jobs?namespace=gco-jobs"
        )
        assert requested_url.startswith("https://")
        assert mock_http.request.call_args.kwargs["headers"] == signed_headers
        mock_sign.assert_called_once_with("us-east-1", "GET", requested_url, None)

    def test_query_region_with_query_params(self):
        """Test region query with query parameters."""

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"jobs": []}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
                "GET",
                query_params={"namespace": "default", "status": "running"},
            )

            call_args = mock_http.request.call_args
            url = call_args[0][1]
            assert "namespace=default" in url
            assert "status=running" in url

    def test_query_region_error(self):
        """Test region query with HTTP error."""

        mock_response = MagicMock()
        mock_response.status = 500

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
            )

            assert result["_status"] == "error"
            assert "HTTP 500" in result["_error"]

    def test_query_region_exception(self):
        """Test region query with exception."""

        mock_http = MagicMock()
        mock_http.request.side_effect = Exception("Connection timeout")

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
            )

            assert result["_status"] == "error"
            assert result["_error"] == "Authenticated regional API request failed"


class TestAggregateJobs:
    """Tests for aggregate_jobs function."""

    def test_aggregate_jobs_success(self):
        """Test successful job aggregation."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {
                "jobs": [
                    {"metadata": {"name": "job1", "creationTimestamp": "2024-01-15T10:00:00Z"}},
                ],
                "count": 1,
                "total": 1,
            }
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_jobs(namespace="default", limit=10)

            assert result["regions_queried"] == 2
            assert result["regions_successful"] == 2
            assert len(result["jobs"]) == 2  # One from each region

    def test_aggregate_jobs_with_errors(self):
        """Test job aggregation with some region errors."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }

        def mock_request(*args, **kwargs):
            url = args[1]
            if "us-east-1" in url:
                response = MagicMock()
                response.status = 200
                response.data = json.dumps({"jobs": [], "count": 0, "total": 0}).encode("utf-8")
                return response
            else:
                response = MagicMock()
                response.status = 500
                return response

        mock_http = MagicMock()
        mock_http.request.side_effect = mock_request

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_jobs()

            assert result["regions_queried"] == 2
            assert result["regions_successful"] == 1
            assert result["errors"] is not None


class TestAggregateHealth:
    """Tests for aggregate_health function."""

    def test_aggregate_health_all_healthy(self):
        """Test health aggregation when all regions are healthy."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {
                "status": "healthy",
                "cluster_id": "gco-cluster",
                "kubernetes_api": "healthy",
            }
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_health()

            assert result["overall_status"] == "healthy"
            assert result["healthy_regions"] == 2
            assert result["total_regions"] == 2

    def test_aggregate_health_degraded(self):
        """Test health aggregation when some regions are unhealthy."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }
        call_count = [0]

        def mock_request(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                response = MagicMock()
                response.status = 200
                response.data = json.dumps({"status": "healthy"}).encode("utf-8")
                return response
            else:
                response = MagicMock()
                response.status = 500
                return response

        mock_http = MagicMock()
        mock_http.request.side_effect = mock_request

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_health()

            assert result["overall_status"] == "degraded"
            assert result["healthy_regions"] == 1


class TestAggregateMetrics:
    """Tests for aggregate_metrics function."""

    def test_aggregate_metrics_success(self):
        """Test successful metrics aggregation."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {
                "cluster_id": "gco-us-east-1",
                "templates_count": 5,
                "webhooks_count": 2,
            }
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_metrics()

            assert result["regions_queried"] == 1
            assert result["regions_successful"] == 1
            assert len(result["regions"]) == 1


class TestBulkDeleteJobs:
    """Tests for bulk_delete_jobs function."""

    def test_bulk_delete_jobs_dry_run(self):
        """Test bulk delete with dry run."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {
                "total_matched": 5,
                "deleted_count": 0,
                "failed_count": 0,
            }
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.bulk_delete_jobs(
                namespace="default",
                status="completed",
                older_than_days=7,
                dry_run=True,
            )

            assert result["dry_run"] is True
            assert result["total_matched"] == 5
            assert result["total_deleted"] == 0


class TestLambdaHandler:
    """Tests for lambda_handler function."""

    def test_handler_get_jobs(self):
        """Test handler for GET /global/jobs."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"jobs": [], "count": 0, "total": 0}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            event = {
                "httpMethod": "GET",
                "path": "/api/v1/global/jobs",
                "queryStringParameters": {"namespace": "default"},
            }

            result = handler.lambda_handler(event, None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert "jobs" in body

    def test_handler_get_health(self):
        """Test handler for GET /global/health."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"status": "healthy"}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            event = {
                "httpMethod": "GET",
                "path": "/api/v1/global/health",
            }

            result = handler.lambda_handler(event, None)

            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert "overall_status" in body

    def test_handler_get_status(self):
        """Test handler for GET /global/status."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"cluster_id": "test"}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            event = {
                "httpMethod": "GET",
                "path": "/api/v1/global/status",
            }

            result = handler.lambda_handler(event, None)

            assert result["statusCode"] == 200

    def test_handler_delete_jobs(self):
        """Test handler for DELETE /global/jobs."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {
                "total_matched": 3,
                "deleted_count": 0,
            }
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            event = {
                "httpMethod": "DELETE",
                "path": "/api/v1/global/jobs",
                "body": json.dumps(
                    {"dry_run": True, "status": "completed", "label_selector": "team=ml"}
                ),
            }

            result = handler.lambda_handler(event, None)

            assert result["statusCode"] == 200
            forwarded = json.loads(mock_http.request.call_args.kwargs["body"].decode("utf-8"))
            assert forwarded["label_selector"] == "team=ml"

    def test_handler_not_found(self):
        """Test handler for unknown path."""

        event = {
            "httpMethod": "GET",
            "path": "/api/v1/unknown",
        }

        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 404

    def test_handler_error(self):
        """Test handler error handling."""

        # Force an error by making aggregate_jobs raise an exception
        with patch.object(handler, "aggregate_jobs", side_effect=Exception("Test error")):
            event = {
                "httpMethod": "GET",
                "path": "/api/v1/global/jobs",
            }

            result = handler.lambda_handler(event, None)

            assert result["statusCode"] == 500
            body = json.loads(result["body"])
            assert "error" in body
