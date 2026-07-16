"""
Tests for the Lambda proxy handlers and shared proxy_utils.

Exercises the cached secret fetch (within-TTL reuse, post-TTL refresh,
stale-cache fallback when Secrets Manager throws, first-call failure
surfacing as RuntimeError), plus the URL-building and urllib3-based
HTTPS forwarding with retries used by both lambda/api-gateway-proxy
and lambda/regional-api-proxy. Covers the header-stripping logic that
prevents client-supplied auth headers from leaking into the internal
ALB request.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import urllib3

from tests._lambda_imports import load_lambda_module

# ============================================================================
# proxy_utils
# ============================================================================


@pytest.fixture
def proxy_utils_module():
    """Import proxy_utils with mocked boto3 and urllib3.PoolManager.

    Loaded via :func:`load_lambda_module` — see
    ``tests/_lambda_imports.py`` for the rationale.
    """
    with (
        patch("boto3.client") as mock_boto,
        patch("urllib3.PoolManager") as mock_pool_cls,
        patch.dict(
            "os.environ",
            {
                "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                "SECRET_CACHE_TTL_SECONDS": "300",
                "PROXY_MAX_RETRIES": "3",
                "PROXY_RETRY_BACKOFF_BASE": "0",
            },
        ),
    ):
        proxy_utils = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])

        proxy_utils._cached_secret = None
        proxy_utils._cache_timestamp = 0.0
        proxy_utils._last_successful_refresh = 0.0
        proxy_utils._last_refresh_attempt = 0.0

        mock_sm = mock_boto.return_value
        mock_pool = mock_pool_cls.return_value
        proxy_utils._http = mock_pool
        yield proxy_utils, mock_sm, mock_pool


class TestGetSecretToken:
    def test_returns_cached_token_within_ttl(self, proxy_utils_module):
        proxy_utils, mock_sm, _ = proxy_utils_module
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"token": "my-secret"})}

        # First call populates cache
        assert proxy_utils.get_secret_token() == "my-secret"
        # Second call should use cache — blow up SM to prove it
        mock_sm.get_secret_value.side_effect = Exception("should not be called")
        assert proxy_utils.get_secret_token() == "my-secret"
        # SM was only called once (the first time)
        assert mock_sm.get_secret_value.call_count == 1

    def test_refreshes_after_ttl_expires(self, proxy_utils_module):
        proxy_utils, mock_sm, _ = proxy_utils_module
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"token": "old-token"})}
        assert proxy_utils.get_secret_token() == "old-token"

        # Expire the cache
        now = time.monotonic()
        proxy_utils._cache_timestamp = now - 400
        proxy_utils._last_successful_refresh = now - 400
        proxy_utils._last_refresh_attempt = 0.0

        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"token": "new-token"})}
        assert proxy_utils.get_secret_token() == "new-token"
        assert mock_sm.get_secret_value.call_count == 2

    def test_stale_cache_fallback_on_sm_failure(self, proxy_utils_module):
        proxy_utils, mock_sm, _ = proxy_utils_module
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"token": "cached-token"})
        }
        assert proxy_utils.get_secret_token() == "cached-token"

        # Expire cache, then make SM fail
        now = time.monotonic()
        proxy_utils._cache_timestamp = now - 400
        proxy_utils._last_successful_refresh = now - 400
        proxy_utils._last_refresh_attempt = 0.0
        mock_sm.get_secret_value.side_effect = Exception("SM unavailable")

        # Should return stale cached value instead of raising
        assert proxy_utils.get_secret_token() == "cached-token"

    def test_raises_runtime_error_on_first_call_if_sm_fails(self, proxy_utils_module):
        proxy_utils, mock_sm, _ = proxy_utils_module
        mock_sm.get_secret_value.side_effect = Exception("SM unavailable")

        with pytest.raises(RuntimeError, match="Authentication signing key is unavailable"):
            proxy_utils.get_secret_token()


class TestBuildTargetUrl:
    def test_builds_url_with_path_and_query_params(self, proxy_utils_module):
        proxy_utils, _, _ = proxy_utils_module
        url = proxy_utils.build_target_url(
            "my-alb.example.com", "/api/v1/jobs", {"status": "running", "limit": "10"}
        )
        assert url.startswith("https://my-alb.example.com/api/v1/jobs?")
        assert "status=running" in url
        assert "limit=10" in url

    def test_builds_url_without_query_params(self, proxy_utils_module):
        proxy_utils, _, _ = proxy_utils_module
        url = proxy_utils.build_target_url("my-alb.example.com", "/health", None)
        assert url == "https://my-alb.example.com/health"

        url_empty = proxy_utils.build_target_url("my-alb.example.com", "/health", {})
        assert url_empty == "https://my-alb.example.com/health"

    def test_encodes_path_and_repeated_query_values(self, proxy_utils_module):
        proxy_utils, _, _ = proxy_utils_module

        url = proxy_utils.build_target_url(
            "https://my-alb.example.com/base/",
            "/team alpha/report?#/%2F",
            {"tag": ["first value", "second&value"]},
        )

        assert url == (
            "https://my-alb.example.com/base/team%20alpha/report%3F%23/%2F"
            "?tag=first+value&tag=second%26value"
        )

    def test_preserves_only_valid_percent_escapes(self, proxy_utils_module):
        proxy_utils, _, _ = proxy_utils_module

        url = proxy_utils.build_target_url(
            "my-alb.example.com",
            "/valid/%2F/literal%/malformed%2G",
            None,
        )

        assert url == "https://my-alb.example.com/valid/%2F/literal%25/malformed%252G"


class TestForwardRequest:
    def test_returns_success_response_on_200(self, proxy_utils_module):
        proxy_utils, _, mock_pool = proxy_utils_module
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.data = b'{"ok": true}'
        mock_pool.request.return_value = mock_response

        result = proxy_utils.forward_request(
            "https://example.com/api", "GET", {"Accept": "application/json"}, ""
        )
        assert result["statusCode"] == 200
        assert result["body"] == '{"ok": true}'
        mock_pool.request.assert_called_once()

    def test_retries_on_503_and_succeeds(self, proxy_utils_module):
        proxy_utils, _, mock_pool = proxy_utils_module

        fail_response = MagicMock()
        fail_response.status = 503
        fail_response.headers = {}
        fail_response.data = b"Service Unavailable"

        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.headers = {"Content-Type": "text/plain"}
        ok_response.data = b"OK"

        mock_pool.request.side_effect = [fail_response, ok_response]

        result = proxy_utils.forward_request("https://example.com/api", "GET", {}, "")
        assert result["statusCode"] == 200
        assert result["body"] == "OK"
        assert mock_pool.request.call_count == 2

    def test_returns_503_on_connection_failure_after_retries(self, proxy_utils_module):
        proxy_utils, _, mock_pool = proxy_utils_module
        mock_pool.request.side_effect = urllib3.exceptions.MaxRetryError(
            pool=None, url="https://example.com", reason="Connection refused"
        )

        result = proxy_utils.forward_request("https://example.com/api", "POST", {}, '{"data": 1}')
        assert result["statusCode"] == 503
        body = json.loads(result["body"])
        assert body["message"] == "Upstream failed after 1 attempt(s)"
        assert mock_pool.request.call_count == 1


# ============================================================================
# api-gateway-proxy handler
# ============================================================================


@pytest.fixture
def api_gw_proxy_module():
    """Import api-gateway-proxy handler with mocked dependencies.

    The handler does ``from proxy_utils import ...`` at module load
    time, so we pass ``shared_dirs=["proxy-shared"]`` to make it
    resolvable during the load. Both the handler and proxy_utils get
    loaded under unique names, so there's no ``sys.modules['handler']``
    pollution between tests.
    """
    with (
        patch("boto3.client") as mock_boto,
        patch("urllib3.PoolManager") as mock_pool_cls,
        patch.dict(
            "os.environ",
            {
                "GLOBAL_ACCELERATOR_ENDPOINT": "ga-abc123.awsglobalaccelerator.com",
                "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                "SECRET_CACHE_TTL_SECONDS": "300",
                "PROXY_MAX_RETRIES": "3",
                "PROXY_RETRY_BACKOFF_BASE": "0",
            },
        ),
    ):
        handler = load_lambda_module("api-gateway-proxy", shared_dirs=["proxy-shared"])
        proxy_state = handler.get_secret_token.__globals__
        proxy_state["_cached_secret"] = None
        proxy_state["_cache_timestamp"] = 0.0
        proxy_state["_last_successful_refresh"] = 0.0
        proxy_state["_last_refresh_attempt"] = 0.0

        mock_sm = mock_boto.return_value
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"token": "gco-secret-token"})
        }
        mock_pool = mock_pool_cls.return_value
        proxy_state["_http"] = mock_pool
        yield handler, mock_sm, mock_pool


class TestApiGatewayProxyHandler:
    def _make_event(self, method="GET", path="/api/v1/health", qs=None, headers=None, body=""):
        return {
            "httpMethod": method,
            "path": path,
            "queryStringParameters": qs,
            "headers": headers or {},
            "body": body,
        }

    def test_adds_auth_token_and_forwards_to_ga(self, api_gw_proxy_module):
        handler, _, mock_pool = api_gw_proxy_module
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.data = b'{"status": "healthy"}'
        mock_pool.request.return_value = mock_response

        result = handler.lambda_handler(self._make_event(), None)

        assert result["statusCode"] == 200
        # Verify the request was made to Global Accelerator with auth header
        call_args = mock_pool.request.call_args
        assert call_args[0][1].startswith("https://")
        assert "ga-abc123.awsglobalaccelerator.com" in call_args[0][1]
        forwarded_headers = (
            call_args[1]["headers"] if "headers" in call_args[1] else call_args[0][2]
        )
        assert "x-gco-auth-token" not in forwarded_headers
        assert forwarded_headers["x-gco-signature-version"] == "v1"
        assert len(forwarded_headers["x-gco-signature"]) == 64
        assert len(forwarded_headers["x-gco-nonce"]) == 32
        assert len(forwarded_headers["x-gco-content-sha256"]) == 64

    def test_caps_forwarding_budget_below_api_gateway_timeout(self, api_gw_proxy_module):
        handler, _, _ = api_gw_proxy_module
        context = MagicMock()
        context.get_remaining_time_in_millis.return_value = 60_000
        response = {"statusCode": 200, "headers": {}, "body": "OK"}

        with patch.object(handler, "forward_request", return_value=response) as mock_forward:
            assert handler.lambda_handler(self._make_event(), context) == response

        assert mock_forward.call_args.kwargs["timeout"] == pytest.approx(28.0)

    def test_passes_query_string_parameters(self, api_gw_proxy_module):
        handler, _, mock_pool = api_gw_proxy_module
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.data = b"OK"
        mock_pool.request.return_value = mock_response

        event = self._make_event(path="/api/v1/jobs", qs={"status": "running", "limit": "5"})
        handler.lambda_handler(event, None)

        call_args = mock_pool.request.call_args
        target_url = call_args[0][1]
        assert "status=running" in target_url
        assert "limit=5" in target_url

    def test_preserves_multi_value_query_parameters(self, api_gw_proxy_module):
        handler, _, mock_pool = api_gw_proxy_module
        mock_response = MagicMock(status=200, headers={}, data=b"OK")
        mock_pool.request.return_value = mock_response
        event = self._make_event(path="/api/v1/jobs", qs={"tag": "last"})
        event["multiValueQueryStringParameters"] = {"tag": ["first value", "second&value"]}

        handler.lambda_handler(event, None)

        target_url = mock_pool.request.call_args[0][1]
        assert target_url.endswith("?tag=first+value&tag=second%26value")
        assert "tag=last" not in target_url

    def test_target_region_header_fails_closed(self, api_gw_proxy_module):
        handler, _, mock_pool = api_gw_proxy_module

        result = handler.lambda_handler(
            self._make_event(headers={"X-GCO-Target-Region": "us-east-1"}),
            None,
        )

        assert result["statusCode"] == 400
        assert "regional API endpoint" in result["body"]
        mock_pool.request.assert_not_called()


# ============================================================================
# regional-api-proxy handler
# ============================================================================


@pytest.fixture
def regional_proxy_module():
    """Import regional-api-proxy handler with mocked dependencies.

    Same load pattern as ``api_gw_proxy_module`` above. See
    ``tests/_lambda_imports.py`` for the full rationale.
    """
    with (
        patch("boto3.client") as mock_boto,
        patch("urllib3.PoolManager") as mock_pool_cls,
        patch.dict(
            "os.environ",
            {
                "ALB_ENDPOINT": "internal-alb.us-east-1.elb.amazonaws.com",
                "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                "SECRET_CACHE_TTL_SECONDS": "300",
                "PROXY_MAX_RETRIES": "3",
                "PROXY_RETRY_BACKOFF_BASE": "0",
            },
        ),
    ):
        handler = load_lambda_module("regional-api-proxy", shared_dirs=["proxy-shared"])
        proxy_state = handler.get_secret_token.__globals__
        proxy_state["_cached_secret"] = None
        proxy_state["_cache_timestamp"] = 0.0
        proxy_state["_last_successful_refresh"] = 0.0
        proxy_state["_last_refresh_attempt"] = 0.0

        mock_sm = mock_boto.return_value
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"token": "regional-secret"})
        }
        mock_pool = mock_pool_cls.return_value
        proxy_state["_http"] = mock_pool
        yield handler, mock_sm, mock_pool


class TestRegionalApiProxyHandler:
    def _make_event(self, method="GET", path="/api/v1/health", qs=None, headers=None, body=""):
        return {
            "httpMethod": method,
            "path": path,
            "queryStringParameters": qs,
            "headers": headers or {},
            "body": body,
        }

    def test_adds_auth_token_and_forwards_to_alb(self, regional_proxy_module):
        handler, _, mock_pool = regional_proxy_module
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.data = b'{"status": "ok"}'
        mock_pool.request.return_value = mock_response

        result = handler.lambda_handler(self._make_event(), None)

        assert result["statusCode"] == 200
        call_args = mock_pool.request.call_args
        assert call_args[0][1].startswith("https://")
        assert "internal-alb.us-east-1.elb.amazonaws.com" in call_args[0][1]
        forwarded_headers = (
            call_args[1]["headers"] if "headers" in call_args[1] else call_args[0][2]
        )
        assert "x-gco-auth-token" not in forwarded_headers
        assert forwarded_headers["x-gco-signature-version"] == "v1"
        assert len(forwarded_headers["x-gco-signature"]) == 64
        assert len(forwarded_headers["x-gco-nonce"]) == 32

    def test_reserves_lambda_response_headroom(self, regional_proxy_module):
        handler, _, _ = regional_proxy_module
        context = MagicMock()
        context.get_remaining_time_in_millis.return_value = 5_000
        response = {"statusCode": 200, "headers": {}, "body": "OK"}

        with patch.object(handler, "forward_request", return_value=response) as mock_forward:
            assert handler.lambda_handler(self._make_event(), context) == response

        assert mock_forward.call_args.kwargs["timeout"] == pytest.approx(4.0)

    def test_strips_host_and_forwarded_headers(self, regional_proxy_module):
        handler, _, mock_pool = regional_proxy_module
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.data = b"OK"
        mock_pool.request.return_value = mock_response

        event = self._make_event(
            headers={
                "Host": "api.example.com",
                "host": "api.example.com",
                "X-Forwarded-For": "1.2.3.4",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Port": "443",
                "Accept": "application/json",
            }
        )
        handler.lambda_handler(event, None)

        call_args = mock_pool.request.call_args
        forwarded_headers = (
            call_args[1]["headers"] if "headers" in call_args[1] else call_args[0][2]
        )
        assert "Host" not in forwarded_headers
        assert "host" not in forwarded_headers
        assert "X-Forwarded-For" not in forwarded_headers
        assert "X-Forwarded-Proto" not in forwarded_headers
        assert "X-Forwarded-Port" not in forwarded_headers
        # Allowlisted headers survive in normalized form.
        assert forwarded_headers["accept"] == "application/json"
        assert "x-gco-auth-token" not in forwarded_headers
        assert forwarded_headers["x-gco-signature-version"] == "v1"
        assert len(forwarded_headers["x-gco-signature"]) == 64
        assert len(forwarded_headers["x-gco-nonce"]) == 32
