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
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
import urllib3
from botocore.exceptions import ClientError

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

    def test_trust_bundle_refresh_consumes_the_forward_budget(self, proxy_utils_module):
        """Regression: a cold-start trust refresh must not extend the deadline.

        The caller derives ``timeout`` from the Lambda's remaining time.
        Anchoring the deadline after ``get_backend_http_pool()`` let a slow
        SSM trust-bundle fetch push the total wall clock past the Lambda
        timeout, so a black-holed backend killed the function at 29s instead
        of returning its bounded 504.
        """
        proxy_utils, _, mock_pool = proxy_utils_module
        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.headers = {"Content-Type": "text/plain"}
        ok_response.data = b"OK"
        mock_pool.request.return_value = ok_response

        clock = {"now": 100.0}

        def slow_pool_fetch():
            clock["now"] += 3.0
            return mock_pool

        proxy_utils._http = None
        with (
            patch.object(proxy_utils, "get_backend_http_pool", side_effect=slow_pool_fetch),
            patch.object(proxy_utils.time, "monotonic", side_effect=lambda: clock["now"]),
        ):
            result = proxy_utils.forward_request(
                "https://example.com/api", "GET", {}, "", timeout=10.0
            )

        assert result["statusCode"] == 200
        request_timeout = mock_pool.request.call_args.kwargs["timeout"]
        assert request_timeout.total == pytest.approx(7.0)


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

    def test_base64_bodies_are_rejected(self, api_gw_proxy_module):
        handler, _, mock_pool = api_gw_proxy_module
        # An unrelated header exercises the case-insensitive scan past a
        # non-matching key before the region header is ruled absent.
        event = self._make_event(method="POST", headers={"Accept": "*/*"}, body="AAAA")
        event["isBase64Encoded"] = True

        result = handler.lambda_handler(event, None)

        assert result["statusCode"] == 415
        assert json.loads(result["body"]) == {
            "error": "Base64-encoded request bodies are not supported"
        }
        mock_pool.request.assert_not_called()

    @pytest.mark.parametrize("failure", [KeyError("SECRET_ARN"), RuntimeError("unavailable")])
    def test_signing_key_failure_is_a_503(self, api_gw_proxy_module, failure):
        handler, _, mock_pool = api_gw_proxy_module

        with patch.object(handler, "get_secret_token", side_effect=failure):
            result = handler.lambda_handler(self._make_event(), None)

        assert result["statusCode"] == 503
        assert "authentication is temporarily unavailable" in result["body"]
        mock_pool.request.assert_not_called()

    def test_missing_backend_endpoint_is_a_503(self, api_gw_proxy_module, monkeypatch):
        handler, _, mock_pool = api_gw_proxy_module
        monkeypatch.delenv("GLOBAL_ACCELERATOR_ENDPOINT")

        result = handler.lambda_handler(self._make_event(), None)

        assert result["statusCode"] == 503
        assert "routing is temporarily unavailable" in result["body"]
        mock_pool.request.assert_not_called()

    def test_unroutable_path_is_a_503(self, api_gw_proxy_module):
        handler, _, mock_pool = api_gw_proxy_module

        with patch.object(handler, "build_target_url", side_effect=ValueError("bad path")):
            result = handler.lambda_handler(self._make_event(path="/../etc"), None)

        assert result["statusCode"] == 503
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
                "AWS_URL_SUFFIX": "amazonaws.com",
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


# ============================================================================
# regional-api-proxy: fail-closed responses and the registry-driven resolver
# ============================================================================

_REGISTRY_ENV = {
    "TARGET_REGION": "us-east-1",
    "REGISTRY_REGION": "us-west-2",
    "PROJECT_NAME": "gco",
    "AWS_ACCOUNT_ID": "123456789012",
    "AWS_URL_SUFFIX": "amazonaws.com",
}
_GATEWAY_DNS = "internal-k8s-gcosyste-gcogatew-abc123.us-east-1.elb.amazonaws.com"
_GATEWAY_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
    "loadbalancer/app/k8s-gcosyste-gcogatew-abc123/0123456789abcdef"
)


def _gateway_load_balancer(**overrides):
    load_balancer = {
        "DNSName": _GATEWAY_DNS,
        "LoadBalancerArn": _GATEWAY_ARN,
        "Type": "application",
        "Scheme": "internal",
    }
    load_balancer.update(overrides)
    return load_balancer


def _gateway_tags(**overrides):
    tags = {"elbv2.k8s.aws/cluster": "gco-us-east-1", "gco.aws/gateway": "gco-system/gco-gateway"}
    tags.update(overrides)
    return {
        "TagDescriptions": [
            {
                "ResourceArn": _GATEWAY_ARN,
                "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
            }
        ]
    }


def _elbv2_stub(load_balancers=None, tags=None):
    elbv2 = MagicMock()
    elbv2.describe_load_balancers.return_value = {
        "LoadBalancers": [_gateway_load_balancer()] if load_balancers is None else load_balancers
    }
    elbv2.describe_tags.return_value = _gateway_tags() if tags is None else tags
    return elbv2


def _ssm_stub(value=_GATEWAY_DNS):
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
    return ssm


@pytest.fixture
def registry_proxy(regional_proxy_module, monkeypatch):
    """regional-api-proxy in registry mode with per-service AWS stubs.

    Clears ``ALB_ENDPOINT`` (the literal-endpoint compatibility path) so
    resolution goes through the SSM registry and the ELBv2 ownership check.
    Returns ``(handler, clients)`` where ``clients`` holds the ``ssm`` and
    ``elbv2`` stubs that ``boto3.client`` hands back.
    """
    handler, mock_sm, _ = regional_proxy_module
    monkeypatch.delenv("ALB_ENDPOINT")
    for name, value in _REGISTRY_ENV.items():
        monkeypatch.setenv(name, value)
    clients = {"ssm": _ssm_stub(), "elbv2": _elbv2_stub(), "secretsmanager": mock_sm}

    def route(service, **kwargs):
        if service == "ssm":
            assert kwargs == {"region_name": "us-west-2"}
        elif service == "elbv2":
            assert kwargs == {"region_name": "us-east-1"}
        return clients[service]

    handler.boto3.client.side_effect = route
    return handler, clients


class TestRegionalApiProxyFailsClosed:
    def _make_event(self, **overrides):
        event = {
            "httpMethod": "GET",
            "path": "/api/v1/health",
            "queryStringParameters": None,
            "headers": {},
            "body": "",
        }
        event.update(overrides)
        return event

    @pytest.mark.parametrize("failure", [KeyError("SECRET_ARN"), RuntimeError("unavailable")])
    def test_signing_key_failure_is_a_503(self, regional_proxy_module, failure):
        handler, _, mock_pool = regional_proxy_module

        with patch.object(handler, "get_secret_token", side_effect=failure):
            result = handler.lambda_handler(self._make_event(), None)

        assert result["statusCode"] == 503
        assert json.loads(result["body"]) == {
            "error": "Backend authentication is temporarily unavailable"
        }
        mock_pool.request.assert_not_called()

    def test_unresolvable_backend_is_a_502_with_the_reason_logged(
        self, regional_proxy_module, monkeypatch, caplog
    ):
        handler, _, mock_pool = regional_proxy_module
        monkeypatch.setenv("ALB_ENDPOINT", "evil.example.com")

        with caplog.at_level(logging.WARNING):
            result = handler.lambda_handler(self._make_event(), None)

        assert result["statusCode"] == 502
        assert json.loads(result["body"]) == {
            "error": "Regional backend is temporarily unavailable"
        }
        assert "Regional backend resolution failed" in caplog.text
        assert "is invalid" in caplog.text
        mock_pool.request.assert_not_called()

    def test_base64_bodies_are_rejected(self, regional_proxy_module):
        handler, _, mock_pool = regional_proxy_module

        result = handler.lambda_handler(
            self._make_event(httpMethod="POST", body="AAAA", isBase64Encoded=True), None
        )

        assert result["statusCode"] == 415
        mock_pool.request.assert_not_called()


class TestRegionalEndpointConfiguration:
    def test_cache_ttl_is_bounded_with_a_sixty_second_default(
        self, regional_proxy_module, monkeypatch
    ):
        handler, _, _ = regional_proxy_module
        monkeypatch.delenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", raising=False)
        assert handler._regional_endpoint_cache_ttl() == 60.0
        monkeypatch.setenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", "soon")
        assert handler._regional_endpoint_cache_ttl() == 60.0
        monkeypatch.setenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", "301")
        assert handler._regional_endpoint_cache_ttl() == 60.0
        monkeypatch.setenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", "0")
        assert handler._regional_endpoint_cache_ttl() == 0.0
        monkeypatch.setenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", "30")
        assert handler._regional_endpoint_cache_ttl() == 30.0

    @pytest.mark.parametrize("suffix", ["", "   ", "not a dns name", "amazonaws"])
    def test_unconfigured_url_suffix_is_refused(self, regional_proxy_module, monkeypatch, suffix):
        handler, _, _ = regional_proxy_module
        monkeypatch.setenv("AWS_URL_SUFFIX", suffix)
        with pytest.raises(RuntimeError, match="AWS URL suffix is not configured"):
            handler._aws_url_suffix()

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "not a hostname",
            "evil.example.com",
            "internal-alb.us-east-1.elb.amazonaws.com.evil.example.com",
        ],
    )
    def test_non_elb_hostnames_are_refused(self, regional_proxy_module, value):
        handler, _, _ = regional_proxy_module
        with pytest.raises(RuntimeError, match="registered backend for us-east-1 is invalid"):
            handler._validated_dns_name(value, region="us-east-1")

    def test_elb_hostname_is_normalised(self, regional_proxy_module):
        handler, _, _ = regional_proxy_module
        assert handler._validated_dns_name(f" {_GATEWAY_DNS}. ", region="us-east-1") == _GATEWAY_DNS

    @pytest.mark.parametrize(
        "overrides",
        [
            {"REGISTRY_REGION": ""},
            {"REGISTRY_REGION": "US-WEST-2"},
            {"TARGET_REGION": "nowhere"},
            {"PROJECT_NAME": ""},
            {"AWS_ACCOUNT_ID": ""},
        ],
    )
    def test_incomplete_registry_configuration_is_refused(
        self, registry_proxy, monkeypatch, overrides
    ):
        handler, clients = registry_proxy
        for name, value in overrides.items():
            monkeypatch.setenv(name, value)

        with pytest.raises(RuntimeError, match="registry is not configured"):
            handler._resolve_registered_endpoint()

        clients["ssm"].get_parameter.assert_not_called()


class TestRegionalEndpointResolution:
    def test_resolves_verifies_and_caches_the_registered_gateway(self, registry_proxy):
        handler, clients = registry_proxy

        assert handler._resolve_registered_endpoint() == _GATEWAY_DNS
        assert handler._resolve_registered_endpoint() == _GATEWAY_DNS

        clients["ssm"].get_parameter.assert_called_once_with(Name="/gco/alb-hostname-us-east-1")
        clients["elbv2"].describe_tags.assert_called_once_with(ResourceArns=[_GATEWAY_ARN])
        assert clients["elbv2"].describe_load_balancers.call_count == 1

    def test_zero_ttl_disables_the_cache(self, registry_proxy, monkeypatch):
        handler, clients = registry_proxy
        monkeypatch.setenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", "0")

        handler._resolve_registered_endpoint()
        handler._resolve_registered_endpoint()

        assert clients["ssm"].get_parameter.call_count == 2

    def test_expired_cache_entry_is_re_verified(self, registry_proxy):
        handler, clients = registry_proxy
        handler._resolve_registered_endpoint()
        key = ("us-west-2", "us-east-1", "gco", "123456789012")
        stamp, endpoint = handler._REGIONAL_ENDPOINT_CACHE[key]
        handler._REGIONAL_ENDPOINT_CACHE[key] = (stamp - 3600, endpoint)

        handler._resolve_registered_endpoint()

        assert clients["ssm"].get_parameter.call_count == 2

    def test_missing_registry_parameter_fails_closed(self, registry_proxy):
        handler, clients = registry_proxy
        clients["ssm"].get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "no"}}, "GetParameter"
        )

        with pytest.raises(RuntimeError, match="could not be verified"):
            handler._resolve_registered_endpoint()

        clients["elbv2"].describe_load_balancers.assert_not_called()

    def test_ownership_check_follows_pagination(self, registry_proxy):
        handler, clients = registry_proxy
        other = _gateway_load_balancer(
            DNSName="internal-other.us-east-1.elb.amazonaws.com",
            LoadBalancerArn=_GATEWAY_ARN.replace("gcogatew", "other"),
        )
        clients["elbv2"].describe_load_balancers.side_effect = [
            {"LoadBalancers": [other], "NextMarker": "page2"},
            {"LoadBalancers": [_gateway_load_balancer()]},
        ]

        assert handler._resolve_registered_endpoint() == _GATEWAY_DNS
        calls = clients["elbv2"].describe_load_balancers.call_args_list
        assert calls[0].kwargs == {}
        assert calls[1].kwargs == {"Marker": "page2"}

    def test_unknown_load_balancer_is_refused(self, registry_proxy):
        handler, clients = registry_proxy
        clients["elbv2"].describe_load_balancers.return_value = {"LoadBalancers": []}

        with pytest.raises(RuntimeError, match="does not exist"):
            handler._resolve_registered_endpoint()

    def test_ownership_scan_is_bounded_to_twenty_pages(self, registry_proxy):
        # An account with an endless supply of unrelated load balancers must
        # not turn one proxied request into an unbounded ELBv2 scan.
        handler, clients = registry_proxy
        other = _gateway_load_balancer(DNSName="internal-other.us-east-1.elb.amazonaws.com")
        clients["elbv2"].describe_load_balancers.return_value = {
            "LoadBalancers": [other],
            "NextMarker": "more",
        }

        with pytest.raises(RuntimeError, match="does not exist"):
            handler._resolve_registered_endpoint()

        assert clients["elbv2"].describe_load_balancers.call_count == 20

    @pytest.mark.parametrize(
        "overrides",
        [{"Type": "network"}, {"Scheme": "internet-facing"}],
        ids=["nlb", "public"],
    )
    def test_only_internal_albs_are_accepted(self, registry_proxy, overrides):
        handler, clients = registry_proxy
        clients["elbv2"].describe_load_balancers.return_value = {
            "LoadBalancers": [_gateway_load_balancer(**overrides)]
        }

        with pytest.raises(RuntimeError, match="not an internal ALB"):
            handler._resolve_registered_endpoint()

    @pytest.mark.parametrize(
        "arn",
        [
            "not-an-arn",
            _GATEWAY_ARN.replace(":123456789012:", ":000000000000:"),
            _GATEWAY_ARN.replace(":us-east-1:", ":eu-west-1:"),
            _GATEWAY_ARN.replace(":elasticloadbalancing:", ":ec2:"),
        ],
        ids=["malformed", "foreign-account", "foreign-region", "wrong-service"],
    )
    def test_foreign_ownership_is_refused(self, registry_proxy, arn):
        handler, clients = registry_proxy
        clients["elbv2"].describe_load_balancers.return_value = {
            "LoadBalancers": [_gateway_load_balancer(LoadBalancerArn=arn)]
        }

        with pytest.raises(RuntimeError, match="invalid ownership"):
            handler._resolve_registered_endpoint()

        clients["elbv2"].describe_tags.assert_not_called()

    def test_eks_cluster_tag_is_an_accepted_alternative(self, registry_proxy):
        handler, clients = registry_proxy
        clients["elbv2"].describe_tags.return_value = {
            "TagDescriptions": [
                {
                    "ResourceArn": _GATEWAY_ARN,
                    "Tags": [
                        {"Key": "eks:eks-cluster-name", "Value": "gco-us-east-1"},
                        {"Key": "gco.aws/gateway", "Value": "gco-system/gco-gateway"},
                    ],
                }
            ]
        }

        assert handler._resolve_registered_endpoint() == _GATEWAY_DNS

    def test_alb_from_another_cluster_is_refused(self, registry_proxy):
        handler, clients = registry_proxy
        clients["elbv2"].describe_tags.return_value = _gateway_tags(
            **{"elbv2.k8s.aws/cluster": "someone-else-us-east-1"}
        )

        with pytest.raises(RuntimeError, match="not owned by the GCO cluster"):
            handler._resolve_registered_endpoint()

    def test_cluster_alb_without_the_gateway_marker_is_refused(self, registry_proxy):
        handler, clients = registry_proxy
        clients["elbv2"].describe_tags.return_value = {
            "TagDescriptions": [
                {
                    "ResourceArn": _GATEWAY_ARN,
                    "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "gco-us-east-1"}],
                }
            ]
        }

        with pytest.raises(RuntimeError, match="not the GCO Gateway"):
            handler._resolve_registered_endpoint()

    def test_ownership_validation_requires_account_and_project(
        self, regional_proxy_module, monkeypatch
    ):
        handler, _, _ = regional_proxy_module
        monkeypatch.setenv("AWS_ACCOUNT_ID", "")
        monkeypatch.setenv("PROJECT_NAME", "gco")

        with pytest.raises(RuntimeError, match="ownership validation is not configured"):
            handler._validate_regional_endpoint_ownership(_GATEWAY_DNS, "us-east-1")
