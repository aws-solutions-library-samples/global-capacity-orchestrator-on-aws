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
import logging
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import urllib3
from botocore.credentials import Credentials

from tests._lambda_imports import load_lambda_module

handler = load_lambda_module("cross-region-aggregator")
# The autouse fixture below stubs the name ``_sigv4_headers`` on the module for
# the transport-focused tests; keep the real function for the signing tests.
_REAL_SIGV4_HEADERS = handler._sigv4_headers


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


class TestDiscoveryConfiguration:
    """The bridge list, project name and URL suffix all fail closed."""

    @pytest.mark.parametrize(
        "raw",
        [None, "not json", "{}", '["us-east-1", 7]', '["US-EAST-1"]', '["nowhere"]'],
        ids=["unset", "not-json", "not-a-list", "non-string", "uppercase", "not-a-region"],
    )
    def test_malformed_target_regions_fail_closed(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("TARGET_REGIONS", raising=False)
        else:
            monkeypatch.setenv("TARGET_REGIONS", raw)
        with pytest.raises(RuntimeError, match=r"not configured|invalid region"):
            handler._configured_regions()

    def test_empty_target_regions_is_a_control_plane_only_deployment(self, monkeypatch):
        """Zero workload Regions discovers no bridges and aggregates to empty results."""
        monkeypatch.setenv("TARGET_REGIONS", "[]")
        monkeypatch.setenv("PROJECT_NAME", "gco")
        monkeypatch.setenv("AWS_URL_SUFFIX", "amazonaws.com")
        handler._cached_endpoints = None
        assert handler._configured_regions() == []

        with patch.object(handler.boto3, "client") as client:
            assert handler.get_regional_endpoints() == {}
            result = handler.aggregate_jobs()
        client.assert_not_called()
        assert result["jobs"] == []
        assert result["total"] == 0
        assert result["regions_queried"] == 0
        assert result["errors"] is None
        handler._cached_endpoints = None

    def test_duplicate_regions_are_collapsed_in_order(self):
        with patch.dict(
            "os.environ", {"TARGET_REGIONS": '["us-west-2", "us-east-1", "us-west-2"]'}
        ):
            assert handler._configured_regions() == ["us-west-2", "us-east-1"]

    @pytest.mark.parametrize("suffix", ["", "   ", "not a suffix", "amazonaws"])
    def test_unconfigured_url_suffix_fails_closed(self, suffix):
        with (
            patch.dict("os.environ", {"AWS_URL_SUFFIX": suffix}),
            pytest.raises(RuntimeError, match="AWS URL suffix is not configured"),
        ):
            handler._aws_url_suffix()

    def test_blank_project_name_fails_closed(self):
        with (
            patch.dict("os.environ", {"PROJECT_NAME": "   ", "TARGET_REGIONS": '["us-east-1"]'}),
            patch.object(handler.boto3, "client") as mock_client,
            pytest.raises(RuntimeError, match="not configured"),
        ):
            handler.get_regional_endpoints()
        mock_client.assert_not_called()

    def test_discovery_failure_falls_back_to_bounded_stale_endpoints(self, caplog):
        stale = {"us-east-1": "https://stale1.execute-api.us-east-1.amazonaws.com/prod"}
        handler._cached_endpoints = stale
        # Past the fresh TTL, inside the stale ceiling.
        handler._endpoints_cache_time = time.monotonic() - handler._ENDPOINTS_CACHE_TTL - 1
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.side_effect = RuntimeError("cloudformation down")

        with (
            patch.dict("os.environ", {"PROJECT_NAME": "gco", "TARGET_REGIONS": '["us-east-1"]'}),
            patch.object(handler.boto3, "client", return_value=mock_cfn),
            caplog.at_level(logging.WARNING),
        ):
            assert handler.get_regional_endpoints() == stale

        assert "Using bounded stale regional API discovery after failures in us-east-1" in (
            caplog.text
        )

    def test_stale_endpoints_past_the_ceiling_are_not_used(self):
        handler._cached_endpoints = {
            "us-east-1": "https://stale1.execute-api.us-east-1.amazonaws.com/prod"
        }
        handler._endpoints_cache_time = time.monotonic() - handler._ENDPOINTS_CACHE_MAX_STALE - 1
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.side_effect = RuntimeError("cloudformation down")

        with (
            patch.dict("os.environ", {"PROJECT_NAME": "gco", "TARGET_REGIONS": '["us-east-1"]'}),
            patch.object(handler.boto3, "client", return_value=mock_cfn),
            pytest.raises(RuntimeError, match="regional API bridges are unavailable"),
        ):
            handler.get_regional_endpoints()


class TestSigV4Signing:
    """Real SigV4 signing against the Lambda execution-role credentials."""

    _URL = "https://abc123.execute-api.us-east-1.amazonaws.com/prod/api/v1/jobs?limit=10"

    def _session(self, credentials):
        session = MagicMock()
        session.get_credentials.return_value = credentials
        return session

    def test_signs_with_frozen_credentials(self):
        credentials = Credentials("AKIAEXAMPLE", "secret", token="session-token")  # nosec B106  # fixture value, not a real credential

        with patch.object(handler.boto3, "Session", return_value=self._session(credentials)):
            headers = _REAL_SIGV4_HEADERS("us-east-1", "get", self._URL, '{"a": 1}')

        assert headers["Content-Type"] == "application/json"
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
        assert "/us-east-1/execute-api/aws4_request" in headers["Authorization"]
        assert headers["X-Amz-Security-Token"] == "session-token"  # nosec B105  # fixture value
        assert "X-Amz-Date" in headers

    def test_signs_with_credentials_that_cannot_be_frozen(self):
        class StaticCredentials:
            access_key = "AKIASTATIC"
            secret_key = "secret"  # nosec B105  # fixture value, not a real credential
            token = None

        with patch.object(
            handler.boto3, "Session", return_value=self._session(StaticCredentials())
        ):
            headers = _REAL_SIGV4_HEADERS("eu-west-1", "DELETE", self._URL, None)

        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIASTATIC/")
        assert "/eu-west-1/execute-api/aws4_request" in headers["Authorization"]
        assert "X-Amz-Security-Token" not in headers

    def test_missing_credentials_fail_closed(self):
        with (
            patch.object(handler.boto3, "Session", return_value=self._session(None)),
            pytest.raises(RuntimeError, match="credentials are unavailable"),
        ):
            _REAL_SIGV4_HEADERS("us-east-1", "GET", self._URL, None)


class TestUnexpectedRegionalFailures:
    """A query that raises (rather than returning ``_error``) is isolated per region."""

    _ENDPOINTS = {
        "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
        "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
    }

    def _query(
        self, region, endpoint, path, method="GET", body=None, query_params=None, deadline=None
    ):
        if region == "us-west-2":
            raise RuntimeError("thread blew up")
        if path == "/api/v1/health":
            return {"_status": "success", "status": "healthy"}
        if path == "/api/v1/status":
            return {"_status": "success", "metrics": {"pods": 1}}
        if method == "DELETE":
            return {"_status": "success", "deleted": 2}
        return {"_status": "success", "jobs": [{"id": "j1", "created_at": "2026-01-01"}]}

    def _patched(self):
        return (
            patch.object(handler, "get_regional_endpoints", return_value=self._ENDPOINTS),
            patch.object(handler, "query_region", side_effect=self._query),
        )

    def test_aggregate_jobs_records_the_failure(self):
        endpoints, query = self._patched()
        with endpoints, query:
            result = handler.aggregate_jobs()
        assert result["errors"] == [{"region": "us-west-2", "error": "Regional request failed"}]
        assert [job["_source_region"] for job in result["jobs"]] == ["us-east-1"]

    def test_aggregate_metrics_records_the_failure(self):
        endpoints, query = self._patched()
        with endpoints, query:
            result = handler.aggregate_metrics()
        assert result["errors"] == [{"region": "us-west-2", "error": "Regional request failed"}]
        assert [item["region"] for item in result["regions"]] == ["us-east-1"]

    def test_aggregate_health_marks_the_region_as_errored(self):
        endpoints, query = self._patched()
        with endpoints, query:
            result = handler.aggregate_health()
        by_region = {item["region"]: item for item in result["regions"]}
        assert by_region["us-west-2"]["status"] == "error"
        assert by_region["us-east-1"]["status"] == "healthy"

    def test_bulk_delete_records_the_failure(self):
        endpoints, query = self._patched()
        with endpoints, query:
            result = handler.bulk_delete_jobs(dry_run=False)
        assert result["errors"] == [{"region": "us-west-2", "error": "Regional request failed"}]


class TestHandlerDiscoveryOutage:
    def test_bridge_discovery_failure_is_a_503(self):
        with patch.object(
            handler,
            "get_regional_endpoints",
            side_effect=RuntimeError("One or more regional API bridges are unavailable"),
        ):
            result = handler.lambda_handler(
                {"httpMethod": "GET", "path": "/api/v1/global/health"}, None
            )

        assert result["statusCode"] == 503
        assert json.loads(result["body"]) == {
            "error": "Regional aggregation is temporarily unavailable"
        }


class _SilentServer:
    """A local TCP server that accepts connections and never answers."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.accepted: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.sock.accept()
            except TimeoutError:
                continue
            self.accepted.append(connection)

    def __enter__(self) -> _SilentServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()
        for connection in self.accepted:
            connection.close()
        self.sock.close()


class TestRegionalRequestBudget:
    """Each regional request is sent once and ends before the invocation does (L5).

    urllib3's default Retry(total=3) re-sent a timed-out GET or DELETE three
    more times with a fresh 10-second timeout each, so a stalled Region took
    about 40 seconds, the 29-second function timed out, and API Gateway failed
    the whole aggregate instead of reporting one Region as an error. The bulk
    DELETE was re-sent while the first was still running.
    """

    _ENDPOINTS = {
        "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod",
        "us-west-2": "https://west123.execute-api.us-west-2.amazonaws.com/prod",
    }

    @staticmethod
    def _context(remaining_ms: int) -> MagicMock:
        context = MagicMock()
        context.get_remaining_time_in_millis.return_value = remaining_ms
        return context

    def test_the_pool_sends_a_timed_out_request_once(self):
        with _SilentServer() as server:
            with pytest.raises(urllib3.exceptions.ReadTimeoutError):
                handler.http.request(
                    "DELETE",
                    f"http://127.0.0.1:{server.port}/api/v1/jobs",
                    timeout=urllib3.Timeout(total=0.3),
                )
            time.sleep(0.2)
            assert len(server.accepted) == 1

    def test_regional_requests_end_before_the_invocation_does(self):
        handler._cached_endpoints = dict(self._ENDPOINTS)
        mock_http = MagicMock()
        mock_http.request.return_value = MagicMock(
            status=200, data=json.dumps({"jobs": [], "count": 0, "total": 0}).encode()
        )
        event = {"httpMethod": "GET", "path": "/api/v1/global/jobs"}

        with patch.object(handler, "http", mock_http):
            result = handler.lambda_handler(event, self._context(5_000))

        assert result["statusCode"] == 200
        budgets = [call.kwargs["timeout"].total for call in mock_http.request.call_args_list]
        assert len(budgets) == 2
        # 5 s remaining less the 2 s kept to assemble the response.
        assert all(2.5 < budget <= 3.0 for budget in budgets)

    def test_without_a_context_the_default_budget_applies(self):
        handler._cached_endpoints = {"us-east-1": self._ENDPOINTS["us-east-1"]}
        mock_http = MagicMock()
        mock_http.request.return_value = MagicMock(status=200, data=b'{"status":"healthy"}')

        with patch.object(handler, "http", mock_http):
            handler.lambda_handler({"path": "/api/v1/global/health"}, None)

        budget = mock_http.request.call_args.kwargs["timeout"].total
        assert (
            handler._DEFAULT_REQUEST_BUDGET_SECONDS - 1
            < budget
            <= handler._DEFAULT_REQUEST_BUDGET_SECONDS
        )

    def test_a_spent_budget_reports_every_region_without_sending(self):
        handler._cached_endpoints = dict(self._ENDPOINTS)
        mock_http = MagicMock()
        event = {"httpMethod": "GET", "path": "/api/v1/global/status"}

        with patch.object(handler, "http", mock_http):
            result = handler.lambda_handler(event, self._context(1_500))

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["regions_successful"] == 0
        assert body["errors"] == [
            {"region": region, "error": "No time left to query the region"}
            for region in sorted(self._ENDPOINTS)
        ]
        mock_http.request.assert_not_called()

    def test_a_stalled_bulk_delete_is_one_error_per_region_not_a_failed_request(self):
        handler._cached_endpoints = dict(self._ENDPOINTS)
        mock_http = MagicMock()
        mock_http.request.side_effect = urllib3.exceptions.ReadTimeoutError(
            None, "https://east123.execute-api.us-east-1.amazonaws.com", "Read timed out."
        )
        event = {
            "httpMethod": "DELETE",
            "path": "/api/v1/global/jobs",
            "body": json.dumps({"status": "failed", "dry_run": False}),
        }

        with patch.object(handler, "http", mock_http):
            result = handler.lambda_handler(event, self._context(29_000))

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert [error["region"] for error in body["errors"]] == sorted(self._ENDPOINTS)
        assert mock_http.request.call_count == len(self._ENDPOINTS)
        assert {call.args[0] for call in mock_http.request.call_args_list} == {"DELETE"}


class TestClientInputValidation:
    """Malformed client input is a 400, not a 500 (L15)."""

    @staticmethod
    def _jobs(limit):
        return {
            "httpMethod": "GET",
            "path": "/api/v1/global/jobs",
            "queryStringParameters": {"limit": limit},
        }

    @pytest.mark.parametrize(
        ("limit", "error"),
        [
            ("ten", "limit must be an integer"),
            ("2.5", "limit must be an integer"),
            ("", "limit must be an integer"),
            ("0", "limit must be between 1 and 1000"),
            ("-5", "limit must be between 1 and 1000"),
            ("1001", "limit must be between 1 and 1000"),
        ],
    )
    def test_invalid_limit_is_a_400_and_queries_nothing(self, limit, error):
        with patch.object(handler, "aggregate_jobs") as aggregate:
            result = handler.lambda_handler(self._jobs(limit), None)

        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"error": error}
        aggregate.assert_not_called()

    def test_the_per_region_limit_never_exceeds_the_regional_ceiling(self):
        """Doubling a limit above 500 used to send a value every Region refused."""
        handler._cached_endpoints = {
            "us-east-1": "https://east123.execute-api.us-east-1.amazonaws.com/prod"
        }
        mock_http = MagicMock()
        mock_http.request.return_value = MagicMock(
            status=200, data=json.dumps({"jobs": [], "count": 0, "total": 0}).encode()
        )

        with patch.object(handler, "http", mock_http):
            result = handler.lambda_handler(self._jobs("1000"), None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["limit"] == 1000
        assert "limit=1000" in mock_http.request.call_args.args[1]

    @pytest.mark.parametrize("body", ["[]", '"jobs"', "5", "null"])
    def test_non_object_delete_body_is_a_400(self, body):
        event = {"httpMethod": "DELETE", "path": "/api/v1/global/jobs", "body": body}

        with patch.object(handler, "bulk_delete_jobs") as bulk_delete:
            result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 400
        assert json.loads(result["body"]) == {"error": "Request body must be a JSON object"}
        bulk_delete.assert_not_called()
