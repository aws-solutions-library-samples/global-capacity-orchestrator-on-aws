"""
Extended coverage tests for the cross-region aggregator Lambda.

Fills gaps left by test_cross_region_aggregator.py: 503 responses
from health endpoints treated as success when they carry a valid
JSON status body but as errors otherwise (including undecodable
bodies), DELETE requests with a body, aggregate_jobs limit trimming
and creationTimestamp sorting, aggregate_health when every region
is unhealthy, bulk_delete_jobs with real deletion (not dry_run),
and the lambda_handler null-query-string/null-body paths.

The handler is loaded via :func:`tests._lambda_imports.load_lambda_module`
so it doesn't collide with other Lambda handler tests in the same
pytest session. See that module for the full rationale.
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
    with patch.object(
        handler,
        "_sigv4_headers",
        return_value={"Authorization": "AWS4-HMAC-SHA256 test-signature"},
    ):
        yield


class TestQueryRegion503Handling:
    """Tests for 503 response handling in query_region."""

    def test_503_with_valid_json_returns_success(self):
        """503 from health endpoint with JSON body should be treated as success."""

        mock_response = MagicMock()
        mock_response.status = 503
        mock_response.data = json.dumps(
            {"status": "degraded", "cluster_id": "gco-us-east-1"}
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/health",
            )

            assert result["_status"] == "success"
            assert result["_region"] == "us-east-1"
            assert result["status"] == "degraded"

    def test_503_with_invalid_json_returns_error(self):
        """503 with non-JSON body should be treated as error."""

        mock_response = MagicMock()
        mock_response.status = 503
        mock_response.data = b"Service Unavailable"

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/health",
            )

            assert result["_status"] == "error"
            assert "HTTP 503" in result["_error"]

    def test_503_with_unicode_error_returns_error(self):
        """503 with undecodable body should be treated as error."""

        mock_response = MagicMock()
        mock_response.status = 503
        mock_response.data = MagicMock()
        mock_response.data.decode.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "invalid")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/health",
            )

            assert result["_status"] == "error"


class TestQueryRegionDeleteMethod:
    """Tests for DELETE method with body in query_region."""

    def test_delete_with_body(self):
        """DELETE request should send body correctly."""

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"deleted_count": 3}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        body = json.dumps({"namespace": "default", "dry_run": False})

        with patch.object(handler, "http", mock_http):
            result = handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
                "DELETE",
                body,
            )

            assert result["_status"] == "success"
            call_args = mock_http.request.call_args
            assert call_args[0][0] == "DELETE"
            assert call_args[1]["body"] == body.encode("utf-8")

    def test_delete_without_body(self):
        """DELETE request without body should send None."""

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"deleted_count": 0}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            handler.query_region(
                "us-east-1",
                "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
                "/api/v1/jobs",
                "DELETE",
            )

            call_args = mock_http.request.call_args
            assert call_args[1]["body"] is None


class TestAggregateJobsLimitTrimming:
    """Tests for aggregate_jobs limit and sorting behavior."""

    def test_jobs_trimmed_to_limit(self):
        """Jobs should be trimmed to the requested limit after aggregation."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }

        def mock_request(*args, **kwargs):
            response = MagicMock()
            response.status = 200
            jobs = [
                {
                    "metadata": {
                        "name": f"job-{i}",
                        "creationTimestamp": f"2024-01-{15 - i:02d}T10:00:00Z",
                    }
                }
                for i in range(5)
            ]
            response.data = json.dumps({"jobs": jobs, "count": 5, "total": 5}).encode("utf-8")
            return response

        mock_http = MagicMock()
        mock_http.request.side_effect = mock_request

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_jobs(limit=3)

            # 10 total jobs (5 per region), trimmed to 3
            assert result["count"] == 3
            assert len(result["jobs"]) == 3

    def test_jobs_sorted_by_creation_time_descending(self):
        """Jobs should be sorted by creationTimestamp descending."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        jobs = [
            {"metadata": {"name": "old-job", "creationTimestamp": "2024-01-01T10:00:00Z"}},
            {"metadata": {"name": "new-job", "creationTimestamp": "2024-01-15T10:00:00Z"}},
            {"metadata": {"name": "mid-job", "creationTimestamp": "2024-01-10T10:00:00Z"}},
        ]
        mock_response.data = json.dumps({"jobs": jobs, "count": 3, "total": 3}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_jobs(limit=10)

            names = [j["metadata"]["name"] for j in result["jobs"]]
            assert names == ["new-job", "mid-job", "old-job"]

    def test_aggregate_jobs_with_namespace_and_status_filters(self):
        """Namespace and status filters should be passed as query params."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"jobs": [], "count": 0, "total": 0}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            handler.aggregate_jobs(namespace="prod", status="running", limit=25)

            call_args = mock_http.request.call_args
            url = call_args[0][1]
            assert "namespace=prod" in url
            assert "status=running" in url
            # limit * 2 = 50 per region
            assert "limit=50" in url


class TestAggregateHealthAllUnhealthy:
    """Tests for aggregate_health when all regions are unhealthy."""

    def test_all_regions_unhealthy(self):
        """When all regions fail, overall_status should be 'unhealthy'."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }
        mock_response = MagicMock()
        mock_response.status = 500

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_health()

            assert result["overall_status"] == "unhealthy"
            assert result["healthy_regions"] == 0
            assert result["total_regions"] == 2

    def test_all_regions_exception(self):
        """When all regions return errors, overall_status should be 'unhealthy'."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_http = MagicMock()
        mock_http.request.side_effect = Exception("Network error")

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_health()

            assert result["overall_status"] == "unhealthy"
            assert result["healthy_regions"] == 0
            # query_region catches exceptions internally, so aggregate_health
            # sees _status="error" and marks the region as "unreachable"
            assert any(r["status"] == "unreachable" for r in result["regions"])

    def test_empty_endpoints(self):
        """When no endpoints exist, should return unhealthy with 0 regions."""

        handler._cached_endpoints = {}
        result = handler.aggregate_health()

        assert result["total_regions"] == 0
        # 0 healthy out of 0 total = unhealthy (0 == 0 is True, but healthy_count == 0)
        assert result["overall_status"] == "unhealthy"


class TestBulkDeleteActualDeletion:
    """Tests for bulk_delete_jobs with actual deletion (not dry_run)."""

    def test_bulk_delete_actual(self):
        """Actual deletion should report deleted counts."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {"total_matched": 5, "deleted_count": 5, "failed_count": 0}
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            result = handler.bulk_delete_jobs(
                namespace="default", status="completed", dry_run=False
            )

            assert result["dry_run"] is False
            assert result["total_deleted"] == 5
            assert result["total_matched"] == 5

    def test_bulk_delete_with_older_than_days(self):
        """older_than_days should be included in request body."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {"total_matched": 2, "deleted_count": 2, "failed_count": 0}
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            handler.bulk_delete_jobs(older_than_days=30, dry_run=False)

            call_args = mock_http.request.call_args
            body = json.loads(call_args[1]["body"].decode("utf-8"))
            assert body["older_than_days"] == 30
            assert body["dry_run"] is False

    def test_bulk_delete_with_region_errors(self):
        """Errors from some regions should be reported."""

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
                response.data = json.dumps(
                    {"total_matched": 3, "deleted_count": 3, "failed_count": 0}
                ).encode("utf-8")
                return response
            else:
                response = MagicMock()
                response.status = 500
                return response

        mock_http = MagicMock()
        mock_http.request.side_effect = mock_request

        with patch.object(handler, "http", mock_http):
            result = handler.bulk_delete_jobs(dry_run=False)

            assert result["errors"] is not None
            assert len(result["errors"]) == 1


class TestLambdaHandlerEdgeCases:
    """Tests for lambda_handler edge cases."""

    def test_handler_null_query_params(self):
        """Handler should handle null queryStringParameters."""

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
                "queryStringParameters": None,
            }

            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200

    def test_handler_missing_query_params_key(self):
        """Handler should handle missing queryStringParameters key."""

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
            }

            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200

    def test_handler_delete_with_null_body(self):
        """DELETE handler should handle null body."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(
            {"total_matched": 0, "deleted_count": 0, "failed_count": 0}
        ).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            event = {
                "httpMethod": "DELETE",
                "path": "/api/v1/global/jobs",
                "body": None,
            }

            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200

    def test_handler_delete_with_invalid_json_body(self):
        """DELETE handler should return 500 on invalid JSON body."""

        event = {
            "httpMethod": "DELETE",
            "path": "/api/v1/global/jobs",
            "body": "not-valid-json{",
        }

        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 500

    def test_handler_get_jobs_with_custom_limit(self):
        """GET jobs should respect custom limit parameter."""

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
                "queryStringParameters": {"limit": "10"},
            }

            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["limit"] == 10

    def test_handler_missing_http_method_defaults_to_get(self):
        """Missing httpMethod should default to GET."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps({"status": "healthy"}).encode("utf-8")

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(handler, "http", mock_http):
            event = {"path": "/api/v1/global/health"}

            result = handler.lambda_handler(event, None)
            assert result["statusCode"] == 200


class TestAggregateMetricsEdgeCases:
    """Tests for aggregate_metrics edge cases."""

    def test_aggregate_metrics_with_errors(self):
        """Metrics aggregation should report errors from failed regions."""

        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
            "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
        }

        def mock_request(method, url, **kwargs):
            response = MagicMock()
            if "east123" in url:
                response.status = 200
                response.data = json.dumps(
                    {"cluster_id": "gco-us-east-1", "templates_count": 3}
                ).encode("utf-8")
            else:
                response.status = 500
                response.data = b""
            return response

        mock_http = MagicMock()
        mock_http.request.side_effect = mock_request

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_metrics()

            assert result["regions_queried"] == 2
            assert result["regions_successful"] == 1
            assert result["errors"] is not None

    def test_aggregate_metrics_with_exception(self):
        """Metrics aggregation should handle exceptions from regions."""

        handler._cached_endpoints = {
            "us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_http = MagicMock()
        mock_http.request.side_effect = Exception("Connection refused")

        with patch.object(handler, "http", mock_http):
            result = handler.aggregate_metrics()

            assert result["regions_successful"] == 0
            assert result["errors"] is not None
