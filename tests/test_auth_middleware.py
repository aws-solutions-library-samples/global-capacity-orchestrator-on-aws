"""
Tests for gco/services/auth_middleware.py.

Exercises the FastAPI authentication middleware that validates unique,
short-lived HMAC request envelopes using signing keys cached from Secrets
Manager. Coverage includes the unauthenticated-path allowlist (/healthz,
/readyz, /metrics, /api/v1/health), the explicit GCO_DEV_MODE bypass,
AWSCURRENT/AWSPENDING key rotation, replay rejection, bounded stale-cache
fallback, and refresh throttling. An autouse fixture resets all module-level
cache, timing, client, and nonce state between tests.
"""

import hashlib
import hmac
import os
import secrets
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gco.services.auth_middleware import (
    UNAUTHENTICATED_PATHS,
    AuthenticationMiddleware,
    clear_token_cache,
    get_secret_token,
    get_secrets_client,
    get_valid_tokens,
)


def _signed_headers(
    signing_key: str,
    method: str,
    target: str,
    body: str = "",
    *,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build the same one-request HMAC envelope as the trusted proxy Lambda."""
    timestamp = str(int(time.time()))
    request_nonce = nonce or secrets.token_hex(16)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = "\n".join(["v1", timestamp, request_nonce, method.upper(), target, content_hash])
    signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-gco-signature-version": "v1",
        "x-gco-signature": signature,
        "x-gco-timestamp": timestamp,
        "x-gco-nonce": request_nonce,
        "x-gco-content-sha256": content_hash,
    }


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset signing-key cache, monotonic timers, client, and replay nonces."""
    import gco.services.auth_middleware as auth_module

    clear_token_cache()
    auth_module._secrets_client = None
    yield
    clear_token_cache()
    auth_module._secrets_client = None


@pytest.fixture
def app_with_middleware():
    """Create FastAPI app with authentication middleware."""
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware)

    @app.get("/api/v1/health")
    async def get_health():  # nosemgrep: useless-inner-function
        return {"status": "healthy"}

    @app.get("/healthz")
    async def healthz():  # nosemgrep: useless-inner-function
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():  # nosemgrep: useless-inner-function
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics():  # nosemgrep: useless-inner-function
        return {"metrics": []}

    @app.post("/api/v1/manifests")
    async def submit_manifest():  # nosemgrep: useless-inner-function
        return {"success": True}

    return app


class TestUnauthenticatedPaths:
    """Tests for unauthenticated path handling."""

    def test_healthz_bypasses_auth(self, app_with_middleware):
        """Test /healthz endpoint bypasses authentication."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value={"secret-token"}),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/healthz")
            assert response.status_code == 200

    def test_readyz_bypasses_auth(self, app_with_middleware):
        """Test /readyz endpoint bypasses authentication."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value={"secret-token"}),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/readyz")
            assert response.status_code == 200

    def test_metrics_bypasses_auth(self, app_with_middleware):
        """Test /metrics endpoint bypasses authentication."""
        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value={"secret-token"}),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/metrics")
            assert response.status_code == 200

    def test_unauthenticated_paths_constant(self):
        """Test UNAUTHENTICATED_PATHS contains expected paths."""
        assert "/healthz" in UNAUTHENTICATED_PATHS
        assert "/readyz" in UNAUTHENTICATED_PATHS
        assert "/metrics" in UNAUTHENTICATED_PATHS
        assert "/api/v1/health" in UNAUTHENTICATED_PATHS

    def test_api_health_bypasses_auth(self, app_with_middleware):
        """Test /api/v1/health bypasses authentication for GA health checks."""
        with (
            patch.dict(
                os.environ,
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
        ):
            client = TestClient(app_with_middleware)
            response = client.get("/api/v1/health")
            assert response.status_code == 200


class TestAuthenticatedPaths:
    """Tests for signed backend request handling."""

    def test_valid_signature_allows_request(self, app_with_middleware):
        """A valid per-request envelope allows the request through."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("valid-key", "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 200

    def test_signature_from_unknown_key_returns_403(self, app_with_middleware):
        """An otherwise well-formed envelope signed by another key is rejected."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("wrong-key", "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 403

    def test_missing_signature_returns_403(self, app_with_middleware):
        """A request without an HMAC envelope is rejected."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post("/api/v1/manifests")
            assert response.status_code == 403

    def test_incomplete_signature_returns_403(self, app_with_middleware):
        """A partial envelope cannot be treated as backend authentication."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/manifests",
                headers={"x-gco-signature-version": "v1", "x-gco-signature": ""},
            )
            assert response.status_code == 403

    def test_pending_key_allowed_during_rotation(self, app_with_middleware):
        """AWSCURRENT and AWSPENDING keys can sign distinct requests during rotation."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"current-key", "pending-key"},
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response1 = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("current-key", "POST", "/api/v1/manifests"),
            )
            response2 = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("pending-key", "POST", "/api/v1/manifests"),
            )
            assert response1.status_code == 200
            assert response2.status_code == 200

    def test_replayed_envelope_is_rejected(self, app_with_middleware):
        """The same valid nonce cannot authenticate two backend requests."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            headers = _signed_headers("valid-key", "POST", "/api/v1/manifests")

            first = client.post("/api/v1/manifests", headers=headers)
            replay = client.post("/api/v1/manifests", headers=headers)

            assert first.status_code == 200
            assert replay.status_code == 403


class TestDevelopmentMode:
    """Tests for development mode (explicit GCO_DEV_MODE flag required)."""

    def test_dev_mode_allows_requests_when_no_secret(self, app_with_middleware):
        """Test requests allowed when GCO_DEV_MODE=true and AUTH_SECRET_ARN not set."""
        with (
            patch.dict("os.environ", {"GCO_DEV_MODE": "true"}, clear=True),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value=set()),
        ):
            client = TestClient(app_with_middleware)
            response = client.post("/api/v1/manifests")
            assert response.status_code == 200

    def test_no_secret_no_dev_mode_returns_503(self, app_with_middleware):
        """Test requests denied when AUTH_SECRET_ARN not set and GCO_DEV_MODE not enabled."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value=set()),
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post("/api/v1/manifests")
            assert response.status_code == 503

    def test_warning_logged_when_dev_mode(self, app_with_middleware):
        """Test warning is logged when dev mode bypasses auth."""
        with (
            patch.dict("os.environ", {"GCO_DEV_MODE": "true"}, clear=True),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value=set()),
            patch("gco.services.auth_middleware.logger") as mock_logger,
        ):
            client = TestClient(app_with_middleware)
            client.post("/api/v1/manifests")
            # Warning should be logged about bypassed auth
            mock_logger.warning.assert_called()


class TestGetSecretToken:
    """Tests for get_secret_token and get_valid_tokens functions."""

    def test_returns_none_when_no_arn(self):
        """Test returns None when AUTH_SECRET_ARN not set."""
        with patch.dict("os.environ", {}, clear=True):
            result = get_secret_token()
            assert result is None

    def test_get_valid_tokens_returns_empty_set_when_no_arn(self):
        """Test get_valid_tokens returns empty set when AUTH_SECRET_ARN not set."""
        with patch.dict("os.environ", {}, clear=True):
            result = get_valid_tokens()
            assert result == set()

    def test_fetches_secret_from_secrets_manager(self):
        """Test fetches secret from Secrets Manager."""
        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {
            "SecretString": '{"token": "my-secret-token"}'
        }
        # Mock ResourceNotFoundException for AWSPENDING
        mock_secrets.exceptions.ResourceNotFoundException = Exception

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            result = get_valid_tokens()
            assert "my-secret-token" in result

    def test_fetches_both_current_and_pending(self):
        """Test fetches both AWSCURRENT and AWSPENDING tokens during rotation."""
        mock_secrets = MagicMock()

        def mock_get_secret(SecretId, VersionStage):
            if VersionStage == "AWSCURRENT":
                return {"SecretString": '{"token": "current-token"}'}
            elif VersionStage == "AWSPENDING":
                return {"SecretString": '{"token": "pending-token"}'}

        mock_secrets.get_secret_value.side_effect = mock_get_secret
        mock_secrets.exceptions.ResourceNotFoundException = Exception

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            result = get_valid_tokens()
            assert "current-token" in result
            assert "pending-token" in result
            assert len(result) == 2

    def test_caches_tokens_with_ttl(self):
        """Test tokens are cached and reused within TTL."""
        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {"SecretString": '{"token": "cached-token"}'}
        mock_secrets.exceptions.ResourceNotFoundException = Exception

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            result1 = get_valid_tokens()
            first_refresh_calls = mock_secrets.get_secret_value.call_count
            # Second call should use cache without querying any secret stage.
            result2 = get_valid_tokens()

            assert result1 == result2
            assert first_refresh_calls == 3
            assert mock_secrets.get_secret_value.call_count == first_refresh_calls
            assert [
                call.kwargs["VersionStage"] for call in mock_secrets.get_secret_value.call_args_list
            ] == ["AWSCURRENT", "AWSPENDING", "AWSPREVIOUS"]

    def test_clear_token_cache(self):
        """Test clear_token_cache resets the cache."""
        import gco.services.auth_middleware as auth_module

        auth_module._cached_tokens = {"old-key"}
        auth_module._token_expirations = {"old-key": time.time() + 60}
        auth_module._cache_timestamp = 999999.0
        auth_module._last_successful_refresh = 999999.0
        auth_module._last_refresh_attempt = 999999.0
        auth_module._seen_nonces["0" * 32] = time.time() + 30

        clear_token_cache()

        assert auth_module._cached_tokens == set()
        assert auth_module._token_expirations == {}
        assert auth_module._cache_timestamp == 0.0
        assert auth_module._last_successful_refresh == 0.0
        assert auth_module._last_refresh_attempt == 0.0
        assert auth_module._seen_nonces == {}


class TestGetSecretsClient:
    """Tests for get_secrets_client function."""

    def test_creates_boto3_client(self):
        """Test creates boto3 Secrets Manager client with region from ARN."""
        # Reset the cached client
        import gco.services.auth_middleware as auth_module

        auth_module._secrets_client = None

        with (
            patch("gco.services.auth_middleware.boto3") as mock_boto3,
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:123456:secret:test"},
            ),
        ):
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            result = get_secrets_client()

            mock_boto3.client.assert_called_once_with("secretsmanager", region_name="us-east-2")
            assert result == mock_client

    def test_creates_client_with_no_region_when_arn_missing(self):
        """Test creates boto3 client with None region when ARN not set."""
        import gco.services.auth_middleware as auth_module

        auth_module._secrets_client = None

        with (
            patch("gco.services.auth_middleware.boto3") as mock_boto3,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            result = get_secrets_client()

            mock_boto3.client.assert_called_once_with("secretsmanager", region_name=None)
            assert result == mock_client

    def test_reuses_existing_client(self):
        """Test reuses existing client on subsequent calls."""
        import gco.services.auth_middleware as auth_module

        auth_module._secrets_client = None

        with patch("gco.services.auth_middleware.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            # First call
            result1 = get_secrets_client()
            # Second call
            result2 = get_secrets_client()

            # Should only create client once
            assert mock_boto3.client.call_count == 1
            assert result1 == result2


class TestMiddlewareLogging:
    """Tests for middleware logging behavior."""

    def test_logs_invalid_token_attempt(self, app_with_middleware):
        """Test logs warning on invalid token attempt."""
        with (
            patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-token"}),
            patch("gco.services.auth_middleware.logger") as mock_logger,
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            client.post(
                "/api/v1/manifests",
                headers=_signed_headers("invalid-key", "POST", "/api/v1/manifests"),
            )
            mock_logger.warning.assert_called()


class TestHeaderCaseSensitivity:
    """Tests for signature header case handling."""

    def test_lowercase_headers_work(self, app_with_middleware):
        """The lowercase names generated by the proxy are accepted."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            client = TestClient(app_with_middleware)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("valid-key", "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 200

    def test_mixed_case_headers_work(self, app_with_middleware):
        """HTTP case-insensitivity applies to every signature field."""
        with patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid-key"}):
            headers = {
                "-".join(part.capitalize() for part in name.split("-")): value
                for name, value in _signed_headers("valid-key", "POST", "/api/v1/manifests").items()
            }
            client = TestClient(app_with_middleware)
            response = client.post("/api/v1/manifests", headers=headers)
            assert response.status_code == 200


class TestSecretRotation:
    """Tests for signing-key rotation support."""

    @pytest.mark.parametrize("signing_key", ["current-key", "pending-key"])
    def test_accepts_current_and_pending_keys(self, app_with_middleware, signing_key):
        """Both AWSCURRENT and AWSPENDING keys authenticate fresh envelopes."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"current-key", "pending-key"},
        ):
            client = TestClient(app_with_middleware)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers(signing_key, "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 200

    def test_rejects_old_key_after_rotation(self, app_with_middleware):
        """An old key no longer authenticates once it leaves the cache."""
        with patch(
            "gco.services.auth_middleware.get_valid_tokens",
            return_value={"new-current-key"},
        ):
            client = TestClient(app_with_middleware, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/manifests",
                headers=_signed_headers("old-key", "POST", "/api/v1/manifests"),
            )
            assert response.status_code == 403


# =============================================================================
# Additional coverage tests for gco/services/auth_middleware.py
# =============================================================================


class TestAuthMiddlewareErrorHandlingExtended:
    """Extended tests for auth middleware error handling paths."""

    def test_refresh_cache_awscurrent_exception(self):
        """Test _refresh_cache handles AWSCURRENT fetch exception."""
        from gco.services.auth_middleware import _refresh_cache, get_valid_tokens

        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.side_effect = Exception("Connection error")
        mock_secrets.exceptions.ResourceNotFoundException = Exception

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            _refresh_cache()
            tokens = get_valid_tokens()
            assert tokens == set()

    def test_refresh_cache_awspending_generic_exception(self):
        """Test _refresh_cache handles AWSPENDING generic exception."""
        from gco.services.auth_middleware import _refresh_cache

        mock_secrets = MagicMock()

        def mock_get_secret(SecretId, VersionStage):
            if VersionStage == "AWSCURRENT":
                return {"SecretString": '{"token": "current-token"}'}
            elif VersionStage == "AWSPENDING":
                raise ValueError("Some other error")

        mock_secrets.get_secret_value.side_effect = mock_get_secret
        mock_secrets.exceptions.ResourceNotFoundException = KeyError

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            _refresh_cache()
            import gco.services.auth_middleware as auth_module

            assert "current-token" in auth_module._cached_tokens

    def test_refresh_cache_retains_previous_key_after_rotation(self):
        """A refreshed verifier accepts a warm signer's key during fixed overlap."""
        import gco.services.auth_middleware as auth_module
        from gco.services.auth_middleware import _refresh_cache

        mock_secrets = MagicMock()
        rotation_completed = 1_700_000_000.0
        mock_secrets.describe_secret.return_value = {
            "LastRotatedDate": datetime.fromtimestamp(rotation_completed, UTC)
        }

        def mock_get_secret(SecretId, VersionStage):
            del SecretId
            tokens = {
                "AWSCURRENT": "new-current-token",
                "AWSPREVIOUS": "former-current-token",
            }
            if VersionStage in tokens:
                return {"SecretString": f'{{"token": "{tokens[VersionStage]}"}}'}
            raise mock_secrets.exceptions.ResourceNotFoundException()

        mock_secrets.get_secret_value.side_effect = mock_get_secret
        mock_secrets.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
            patch(
                "gco.services.auth_middleware.time.time",
                return_value=rotation_completed + 1,
            ),
        ):
            assert _refresh_cache() is True

        assert auth_module._cached_tokens == {
            "new-current-token",
            "former-current-token",
        }
        assert auth_module._token_expirations == {
            "former-current-token": rotation_completed + auth_module.CACHE_MAX_STALE_SECONDS
        }
        mock_secrets.describe_secret.assert_called_once_with(
            SecretId="arn:aws:secretsmanager:us-east-1:123:secret:test"
        )
        requested_stages = [
            call.kwargs["VersionStage"] for call in mock_secrets.get_secret_value.call_args_list
        ]
        assert requested_stages == ["AWSCURRENT", "AWSPENDING", "AWSPREVIOUS"]

    def test_previous_key_expires_despite_successful_refreshes(self):
        """Repeated refreshes cannot renew AWSPREVIOUS beyond rotation overlap."""
        import gco.services.auth_middleware as auth_module
        from gco.services.auth_middleware import _refresh_cache

        mock_secrets = MagicMock()
        rotation_completed = 1_700_000_000.0
        mock_secrets.describe_secret.return_value = {
            "LastRotatedDate": datetime.fromtimestamp(rotation_completed, UTC)
        }

        def mock_get_secret(SecretId, VersionStage):
            del SecretId
            if VersionStage == "AWSCURRENT":
                return {"SecretString": '{"token": "new-current-token"}'}
            if VersionStage == "AWSPREVIOUS":
                return {"SecretString": '{"token": "former-current-token"}'}
            raise mock_secrets.exceptions.ResourceNotFoundException()

        mock_secrets.get_secret_value.side_effect = mock_get_secret
        mock_secrets.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )
        secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:test"
        deadline = rotation_completed + auth_module.CACHE_MAX_STALE_SECONDS

        with (
            patch.dict("os.environ", {"AUTH_SECRET_ARN": secret_arn}),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
            patch("gco.services.auth_middleware.time.time", return_value=deadline - 1),
        ):
            assert _refresh_cache() is True
            assert _refresh_cache() is True
            assert get_valid_tokens() == {"new-current-token", "former-current-token"}

        assert auth_module._token_expirations["former-current-token"] == deadline

        with patch("gco.services.auth_middleware.time.time", return_value=deadline + 1):
            assert get_valid_tokens() == {"new-current-token"}

    def test_previous_key_requires_rotation_completion_metadata(self):
        """Missing LastRotatedDate fails closed for AWSPREVIOUS only."""
        import gco.services.auth_middleware as auth_module
        from gco.services.auth_middleware import _refresh_cache

        mock_secrets = MagicMock()
        mock_secrets.describe_secret.return_value = {}

        def mock_get_secret(SecretId, VersionStage):
            del SecretId
            if VersionStage == "AWSCURRENT":
                return {"SecretString": '{"token": "current-token"}'}
            if VersionStage == "AWSPREVIOUS":
                return {"SecretString": '{"token": "previous-token"}'}
            raise mock_secrets.exceptions.ResourceNotFoundException()

        mock_secrets.get_secret_value.side_effect = mock_get_secret
        mock_secrets.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            assert _refresh_cache() is True

        assert auth_module._cached_tokens == {"current-token"}
        assert auth_module._token_expirations == {}

    def test_refresh_cache_outer_exception(self):
        """Test _refresh_cache handles outer exception gracefully."""
        from gco.services.auth_middleware import _refresh_cache

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch(
                "gco.services.auth_middleware.get_secrets_client",
                side_effect=Exception("Client creation failed"),
            ),
        ):
            _refresh_cache()

    def test_middleware_503_when_secret_configured_but_load_fails(self):
        """Test middleware returns 503 when secret is configured but can't be loaded."""
        from fastapi import FastAPI

        from gco.services.auth_middleware import AuthenticationMiddleware

        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware)

        @app.post("/api/v1/test")
        async def test_endpoint():
            return {"status": "ok"}

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_valid_tokens", return_value=set()),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/api/v1/test")
            assert response.status_code == 503

    def test_middleware_logs_client_ip_unknown(self):
        """Test middleware handles missing client IP gracefully."""
        from fastapi import FastAPI

        from gco.services.auth_middleware import AuthenticationMiddleware

        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware)

        @app.post("/api/v1/test")
        async def test_endpoint():
            return {"status": "ok"}

        with (
            patch("gco.services.auth_middleware.get_valid_tokens", return_value={"valid"}),
            patch("gco.services.auth_middleware.logger") as mock_logger,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/api/v1/test",
                headers=_signed_headers("invalid-key", "POST", "/api/v1/test"),
            )
            assert response.status_code == 403
            mock_logger.warning.assert_called()


# =============================================================================
# Bounded stale cache and refresh-throttle tests
# =============================================================================


class TestAuthMiddlewareStaleCacheFallback:
    """Tests for bounded stale-key use when Secrets Manager is unavailable."""

    def test_returns_bounded_stale_keys_without_extending_their_age(self):
        """A failed refresh preserves, but does not make stale keys younger."""
        import gco.services.auth_middleware as auth_module

        now = time.monotonic()
        original_refresh = now - auth_module.CACHE_TTL_SECONDS - 1
        auth_module._cached_tokens = {"stale-key"}
        auth_module._cache_timestamp = original_refresh
        auth_module._last_successful_refresh = original_refresh
        auth_module._last_refresh_attempt = 0.0

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch(
                "gco.services.auth_middleware.get_secrets_client",
                side_effect=Exception("SM unavailable"),
            ),
        ):
            assert get_valid_tokens() == {"stale-key"}

        assert auth_module._last_successful_refresh == original_refresh
        assert auth_module._cache_timestamp == original_refresh
        assert auth_module._last_refresh_attempt >= now

    def test_refresh_failure_is_throttled(self):
        """Repeated requests within CACHE_RETRY_SECONDS do not hammer Secrets Manager."""
        import gco.services.auth_middleware as auth_module

        now = time.monotonic()
        auth_module._cached_tokens = {"stale-key"}
        auth_module._cache_timestamp = now - auth_module.CACHE_TTL_SECONDS - 1
        auth_module._last_successful_refresh = auth_module._cache_timestamp
        auth_module._last_refresh_attempt = now

        with patch("gco.services.auth_middleware._refresh_cache") as refresh:
            assert get_valid_tokens() == {"stale-key"}

        refresh.assert_not_called()

    def test_keys_older_than_max_stale_age_are_rejected(self):
        """A cached key cannot fail open beyond CACHE_MAX_STALE_SECONDS."""
        import gco.services.auth_middleware as auth_module

        now = time.monotonic()
        auth_module._cached_tokens = {"expired-key"}
        auth_module._cache_timestamp = now - auth_module.CACHE_MAX_STALE_SECONDS - 1
        auth_module._last_successful_refresh = auth_module._cache_timestamp
        auth_module._last_refresh_attempt = now

        assert get_valid_tokens() == set()

    def test_successful_refresh_replaces_keys_and_monotonic_timestamps(self):
        """A successful refresh replaces old keys and records monotonic timing state."""
        import gco.services.auth_middleware as auth_module
        from gco.services.auth_middleware import _refresh_cache

        auth_module._cached_tokens = {"old-key"}
        before = time.monotonic()
        mock_secrets = MagicMock()

        def mock_get(SecretId, VersionStage):
            if VersionStage == "AWSCURRENT":
                return {"SecretString": '{"token": "new-key"}'}
            raise mock_secrets.exceptions.ResourceNotFoundException()

        mock_secrets.get_secret_value.side_effect = mock_get
        mock_secrets.exceptions.ResourceNotFoundException = type(
            "ResourceNotFoundException", (Exception,), {}
        )

        with (
            patch.dict(
                "os.environ",
                {"AUTH_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:test"},
            ),
            patch("gco.services.auth_middleware.get_secrets_client", return_value=mock_secrets),
        ):
            assert _refresh_cache() is True

        assert auth_module._cached_tokens == {"new-key"}
        assert auth_module._last_successful_refresh >= before
        assert auth_module._last_refresh_attempt == auth_module._last_successful_refresh
        assert auth_module._cache_timestamp == auth_module._last_successful_refresh
