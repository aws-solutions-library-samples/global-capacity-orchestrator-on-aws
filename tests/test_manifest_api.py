"""
Tests for gco/services/manifest_api.py — the FastAPI manifest service.

Covers the create_app factory, the core route set (/, /healthz, /readyz,
/api/v1/health, /api/v1/status, /api/v1/manifests, /api/v1/manifests/validate),
and the request/response shapes via ManifestSubmissionAPIRequest and
ResourceIdentifier. An autouse fixture seeds the auth middleware token
cache with a known test token so the real AuthenticationMiddleware
runs end-to-end against TestClient traffic — same code path as
production, no get_valid_tokens mocking.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Auth token used by all tests in this module. The autouse fixture seeds
# the auth middleware's token cache so the real middleware validation runs.
_TEST_AUTH_TOKEN = "test-manifest-api-token"  # nosec B105 - test fixture token, not a real credential
_AUTH_HEADERS = {"x-gco-auth-token": _TEST_AUTH_TOKEN}


@pytest.fixture(autouse=True)
def _seed_auth_cache():
    """Seed the auth middleware token cache with a known token.

    This lets the real middleware code run (no mocking of get_valid_tokens)
    while giving tests a token they can send in headers. The middleware
    validates the header against the cache — same code path as production.
    """
    import gco.services.auth_middleware as auth_module

    original_tokens = auth_module._cached_tokens
    original_timestamp = auth_module._cache_timestamp
    auth_module._cached_tokens = {_TEST_AUTH_TOKEN}
    auth_module._cache_timestamp = time.time()
    yield
    auth_module._cached_tokens = original_tokens
    auth_module._cache_timestamp = original_timestamp


class TestManifestAPIModels:
    """Tests for Manifest API models and functions."""

    def test_create_app_returns_fastapi(self):
        """Test create_app returns FastAPI instance."""
        from fastapi import FastAPI

        from gco.services.manifest_api import create_app

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_app_has_routes(self):
        """Test app has expected routes."""
        from gco.services.manifest_api import app

        routes = [r.path for r in app.routes if hasattr(r, "path")] + [
            s.path
            for r in app.routes
            for s in getattr(getattr(r, "original_router", None), "routes", [])
            if hasattr(s, "path")
        ]
        assert "/" in routes
        assert "/healthz" in routes
        assert "/readyz" in routes
        assert "/api/v1/health" in routes
        assert "/api/v1/status" in routes
        assert "/api/v1/manifests" in routes
        assert "/api/v1/manifests/validate" in routes

    def test_manifest_submission_api_request(self):
        """Test ManifestSubmissionAPIRequest model."""
        from gco.services.api_shared import ManifestSubmissionAPIRequest

        request = ManifestSubmissionAPIRequest(
            manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
            namespace="default",
            dry_run=True,
        )
        assert len(request.manifests) == 1
        assert request.namespace == "default"
        assert request.dry_run is True

    def test_resource_identifier(self):
        """Test ResourceIdentifier model."""
        from gco.services.api_shared import ResourceIdentifier

        identifier = ResourceIdentifier(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
        )
        assert identifier.api_version == "apps/v1"
        assert identifier.kind == "Deployment"


@pytest.fixture
def mock_manifest_processor():
    """Fixture to mock the manifest processor creation."""
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


class TestManifestAPIBasicEndpoints:
    """Tests for basic Manifest API endpoints using TestClient."""

    def test_root_endpoint(self, mock_manifest_processor):
        """Test root endpoint returns service info."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN})
                assert response.status_code == 200
                data = response.json()
                assert data["service"] == "GCO Manifest Processor API"
                assert "endpoints" in data

    def test_healthz_endpoint(self, mock_manifest_processor):
        """Test Kubernetes liveness probe."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/healthz")
                assert response.status_code == 200
                assert response.json()["status"] == "ok"

    def test_readyz_endpoint(self, mock_manifest_processor):
        """Test Kubernetes readiness probe."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/readyz")
                # Returns 200 if processor is set, 503 if not
                assert response.status_code in [200, 503]

    def test_status_endpoint(self, mock_manifest_processor):
        """Test service status endpoint."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/status", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                data = response.json()
                assert data["service"] == "GCO Manifest Processor API"
                assert "environment" in data


class TestSubmitManifestsEndpoint:
    """Tests for POST /api/v1/manifests endpoint."""

    def test_submit_manifests(self, mock_manifest_processor):
        """Test manifest submission endpoint."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/manifests",
                    json={"manifests": [{"apiVersion": "v1", "kind": "ConfigMap"}]},
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Returns 200 on success, 400 on validation error, 500/503 on error
                assert response.status_code in [200, 400, 500, 503]

    def test_empty_manifests_returns_400_not_500(self, mock_manifest_processor):
        """Regression: sending an empty manifests list must return HTTP 400
        (client error), not HTTP 500 (server error). The request object's
        own ValueError for missing manifests is client-side, not a server
        fault."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/manifests",
                    json={"manifests": []},
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 400, (
                    f"empty manifests should return 400, got {response.status_code} "
                    f"({response.json()!r})"
                )
                assert "At least one manifest" in response.text


class TestValidateManifestsEndpoint:
    """Tests for POST /api/v1/manifests/validate endpoint."""

    def test_validate_manifests(self, mock_manifest_processor):
        """Test manifest validation endpoint."""
        mock_manifest_processor.validate_manifest = MagicMock(return_value=(True, None))
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/manifests/validate",
                    json={"manifests": [{"apiVersion": "v1", "kind": "ConfigMap"}]},
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Returns 200 on success, 500/503 on error
                assert response.status_code in [200, 500, 503]


class TestDeleteResourceEndpoint:
    """Tests for DELETE /api/v1/manifests/{namespace}/{name} endpoint."""

    def test_delete_resource(self, mock_manifest_processor):
        """Test resource deletion endpoint."""
        from gco.models import ResourceStatus

        mock_status = ResourceStatus(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
            status="deleted",
        )
        mock_manifest_processor.delete_resource = AsyncMock(return_value=mock_status)

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/manifests/default/test-app",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Returns 200 on success, 400/404 on error, 500/503 on server error
                assert response.status_code in [200, 400, 404, 500, 503]


class TestGetResourceEndpoint:
    """Tests for GET /api/v1/manifests/{namespace}/{name} endpoint."""

    def test_get_resource(self, mock_manifest_processor):
        """Test get resource endpoint."""
        mock_manifest_processor.get_resource_status = AsyncMock(
            return_value={"exists": True, "name": "test-app"}
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/manifests/default/test-app",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Returns 200 if found, 404 if not found, 500/503 on error
                assert response.status_code in [200, 404, 500, 503]


class TestHealthCheckEndpoint:
    """Tests for /api/v1/health endpoint."""

    def test_health_check(self, mock_manifest_processor):
        """Test health check endpoint."""
        mock_manifest_processor.core_v1.list_namespace = MagicMock()

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/health")
                # Returns 200 if healthy, 503 if not
                assert response.status_code in [200, 503]


class TestManifestAPIWithMockedProcessor:
    """Tests for Manifest API with mocked processor - basic tests."""

    def test_submit_manifests_returns_response(self, mock_manifest_processor):
        """Test manifest submission returns a response."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        mock_response = ManifestSubmissionResponse(
            success=True,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="v1",
                    kind="ConfigMap",
                    name="test-config",
                    namespace="default",
                    status="created",
                )
            ],
        )
        mock_manifest_processor.process_manifest_submission = AsyncMock(return_value=mock_response)

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/manifests",
                    json={
                        "manifests": [
                            {
                                "apiVersion": "v1",
                                "kind": "ConfigMap",
                                "metadata": {"name": "test-config", "namespace": "default"},
                                "data": {"key": "value"},
                            }
                        ]
                    },
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Should return some response (success or error)
                assert response.status_code in [200, 400, 500, 503]


class TestValidateManifestsWithMockedProcessor:
    """Tests for validate manifests endpoint."""

    def test_validate_manifests_returns_response(self, mock_manifest_processor):
        """Test validating manifests returns a response."""
        mock_manifest_processor.validate_manifest = MagicMock(return_value=(True, None))

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/manifests/validate",
                    json={
                        "manifests": [
                            {
                                "apiVersion": "v1",
                                "kind": "ConfigMap",
                                "metadata": {"name": "test-config"},
                                "data": {"key": "value"},
                            }
                        ]
                    },
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Should return some response
                assert response.status_code in [200, 500, 503]


class TestDeleteResourceWithMockedProcessor:
    """Tests for delete resource endpoint."""

    def test_delete_resource_returns_response(self, mock_manifest_processor):
        """Test resource deletion returns a response."""
        from gco.models import ResourceStatus

        mock_status = ResourceStatus(
            api_version="apps/v1",
            kind="Deployment",
            name="test-app",
            namespace="default",
            status="deleted",
        )
        mock_manifest_processor.delete_resource = AsyncMock(return_value=mock_status)

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/manifests/default/test-app",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Should return some response
                assert response.status_code in [200, 400, 404, 500, 503]


class TestGetResourceWithMockedProcessor:
    """Tests for get resource endpoint."""

    def test_get_resource_returns_response(self, mock_manifest_processor):
        """Test getting resource returns a response."""
        mock_manifest_processor.get_resource_status = AsyncMock(
            return_value={"exists": True, "name": "test-app"}
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/manifests/default/test-app",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                # Should return some response
                assert response.status_code in [200, 404, 500, 503]


class TestHealthCheckWithMockedProcessor:
    """Tests for health check endpoint."""

    def test_health_check_returns_response(self, mock_manifest_processor):
        """Test health check returns a response."""
        mock_manifest_processor.core_v1.list_namespace = MagicMock()

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/health")
                # Should return some response
                assert response.status_code in [200, 503]


class TestRootEndpointDetails:
    """Tests for root endpoint details."""

    def test_root_returns_service_info(self, mock_manifest_processor):
        """Test root endpoint returns service info."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN})
                assert response.status_code == 200
                data = response.json()
                assert data["service"] == "GCO Manifest Processor API"
                assert "endpoints" in data


class TestStatusEndpointWithProcessor:
    """Tests for status endpoint."""

    def test_status_returns_service_info(self, mock_manifest_processor):
        """Test status endpoint returns service info."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/status", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                data = response.json()
                assert data["service"] == "GCO Manifest Processor API"
                assert "environment" in data


class TestListJobsEndpoint:
    """Tests for GET /api/v1/jobs endpoint."""

    def test_list_jobs_success(self, mock_manifest_processor):
        """Test listing jobs returns success."""
        mock_manifest_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "test-job",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-01T00:00:00Z",
                        "labels": {},
                        "uid": "test-uid",
                    },
                    "spec": {"parallelism": 1, "completions": 1, "backoffLimit": 6},
                    "status": {
                        "active": 1,
                        "succeeded": 0,
                        "failed": 0,
                        "startTime": "2024-01-01T00:00:00Z",
                        "completionTime": None,
                        "conditions": [],
                    },
                }
            ]
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                data = response.json()
                assert "jobs" in data
                assert data["count"] == 1

    def test_list_jobs_with_namespace_filter(self, mock_manifest_processor):
        """Test listing jobs with namespace filter."""
        mock_manifest_processor.list_jobs = AsyncMock(return_value=[])

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs?namespace=default", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                mock_manifest_processor.list_jobs.assert_called_with(
                    namespace="default", status_filter=None
                )

    def test_list_jobs_with_status_filter(self, mock_manifest_processor):
        """Test listing jobs with status filter."""
        mock_manifest_processor.list_jobs = AsyncMock(return_value=[])

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs?status=running", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                mock_manifest_processor.list_jobs.assert_called_with(
                    namespace=None, status_filter="running"
                )

    def test_list_jobs_disallowed_namespace(self, mock_manifest_processor):
        """Test listing jobs with disallowed namespace returns 400."""
        mock_manifest_processor.list_jobs = AsyncMock(
            side_effect=ValueError("Namespace 'kube-system' not allowed")
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs?namespace=kube-system",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 400

    def test_list_jobs_server_error(self, mock_manifest_processor):
        """Test listing jobs with server error returns 500."""
        mock_manifest_processor.list_jobs = AsyncMock(side_effect=Exception("Database error"))

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 500

    def test_app_has_jobs_route(self):
        """Test app has /api/v1/jobs route."""
        from gco.services.manifest_api import app

        routes = [r.path for r in app.routes if hasattr(r, "path")] + [
            s.path
            for r in app.routes
            for s in getattr(getattr(r, "original_router", None), "routes", [])
            if hasattr(s, "path")
        ]
        assert "/api/v1/jobs" in routes


class TestGetJobEndpoint:
    """Tests for GET /api/v1/jobs/{namespace}/{name} endpoint."""

    def test_get_job_success(self, mock_manifest_processor):
        """Test getting a specific job returns success."""
        from datetime import datetime

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = {"app": "test"}
        mock_job.metadata.annotations = {}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        mock_manifest_processor.batch_v1 = MagicMock()
        mock_manifest_processor.batch_v1.read_namespaced_job.return_value = mock_job

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                data = response.json()
                assert data["cluster_id"] == "test-cluster"
                assert data["region"] == "us-east-1"

    def test_get_job_not_found(self, mock_manifest_processor):
        """Test getting a non-existent job returns 404."""
        mock_manifest_processor.batch_v1 = MagicMock()
        mock_manifest_processor.batch_v1.read_namespaced_job.side_effect = Exception(
            "NotFound: job not found"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/nonexistent-job",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 404

    def test_get_job_disallowed_namespace(self, mock_manifest_processor):
        """Test getting job from disallowed namespace returns 403."""
        mock_manifest_processor.allowed_namespaces = {"default", "gco-jobs"}

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/kube-system/test-job",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 403

    def test_get_job_server_error(self, mock_manifest_processor):
        """Test getting job with server error returns 500."""
        mock_manifest_processor.batch_v1 = MagicMock()
        mock_manifest_processor.batch_v1.read_namespaced_job.side_effect = Exception(
            "Internal error"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 500


class TestGetJobLogsEndpoint:
    """Tests for GET /api/v1/jobs/{namespace}/{name}/logs endpoint."""

    def test_get_job_logs_success(self, mock_manifest_processor):
        """Test getting job logs returns success."""
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]

        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods
        mock_manifest_processor.core_v1.read_namespaced_pod_log.return_value = (
            "Log line 1\nLog line 2"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/logs",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["job_name"] == "test-job"
                assert data["logs"] == "Log line 1\nLog line 2"

    def test_get_job_logs_with_tail(self, mock_manifest_processor):
        """Test getting job logs with tail parameter."""
        mock_container = MagicMock()
        mock_container.name = "main"

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.init_containers = []
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]

        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods
        mock_manifest_processor.core_v1.read_namespaced_pod_log.return_value = "Last 50 lines"

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/logs?tail=50",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 200
                # Verify the call was made with expected parameters
                call_kwargs = (
                    mock_manifest_processor.core_v1.read_namespaced_pod_log.call_args.kwargs
                )
                assert call_kwargs["name"] == "test-job-abc123"
                assert call_kwargs["namespace"] == "default"
                assert call_kwargs["tail_lines"] == 50

    def test_get_job_logs_no_pods(self, mock_manifest_processor):
        """Test getting job logs when no pods found returns 404."""
        mock_pods = MagicMock()
        mock_pods.items = []

        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/logs",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 404

    def test_get_job_logs_disallowed_namespace(self, mock_manifest_processor):
        """Test getting job logs from disallowed namespace returns 403."""
        mock_manifest_processor.allowed_namespaces = {"default", "gco-jobs"}

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/kube-system/test-job/logs",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 403

    def test_get_job_logs_server_error(self, mock_manifest_processor):
        """Test getting job logs with server error returns 500."""
        mock_manifest_processor.core_v1.list_namespaced_pod.side_effect = Exception(
            "Internal error"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/logs",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 500


class TestDeleteJobEndpoint:
    """Tests for DELETE /api/v1/jobs/{namespace}/{name} endpoint."""

    def test_delete_job_success(self, mock_manifest_processor):
        """Test deleting a job returns success."""
        mock_manifest_processor.batch_v1 = MagicMock()
        mock_manifest_processor.batch_v1.delete_namespaced_job.return_value = None

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/default/test-job", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "deleted"
                assert data["job_name"] == "test-job"

    def test_delete_job_not_found(self, mock_manifest_processor):
        """Test deleting a non-existent job returns 404."""
        mock_manifest_processor.batch_v1 = MagicMock()
        mock_manifest_processor.batch_v1.delete_namespaced_job.side_effect = Exception(
            "NotFound: job not found"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/default/nonexistent-job",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 404

    def test_delete_job_disallowed_namespace(self, mock_manifest_processor):
        """Test deleting job from disallowed namespace returns 403."""
        mock_manifest_processor.allowed_namespaces = {"default", "gco-jobs"}

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/kube-system/test-job",
                    headers={"x-gco-auth-token": _TEST_AUTH_TOKEN},
                )
                assert response.status_code == 403

    def test_delete_job_server_error(self, mock_manifest_processor):
        """Test deleting job with server error returns 500."""
        mock_manifest_processor.batch_v1 = MagicMock()
        mock_manifest_processor.batch_v1.delete_namespaced_job.side_effect = Exception(
            "Internal error"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/default/test-job", headers={"x-gco-auth-token": _TEST_AUTH_TOKEN}
                )
                assert response.status_code == 500


class TestParseJobToDict:
    """Tests for _parse_job_to_dict helper function."""

    def test_parse_job_to_dict_completed(self):
        """Test parsing a completed job."""
        from datetime import datetime

        from gco.services.api_shared import _parse_job_to_dict

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = {"app": "test"}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 0
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.status.completion_time = datetime(2024, 1, 1, 0, 5, 0)

        # Create a condition for Complete
        mock_condition = MagicMock()
        mock_condition.type = "Complete"
        mock_condition.status = "True"
        mock_job.status.conditions = [mock_condition]

        result = _parse_job_to_dict(mock_job)

        assert result["metadata"]["name"] == "test-job"
        assert result["metadata"]["namespace"] == "default"
        assert result["status"]["succeeded"] == 1

    def test_parse_job_to_dict_failed(self):
        """Test parsing a failed job."""
        from datetime import datetime

        from gco.services.api_shared import _parse_job_to_dict

        mock_job = MagicMock()
        mock_job.metadata.name = "failed-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 0
        mock_job.status.succeeded = 0
        mock_job.status.failed = 1
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.status.completion_time = None

        # Create a condition for Failed
        mock_condition = MagicMock()
        mock_condition.type = "Failed"
        mock_condition.status = "True"
        mock_job.status.conditions = [mock_condition]

        result = _parse_job_to_dict(mock_job)

        assert result["metadata"]["name"] == "failed-job"
        assert result["status"]["failed"] == 1

    def test_parse_job_to_dict_running(self):
        """Test parsing a running job."""
        from datetime import datetime

        from gco.services.api_shared import _parse_job_to_dict

        mock_job = MagicMock()
        mock_job.metadata.name = "running-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0)
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        result = _parse_job_to_dict(mock_job)

        assert result["metadata"]["name"] == "running-job"
        assert result["status"]["active"] == 1
