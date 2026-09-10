"""
Extended tests for lambda/proxy-shared/proxy_utils.py.

Covers the forward_request paths the base proxy suite doesn't reach:
urllib3 TimeoutError → 504 response, MaxRetryError → 503 response,
unknown exceptions → 500 without retry, retryable statuses that
persist through every attempt, hop-by-hop header stripping in
_build_success_response, and body encoding (None vs string). Also
exercises get_secret_token's thread-safe caching and the stale-cache
fallback when Secrets Manager becomes unavailable.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import urllib3

from tests._lambda_imports import load_lambda_module


@pytest.fixture
def proxy_module():
    """Import proxy_utils with mocked boto3 and env.

    Loaded via :func:`load_lambda_module` — see
    ``tests/_lambda_imports.py`` for the rationale.
    """
    with (
        patch("boto3.client") as mock_client,
        patch.dict(
            "os.environ",
            {
                "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test",
                "SECRET_CACHE_TTL_SECONDS": "300",
                "PROXY_MAX_RETRIES": "3",
                "PROXY_RETRY_BACKOFF_BASE": "0.001",
            },
        ),
    ):
        proxy_utils = load_lambda_module("proxy-shared", "proxy_utils", shared_dirs=["tls-shared"])

        proxy_utils._cached_secret = None
        proxy_utils._cache_timestamp = 0.0
        proxy_utils._last_successful_refresh = 0.0
        proxy_utils._last_refresh_attempt = 0.0

        mock_sm = mock_client.return_value
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"token": "test-token"})
        }
        yield proxy_utils, mock_sm


@pytest.fixture(
    params=["proxy-shared", "api-gateway-proxy", "regional-api-proxy"],
    ids=["shared", "global-deployment", "regional-deployment"],
)
def packaged_proxy_module(request):
    """Load every proxy helper copy that is packaged or used as its source."""
    with (
        patch("boto3.client"),
        patch("urllib3.PoolManager") as mock_pool_cls,
        patch.dict(
            "os.environ",
            {
                "PROXY_MAX_RETRIES": "3",
                "PROXY_RETRY_BACKOFF_BASE": "0.1",
            },
        ),
    ):
        module = load_lambda_module(request.param, "proxy_utils", shared_dirs=["tls-shared"])
        module._http = mock_pool_cls.return_value
        yield module


class TestPackagedProxyHelperContract:
    """Keep URL encoding and request-budget behavior aligned in all copies."""

    def test_encodes_malformed_percent_and_repeated_query_values(self, packaged_proxy_module):
        url = packaged_proxy_module.build_target_url(
            "example.com/base/",
            "/valid/%2F/literal%/bad%2G space",
            {"tag": ["first value", "second&value"]},
        )

        assert url == (
            "https://example.com/base/valid/%2F/literal%25/bad%252G%20space"
            "?tag=first+value&tag=second%26value"
        )

    def test_retries_share_one_total_timeout(self, packaged_proxy_module):
        retryable_response = MagicMock()
        retryable_response.status = 503
        successful_response = MagicMock()
        successful_response.status = 200
        successful_response.headers = {}
        successful_response.data = b"OK"
        mock_http = MagicMock()
        mock_http.request.side_effect = [retryable_response, successful_response]

        with (
            patch.object(packaged_proxy_module, "_http", mock_http),
            patch.object(
                packaged_proxy_module.time,
                "monotonic",
                side_effect=[0.0, 0.0, 0.5, 0.75],
            ),
            patch.object(packaged_proxy_module.time, "sleep"),
        ):
            result = packaged_proxy_module.forward_request(
                "https://example.com/api", "GET", {}, None, timeout=2.0
            )

        assert result["statusCode"] == 200
        attempt_timeouts = [
            call.kwargs["timeout"].total for call in mock_http.request.call_args_list
        ]
        assert attempt_timeouts == pytest.approx([2.0, 1.25])

    @pytest.mark.parametrize(
        "target_url",
        [
            "http://example.com/api",
            "https://example.com:8443/api",
        ],
    )
    def test_rejects_plaintext_and_non_443_targets(self, packaged_proxy_module, target_url):
        with pytest.raises(ValueError, match="Backend proxy targets must use HTTPS on port 443"):
            packaged_proxy_module.forward_request(target_url, "GET", {}, None)


class TestForwardRequestTimeout:
    """Tests for forward_request timeout handling."""

    def test_timeout_returns_504(self, proxy_module):
        """TimeoutError should result in 504 after retries."""
        pu, _ = proxy_module

        mock_http = MagicMock()
        mock_http.request.side_effect = urllib3.exceptions.TimeoutError("timed out")

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None, timeout=1.0)

        assert result["statusCode"] == 504
        body = json.loads(result["body"])
        assert "Gateway timeout" in body["error"]

    def test_retry_attempts_share_one_total_budget(self, proxy_module):
        """Each retry receives only the time remaining from the original budget."""
        pu, _ = proxy_module
        mock_http = MagicMock()
        mock_http.request.side_effect = urllib3.exceptions.TimeoutError("timed out")

        with (
            patch.object(pu, "_http", mock_http),
            patch.object(pu.time, "monotonic", side_effect=[0.0, 0.0, 0.6, 0.6, 1.0]),
            patch.object(pu.time, "sleep"),
        ):
            result = pu.forward_request("https://example.com/api", "GET", {}, None, timeout=1.0)

        assert result["statusCode"] == 504
        assert mock_http.request.call_count == 2
        attempt_timeouts = [
            call.kwargs["timeout"].total for call in mock_http.request.call_args_list
        ]
        assert attempt_timeouts == pytest.approx([1.0, 0.4])

    def test_connection_error_returns_503(self, proxy_module):
        """MaxRetryError should result in 503 after retries."""
        pu, _ = proxy_module

        mock_http = MagicMock()
        mock_http.request.side_effect = urllib3.exceptions.MaxRetryError(
            pool=None, url="https://example.com", reason="Connection refused"
        )

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 503
        body = json.loads(result["body"])
        assert "Service unavailable" in body["error"]


class TestForwardRequestUnknownException:
    """Tests for forward_request unknown exception handling."""

    def test_unknown_exception_returns_500_no_retry(self, proxy_module):
        """Unknown exceptions should return 500 immediately without retry."""
        pu, _ = proxy_module

        mock_http = MagicMock()
        mock_http.request.side_effect = RuntimeError("Unexpected crash")

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Internal server error" in body["error"]
        # Should only be called once (no retry)
        assert mock_http.request.call_count == 1


class TestForwardRequestRetryableStatus:
    """Tests for retryable status code handling."""

    def test_502_retries_then_returns_last_response(self, proxy_module):
        """502 should retry and return the last response on exhaustion."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 502
        mock_response.headers = {}
        mock_response.data = b'{"error": "Bad Gateway"}'

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 502
        assert mock_http.request.call_count == 3  # 3 retries

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_mutating_methods_never_retry(self, proxy_module, method):
        """A transient response cannot replay a potentially mutating request."""
        pu, _ = proxy_module
        response = MagicMock(status=503, headers={}, data=b"unavailable")
        mock_http = MagicMock()
        mock_http.request.return_value = response

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request(
                "https://example.com/api", method, {}, '{"operation":"mutate"}'
            )

        assert result["statusCode"] == 503
        assert mock_http.request.call_count == 1
        response.release_conn.assert_not_called()

    def test_429_retries_then_succeeds(self, proxy_module):
        """429 should retry and succeed if next attempt returns 200."""
        pu, _ = proxy_module

        fail_response = MagicMock()
        fail_response.status = 429

        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.headers = {"Content-Type": "application/json"}
        ok_response.data = b'{"ok": true}'

        mock_http = MagicMock()
        mock_http.request.side_effect = [fail_response, ok_response]

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 200
        assert mock_http.request.call_count == 2

    def test_504_retries(self, proxy_module):
        """504 should be retried."""
        pu, _ = proxy_module

        fail_response = MagicMock()
        fail_response.status = 504

        ok_response = MagicMock()
        ok_response.status = 200
        ok_response.headers = {}
        ok_response.data = b'{"ok": true}'

        mock_http = MagicMock()
        mock_http.request.side_effect = [fail_response, ok_response]

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 200

    def test_non_retryable_status_returned_immediately(self, proxy_module):
        """Non-retryable status codes (400, 404, etc.) should return immediately."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.data = b'{"error": "Not found"}'

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 404
        assert mock_http.request.call_count == 1


class TestBuildSuccessResponse:
    """Tests for _build_success_response hop-by-hop header removal."""

    def test_removes_hop_by_hop_headers(self, proxy_module):
        """Hop-by-hop headers should be stripped from response."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "Transfer-Encoding": "chunked",
            "X-Custom": "preserved",
        }
        mock_response.data = b'{"ok": true}'

        result = pu._build_success_response(mock_response)

        assert result["statusCode"] == 200
        assert "Connection" not in result["headers"]
        assert "connection" not in result["headers"]
        assert "Keep-Alive" not in result["headers"]
        assert "Transfer-Encoding" not in result["headers"]
        assert result["headers"]["X-Custom"] == "preserved"
        assert result["headers"]["Content-Type"] == "application/json"

    def test_removes_lowercase_hop_by_hop(self, proxy_module):
        """Lowercase hop-by-hop headers should also be removed."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {
            "te": "trailers",
            "trailer": "Expires",
            "upgrade": "websocket",
        }
        mock_response.data = b"{}"

        result = pu._build_success_response(mock_response)

        assert "te" not in result["headers"]
        assert "trailer" not in result["headers"]
        assert "upgrade" not in result["headers"]


class TestForwardRequestBodyEncoding:
    """Tests for body encoding in forward_request."""

    def test_none_body_sends_none(self, proxy_module):
        """None body should send None to urllib3."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.data = b"{}"

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            pu.forward_request("https://example.com", "GET", {}, None)

        call_kwargs = mock_http.request.call_args[1]
        assert call_kwargs["body"] is None

    def test_string_body_encoded_to_utf8(self, proxy_module):
        """String body should be encoded to UTF-8 bytes."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.data = b"{}"

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            pu.forward_request("https://example.com", "POST", {}, '{"key": "value"}')

        call_kwargs = mock_http.request.call_args[1]
        assert call_kwargs["body"] == b'{"key": "value"}'

    def test_empty_string_body_sends_none(self, proxy_module):
        """Empty string body should send None (falsy)."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.data = b"{}"

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            pu.forward_request("https://example.com", "POST", {}, "")

        call_kwargs = mock_http.request.call_args[1]
        assert call_kwargs["body"] is None


class TestGetSecretTokenCaching:
    """Tests for get_secret_token caching behavior."""

    def test_caches_within_ttl(self, proxy_module):
        """Second call within TTL should not hit Secrets Manager."""
        pu, mock_sm = proxy_module

        pu.get_secret_token()
        mock_sm.get_secret_value.reset_mock()

        token = pu.get_secret_token()
        assert token == "test-token"  # nosec B105 - test assertion against fixture value, not a real credential
        mock_sm.get_secret_value.assert_not_called()

    def test_refreshes_after_ttl(self, proxy_module):
        """Call after TTL should refresh from Secrets Manager."""
        pu, mock_sm = proxy_module

        pu.get_secret_token()
        now = time.monotonic()
        pu._cache_timestamp = now - 400
        pu._last_successful_refresh = now - 400
        pu._last_refresh_attempt = 0.0  # Expire normal TTL and permit refresh

        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"token": "new-token"})}

        token = pu.get_secret_token()
        assert token == "new-token"  # nosec B105 - test assertion against fixture value, not a real credential

    def test_stale_cache_on_sm_failure(self, proxy_module):
        """SM failure with existing cache should return stale token."""
        pu, mock_sm = proxy_module

        pu.get_secret_token()
        now = time.monotonic()
        pu._cache_timestamp = now - 400
        pu._last_successful_refresh = now - 400
        pu._last_refresh_attempt = 0.0  # Expire normal TTL and permit refresh

        mock_sm.get_secret_value.side_effect = Exception("SM down")

        token = pu.get_secret_token()
        assert token == "test-token"  # Stale cache

    def test_stale_cache_refresh_is_throttled(self, proxy_module):
        """Requests inside the retry window reuse bounded stale data without an SM call."""
        pu, mock_sm = proxy_module
        pu.get_secret_token()
        now = time.monotonic()
        pu._last_successful_refresh = now - pu._CACHE_TTL_SECONDS - 1
        pu._cache_timestamp = pu._last_successful_refresh
        pu._last_refresh_attempt = now
        mock_sm.get_secret_value.reset_mock()

        assert pu.get_secret_token() == "test-token"
        mock_sm.get_secret_value.assert_not_called()

    def test_stale_cache_expires_at_max_age(self, proxy_module):
        """A signing key older than the stale ceiling fails closed on refresh failure."""
        pu, mock_sm = proxy_module
        pu.get_secret_token()
        now = time.monotonic()
        pu._last_successful_refresh = now - pu._CACHE_MAX_STALE_SECONDS - 1
        pu._cache_timestamp = pu._last_successful_refresh
        pu._last_refresh_attempt = 0.0
        mock_sm.get_secret_value.side_effect = Exception("SM down")

        with pytest.raises(RuntimeError, match="Authentication signing key is unavailable"):
            pu.get_secret_token()

    def test_no_cache_and_sm_failure_raises(self, proxy_module):
        """SM failure with no cache should raise RuntimeError."""
        pu, mock_sm = proxy_module

        mock_sm.get_secret_value.side_effect = Exception("SM down")

        with pytest.raises(RuntimeError, match="Authentication signing key is unavailable"):
            pu.get_secret_token()


class TestForwardRequestSuccess:
    """Tests for successful forward_request scenarios."""

    def test_200_with_json_body(self, proxy_module):
        """200 response should be returned with decoded body."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.data = b'{"result": "ok"}'

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request(
                "https://example.com/api",
                "POST",
                {"Accept": "application/json"},
                '{"input": "data"}',
                timeout=10.0,
            )

        assert result["statusCode"] == 200
        assert result["body"] == '{"result": "ok"}'
        assert result["headers"]["Content-Type"] == "application/json"

    def test_201_returned_immediately(self, proxy_module):
        """201 Created should be returned without retry."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 201
        mock_response.headers = {}
        mock_response.data = b'{"id": "123"}'

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "POST", {}, '{"name": "test"}')

        assert result["statusCode"] == 201
        assert mock_http.request.call_count == 1

    def test_custom_timeout_passed_to_urllib3(self, proxy_module):
        """Custom timeout should bound urllib3's total attempt time."""
        pu, _ = proxy_module

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.data = b"{}"

        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        with patch.object(pu, "_http", mock_http):
            pu.forward_request("https://example.com", "GET", {}, None, timeout=5.0)

        request_timeout = mock_http.request.call_args.kwargs["timeout"]
        assert isinstance(request_timeout, urllib3.Timeout)
        assert 0 < request_timeout.total <= 5.0


class TestBoundedEnvParsing:
    """Malformed or out-of-range tuning values fall back to the defaults."""

    def test_float_falls_back_on_garbage_and_out_of_range(self, proxy_module, monkeypatch):
        pu, _ = proxy_module
        monkeypatch.setenv("PROXY_TUNING", "not-a-number")
        assert pu._bounded_env_float("PROXY_TUNING", 0.3, 0.0, 5.0) == 0.3
        monkeypatch.setenv("PROXY_TUNING", "9.5")
        assert pu._bounded_env_float("PROXY_TUNING", 0.3, 0.0, 5.0) == 0.3
        monkeypatch.setenv("PROXY_TUNING", "1.5")
        assert pu._bounded_env_float("PROXY_TUNING", 0.3, 0.0, 5.0) == 1.5

    def test_int_falls_back_on_garbage_and_out_of_range(self, proxy_module, monkeypatch):
        pu, _ = proxy_module
        monkeypatch.setenv("PROXY_TUNING", "three")
        assert pu._bounded_env_int("PROXY_TUNING", 3, 1, 5) == 3
        monkeypatch.setenv("PROXY_TUNING", "0")
        assert pu._bounded_env_int("PROXY_TUNING", 3, 1, 5) == 3
        monkeypatch.setenv("PROXY_TUNING", "4")
        assert pu._bounded_env_int("PROXY_TUNING", 3, 1, 5) == 4


class _LockThatLetsAnotherRefreshWin:
    """Stand-in for ``_secret_lock`` that mutates module state on acquisition.

    Simulates the race the double-checked locking in ``get_secret_token``
    exists for: by the time this caller acquires the lock, a concurrent
    invocation has already finished its own refresh (or refresh attempt).
    """

    def __init__(self, module, on_acquire):
        self._module = module
        self._on_acquire = on_acquire

    def __enter__(self):
        self._on_acquire(self._module)

    def __exit__(self, *exc_info):
        return False


class TestGetSecretTokenDoubleCheckedLocking:
    def test_fresh_refresh_by_another_caller_is_reused_under_the_lock(self, proxy_module):
        pu, mock_sm = proxy_module
        # Expired from this caller's point of view before it takes the lock.
        pu._cached_secret = "old-token"  # nosec B105 - fixture value, not a credential
        pu._last_successful_refresh = time.monotonic() - pu._CACHE_TTL_SECONDS - 1
        pu._last_refresh_attempt = 0.0

        def other_caller_refreshed(module):
            module._cached_secret = "refreshed-elsewhere"  # nosec B105 - fixture value
            module._last_successful_refresh = time.monotonic()

        pu._secret_lock = _LockThatLetsAnotherRefreshWin(pu, other_caller_refreshed)

        assert pu.get_secret_token() == "refreshed-elsewhere"  # nosec B105 - fixture value
        mock_sm.get_secret_value.assert_not_called()

    def test_recent_failed_attempt_by_another_caller_keeps_stale_key(self, proxy_module):
        pu, mock_sm = proxy_module
        pu._cached_secret = "stale-token"  # nosec B105 - fixture value, not a credential
        pu._last_successful_refresh = time.monotonic() - pu._CACHE_TTL_SECONDS - 1
        pu._last_refresh_attempt = 0.0

        def other_caller_just_tried(module):
            module._last_refresh_attempt = time.monotonic()

        pu._secret_lock = _LockThatLetsAnotherRefreshWin(pu, other_caller_just_tried)

        assert pu.get_secret_token() == "stale-token"  # nosec B105 - fixture value
        mock_sm.get_secret_value.assert_not_called()

    def test_empty_token_in_secret_fails_closed(self, proxy_module):
        pu, mock_sm = proxy_module
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"token": ""})}

        with pytest.raises(RuntimeError, match="Authentication signing key is unavailable"):
            pu.get_secret_token()


class TestForwardRequestTlsAndTrust:
    def test_tls_verification_failure_is_a_bounded_502(self, proxy_module):
        pu, _ = proxy_module
        mock_http = MagicMock()
        mock_http.request.side_effect = urllib3.exceptions.SSLError("certificate verify failed")

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 502
        assert json.loads(result["body"]) == {"error": "Backend TLS verification failed"}
        assert mock_http.request.call_count == 1

    def test_tls_failure_wrapped_in_max_retry_error_is_a_502(self, proxy_module):
        pu, _ = proxy_module
        mock_http = MagicMock()
        mock_http.request.side_effect = urllib3.exceptions.MaxRetryError(
            pool=None,
            url="https://example.com",
            reason=urllib3.exceptions.SSLError("certificate verify failed"),
        )

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 502
        assert mock_http.request.call_count == 1

    def test_unavailable_trust_bundle_is_a_503_before_any_request(self, proxy_module):
        pu, _ = proxy_module

        with (
            patch.object(pu, "_http", None),
            patch.object(pu, "get_backend_http_pool", side_effect=RuntimeError("no bundle")),
        ):
            result = pu.forward_request("https://example.com/api", "GET", {}, None)

        assert result["statusCode"] == 503
        assert json.loads(result["body"]) == {"error": "Backend trust is temporarily unavailable"}


class TestForwardRequestBudgetEdges:
    def test_exhausted_budget_returns_504_without_a_request(self, proxy_module):
        pu, _ = proxy_module
        mock_http = MagicMock()

        with patch.object(pu, "_http", mock_http):
            result = pu.forward_request("https://example.com/api", "GET", {}, None, timeout=0.0)

        assert result["statusCode"] == 504
        assert json.loads(result["body"]) == {"error": "Gateway timeout"}
        mock_http.request.assert_not_called()

    def test_retryable_status_is_relayed_when_backoff_would_overrun(self, proxy_module):
        pu, _ = proxy_module
        response = MagicMock(status=503, headers={}, data=b"busy")
        mock_http = MagicMock()
        mock_http.request.return_value = response

        with (
            patch.object(pu, "_http", mock_http),
            patch.object(pu, "_RETRY_BACKOFF_BASE", 100.0),
        ):
            result = pu.forward_request("https://example.com/api", "GET", {}, None, timeout=1.0)

        # One attempt, then the backoff would outlive the budget, so the last
        # upstream answer is relayed instead of a synthetic timeout.
        assert result["statusCode"] == 503
        assert result["body"] == "busy"
        assert mock_http.request.call_count == 1
        response.release_conn.assert_called_once()

    def test_unparseable_target_port_is_rejected(self, proxy_module):
        pu, _ = proxy_module
        with pytest.raises(ValueError, match="invalid port"):
            pu.forward_request("https://example.com:notaport/api", "GET", {}, None)


class TestBuildTargetUrlValidation:
    def test_unparseable_endpoint_port_is_rejected(self, proxy_module):
        pu, _ = proxy_module
        with pytest.raises(ValueError, match="Invalid proxy endpoint"):
            pu.build_target_url("https://example.com:notaport", "/api", None)

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://example.com",
            "https://example.com:8443",
            "https://user:pw@example.com",
            "https://example.com/?q=1",
            "https://example.com/#frag",
        ],
    )
    def test_non_https_443_endpoints_are_rejected(self, proxy_module, endpoint):
        pu, _ = proxy_module
        with pytest.raises(ValueError, match="HTTPS on port 443"):
            pu.build_target_url(endpoint, "/api", None)
