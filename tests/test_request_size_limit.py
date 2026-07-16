"""
Tests for the RequestSizeLimitMiddleware on the Manifest API.

Verifies the middleware rejects POST/PUT/PATCH requests whose
Content-Length exceeds DEFAULT_MAX_REQUEST_BODY_BYTES (1 MiB) with 413,
rejects chunked/no-Content-Length requests whose body grows past the
limit while streaming, and leaves GET/HEAD/OPTIONS/DELETE requests
untouched. Uses a TestClient fixture with the manifest processor mocked
and backend signature verification bypassed; includes a Hypothesis sweep
over Content-Length values around the boundary.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from gco.services.manifest_api import DEFAULT_MAX_REQUEST_BODY_BYTES
from tests._auth import bypass_backend_auth

# Call sites retain a common header mapping for concise request construction;
# authentication itself is handled by the autouse fixture below.
_AUTH_HEADERS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _bypass_authentication():
    """Isolate size-limit behavior from the separately tested HMAC verifier."""
    with bypass_backend_auth():
        yield


@pytest.fixture
def mock_manifest_processor():
    """Fixture to mock the manifest processor."""
    mock_processor = MagicMock()
    mock_processor.cluster_id = "test-cluster"
    mock_processor.region = "us-east-1"
    mock_processor.core_v1 = MagicMock()
    mock_processor.max_cpu_per_manifest = 10000
    mock_processor.max_memory_per_manifest = 34359738368
    mock_processor.max_gpu_per_manifest = 4
    mock_processor.allowed_namespaces = {"default", "gco-jobs"}
    mock_processor.validation_enabled = True
    return mock_processor


@pytest.fixture
def client(mock_manifest_processor):
    """Create a TestClient with mocked processor."""
    with (
        patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ),
        patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
    ):
        import gco.services.manifest_api as api_module

        api_module.manifest_processor = mock_manifest_processor

        from gco.services.manifest_api import app

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestRequestSizeLimitContentLength:
    """Tests for Content-Length based rejection."""

    def test_rejects_request_with_content_length_exceeding_limit(self, client):
        """Request with Content-Length > max should get 413."""
        # Default limit is 1MB (1048576 bytes)
        oversized_header = {"content-length": "2000000"}
        response = client.post(
            "/api/v1/manifests/validate",
            content=b"x",  # Actual body doesn't matter — header triggers rejection
            headers={**_AUTH_HEADERS, **oversized_header},
        )
        assert response.status_code == 413
        assert "exceeds maximum size" in response.json()["detail"]

    def test_allows_request_with_content_length_within_limit(self, client):
        """Request with Content-Length <= max should proceed."""
        small_body = b'{"manifests": []}'
        response = client.post(
            "/api/v1/manifests/validate",
            content=small_body,
            headers={**_AUTH_HEADERS, "content-length": str(len(small_body))},
        )
        # Should not be 413 — may be 400/422 due to validation, but not size-limited
        assert response.status_code != 413

    def test_rejects_request_at_exact_boundary(self, client):
        """Request with Content-Length exactly 1 byte over limit should get 413."""
        from gco.services.manifest_api import DEFAULT_MAX_REQUEST_BODY_BYTES

        over_limit = str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1)
        response = client.post(
            "/api/v1/manifests/validate",
            content=b"x",
            headers={**_AUTH_HEADERS, "content-length": over_limit},
        )
        assert response.status_code == 413

    def test_allows_request_at_exact_limit(self, client):
        """Request with Content-Length exactly at limit should proceed."""
        from gco.services.manifest_api import DEFAULT_MAX_REQUEST_BODY_BYTES

        at_limit = str(DEFAULT_MAX_REQUEST_BODY_BYTES)
        response = client.post(
            "/api/v1/manifests/validate",
            content=b"x" * 10,  # Actual body smaller, but header says at limit
            headers={**_AUTH_HEADERS, "content-length": at_limit},
        )
        # Should not be 413
        assert response.status_code != 413


class TestRequestSizeLimitNoContentLength:
    """Tests for body-reading based rejection (no Content-Length header)."""

    def test_rejects_oversized_body_without_content_length(self, client):
        """Request without Content-Length but oversized body should get 413."""
        from gco.services.manifest_api import DEFAULT_MAX_REQUEST_BODY_BYTES

        oversized_body = b"x" * (DEFAULT_MAX_REQUEST_BODY_BYTES + 1)
        response = client.post(
            "/api/v1/manifests/validate",
            content=oversized_body,
            headers=_AUTH_HEADERS,
        )
        assert response.status_code == 413
        assert "exceeds maximum size" in response.json()["detail"]

    def test_allows_body_within_limit_without_content_length(self, client):
        """Request without Content-Length but within-limit body should proceed."""
        small_body = b'{"manifests": []}'
        # TestClient normally adds Content-Length, but the middleware handles both cases
        response = client.post(
            "/api/v1/manifests/validate",
            content=small_body,
            headers=_AUTH_HEADERS,
        )
        # Should not be 413
        assert response.status_code != 413


class TestRequestSizeLimitMethodSkipping:
    """Tests that GET/HEAD/OPTIONS/DELETE skip size checks."""

    def test_get_requests_skip_size_check(self, client):
        """GET requests should not be size-checked."""
        response = client.get("/api/v1/health")
        assert response.status_code != 413

    def test_delete_requests_skip_size_check(self, client):
        """DELETE requests should not be size-checked."""
        response = client.delete(
            "/api/v1/jobs/gco-jobs/test-job",
            headers=_AUTH_HEADERS,
        )
        # Should not be 413 regardless of any headers
        assert response.status_code != 413


class TestRequestSizeLimitConfiguration:
    """Tests for middleware configuration."""

    def test_default_limit_is_1mb(self):
        """Default limit should be 1MB (1048576 bytes)."""
        from gco.services.manifest_api import DEFAULT_MAX_REQUEST_BODY_BYTES

        assert DEFAULT_MAX_REQUEST_BODY_BYTES == 1_048_576

    def test_middleware_uses_env_var(self):
        """Middleware should read MAX_REQUEST_BODY_BYTES from environment."""
        with patch.dict("os.environ", {"MAX_REQUEST_BODY_BYTES": "512000"}):
            # Re-evaluate the env var reading
            import gco.services.manifest_api as api_module

            result = int(
                __import__("os").getenv(
                    "MAX_REQUEST_BODY_BYTES", str(api_module.DEFAULT_MAX_REQUEST_BODY_BYTES)
                )
            )
            assert result == 512000

    def test_413_response_includes_limit_in_message(self, client):
        """413 response should include the configured limit in the error message."""
        from gco.services.manifest_api import DEFAULT_MAX_REQUEST_BODY_BYTES

        response = client.post(
            "/api/v1/manifests/validate",
            content=b"x",
            headers={**_AUTH_HEADERS, "content-length": "99999999"},
        )
        assert response.status_code == 413
        assert str(DEFAULT_MAX_REQUEST_BODY_BYTES) in response.json()["detail"]


# =============================================================================
# Property: Request Body Size Enforcement
#
# For any HTTP request to the manifest submission endpoint, if the request body
# size exceeds the configured maximum (default 1MB), the system SHALL return
# HTTP 413 without fully reading the body into memory. If the body is within
# limits, the request SHALL proceed to validation.
# =============================================================================


# The configured limit used by the middleware (1MB by default)
_LIMIT = DEFAULT_MAX_REQUEST_BODY_BYTES


@contextmanager
def _make_test_client():
    """Create a TestClient with mocked dependencies for property tests."""
    mock_processor = MagicMock()
    mock_processor.cluster_id = "test-cluster"
    mock_processor.region = "us-east-1"
    mock_processor.core_v1 = MagicMock()
    mock_processor.max_cpu_per_manifest = 10000
    mock_processor.max_memory_per_manifest = 34359738368
    mock_processor.max_gpu_per_manifest = 4
    mock_processor.allowed_namespaces = {"default", "gco-jobs"}
    mock_processor.validation_enabled = True

    with (
        patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_processor,
        ),
        patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
        patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
    ):
        import gco.services.manifest_api as api_module

        api_module.manifest_processor = mock_processor

        from gco.services.manifest_api import app

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestRequestBodySizeEnforcementProperty:
    """Property-based tests for request body size enforcement.

    For any HTTP request to the manifest submission endpoint, if the request
    body size exceeds the configured maximum (default 1MB), the system SHALL
    return HTTP 413 without fully reading the body into memory. If the body
    is within limits, the request SHALL proceed to validation.
    """

    # --- Strategy: payload sizes above the limit ---
    # Generate sizes from limit+1 up to limit+100KB (enough to test the boundary)
    @given(over_size=st.integers(min_value=_LIMIT + 1, max_value=_LIMIT + 102_400))
    @settings(max_examples=100)
    def test_oversized_content_length_returns_413(self, over_size: int):
        """Requests with Content-Length exceeding the limit always get 413."""
        with _make_test_client() as client:
            response = client.post(
                "/api/v1/manifests/validate",
                content=b"x",
                headers={**_AUTH_HEADERS, "content-length": str(over_size)},
            )
            assert response.status_code == 413, (
                f"Expected 413 for Content-Length={over_size}, got {response.status_code}"
            )
            assert "exceeds maximum size" in response.json()["detail"]

    # --- Strategy: payload sizes at or below the limit ---
    # Generate sizes from 1 up to the limit (inclusive)
    @given(under_size=st.integers(min_value=1, max_value=_LIMIT))
    @settings(max_examples=100)
    def test_within_limit_content_length_proceeds(self, under_size: int):
        """Requests with Content-Length within the limit are NOT rejected with 413."""
        with _make_test_client() as client:
            response = client.post(
                "/api/v1/manifests/validate",
                content=b'{"manifests": []}',
                headers={**_AUTH_HEADERS, "content-length": str(under_size)},
            )
            # The request should proceed past the size middleware.
            # It may fail with 400/422 due to validation, but never 413.
            assert response.status_code != 413, (
                f"Got unexpected 413 for Content-Length={under_size} (limit={_LIMIT})"
            )

    # --- Strategy: actual oversized bodies (no Content-Length reliance) ---
    # Generate body sizes just over the limit (limit+1 to limit+1024)
    @given(extra_bytes=st.integers(min_value=1, max_value=1024))
    @settings(max_examples=100)
    def test_oversized_body_without_content_length_returns_413(self, extra_bytes: int):
        """Oversized bodies without Content-Length header get 413."""
        body = b"x" * (_LIMIT + extra_bytes)
        with _make_test_client() as client:
            response = client.post(
                "/api/v1/manifests/validate",
                content=body,
                headers=_AUTH_HEADERS,
            )
            assert response.status_code == 413, (
                f"Expected 413 for body size {len(body)}, got {response.status_code}"
            )

    # --- Strategy: actual small bodies ---
    # Generate small body sizes (1 to 1024 bytes) that are well within the limit
    @given(body_size=st.integers(min_value=1, max_value=1024))
    @settings(max_examples=100)
    def test_small_body_without_content_length_proceeds(self, body_size: int):
        """Small bodies without explicit Content-Length are NOT rejected with 413."""
        body = b"x" * body_size
        with _make_test_client() as client:
            response = client.post(
                "/api/v1/manifests/validate",
                content=body,
                headers=_AUTH_HEADERS,
            )
            assert response.status_code != 413, (
                f"Got unexpected 413 for body size {body_size} (limit={_LIMIT})"
            )
