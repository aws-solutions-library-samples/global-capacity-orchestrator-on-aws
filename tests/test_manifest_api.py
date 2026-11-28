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

import hashlib
import hmac
import secrets
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_TEST_SIGNING_KEY = "test-manifest-api-signing-key"  # nosec B105  # test-only key


def _sign_request(request) -> None:
    """Attach a unique HMAC envelope after TestClient serializes the request."""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    raw_target = request.url.raw_path
    target = raw_target.decode("ascii") if isinstance(raw_target, bytes) else str(raw_target)
    body = bytes(request.content)
    content_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(["v1", timestamp, nonce, request.method.upper(), target, content_hash])
    signature = hmac.new(_TEST_SIGNING_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    request.headers.update(
        {
            "x-gco-signature-version": "v1",
            "x-gco-signature": signature,
            "x-gco-timestamp": timestamp,
            "x-gco-nonce": nonce,
            "x-gco-content-sha256": content_hash,
        }
    )


@pytest.fixture(autouse=True)
def _seed_auth_cache(monkeypatch):
    """Seed a fresh signing key and sign every serialized TestClient request."""
    from fastapi.testclient import TestClient

    import gco.services.auth_middleware as auth_module

    original_send = TestClient.send

    def send_with_signature(client, request, *args, **kwargs):
        _sign_request(request)
        return original_send(client, request, *args, **kwargs)

    auth_module.clear_token_cache()
    now = time.monotonic()
    auth_module._cached_tokens = {_TEST_SIGNING_KEY}
    auth_module._cache_timestamp = now
    auth_module._last_successful_refresh = now
    auth_module._last_refresh_attempt = now
    auth_module._secrets_client = None
    monkeypatch.setattr(TestClient, "send", send_with_signature)
    yield
    auth_module.clear_token_cache()
    auth_module._secrets_client = None


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


class TestRequestCorrelation:
    """Every response carries a fresh server-generated X-Request-ID header."""

    def test_responses_carry_unique_request_id_headers(self, mock_manifest_processor):
        import re

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.get("/", headers={})
                second = client.get("/", headers={})

        assert first.status_code == 200
        first_id = first.headers["X-Request-ID"]
        second_id = second.headers["X-Request-ID"]
        assert re.fullmatch(r"[0-9a-f]{32}", first_id)
        assert re.fullmatch(r"[0-9a-f]{32}", second_id)
        # Server-generated per request: two requests never share an id.
        assert first_id != second_id

    def test_client_supplied_header_is_not_trusted(self, mock_manifest_processor):
        """An inbound X-Request-ID never becomes the response id (log-injection surface)."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/", headers={"X-Request-ID": "attacker\ninjected"})

        assert response.headers["X-Request-ID"] != "attacker\ninjected"


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
                response = client.get("/", headers={})
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
                response = client.get("/api/v1/status", headers={})
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
                    headers={},
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
                    headers={},
                )
                assert response.status_code == 400, (
                    f"empty manifests should return 400, got {response.status_code} "
                    f"({response.json()!r})"
                )
                assert "At least one manifest" in response.text

    def test_a_configmap_without_data_reaches_the_processor(self, mock_manifest_processor):
        """A ConfigMap needs no ``data``: ``binaryData`` alone, or nothing at all, is valid.

        The request model used to demand ``spec`` or ``data``, so this route
        answered 400 for manifests the API server accepts, while the validate
        route, which does not build the model, passed them.
        """
        from fastapi.testclient import TestClient

        from gco.models import ManifestSubmissionResponse, ResourceStatus
        from gco.services.manifest_api import app

        configmaps = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "empty"}},
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "blob"},
                "binaryData": {"payload": "AAEC"},
            },
        ]
        mock_manifest_processor.process_manifest_submission = AsyncMock(
            return_value=ManifestSubmissionResponse(
                success=True,
                cluster_id="test-cluster",
                region="us-east-1",
                resources=[
                    ResourceStatus("v1", "ConfigMap", name, "gco-jobs", "created")
                    for name in ("empty", "blob")
                ],
            )
        )
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.ManifestProcessorMetrics"),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/api/v1/manifests", json={"manifests": configmaps, "namespace": "gco-jobs"}
            )

        assert response.status_code == 200, response.text
        (submission,) = mock_manifest_processor.process_manifest_submission.call_args.args
        assert submission.manifests == configmaps


class TestValidateManifestsEndpoint:
    """Tests for POST /api/v1/manifests/validate endpoint."""

    def test_validate_manifests(self, mock_manifest_processor):
        """Request namespace is forwarded to validation and reported consistently."""
        mock_manifest_processor.validate_manifest = MagicMock(return_value=(True, None))
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "test-config"},
        }
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/manifests/validate",
                    json={"manifests": [manifest], "namespace": "team-a"},
                    headers={},
                )

        assert response.status_code == 200
        mock_manifest_processor.validate_manifest.assert_called_once_with(manifest, "team-a")
        assert response.json()["results"][0]["namespace"] == "team-a"


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
                    headers={},
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
                    headers={},
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
                    headers={},
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
                    headers={},
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
                    headers={},
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
                    headers={},
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
                response = client.get("/", headers={})
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
                response = client.get("/api/v1/status", headers={})
                assert response.status_code == 200
                data = response.json()
                assert data["service"] == "GCO Manifest Processor API"
                assert "environment" in data

    def test_status_reports_the_cap_defaults_the_processors_enforce(
        self, mock_manifest_processor, monkeypatch
    ):
        """Regression: /status reported 10 / 32Gi / 4 when the variables were unset."""
        from gco.resource_governance import DEFAULT_MANIFEST_RESOURCE_CAPS

        for name in ("MAX_CPU_PER_MANIFEST", "MAX_MEMORY_PER_MANIFEST", "MAX_GPU_PER_MANIFEST"):
            monkeypatch.delenv(name, raising=False)
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                environment = client.get("/api/v1/status", headers={}).json()["environment"]
        for key in ("max_cpu_per_manifest", "max_memory_per_manifest", "max_gpu_per_manifest"):
            assert environment[key] == str(DEFAULT_MANIFEST_RESOURCE_CAPS[key])


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
                response = client.get("/api/v1/jobs", headers={})
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
                response = client.get("/api/v1/jobs?namespace=default", headers={})
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
                response = client.get("/api/v1/jobs?status=running", headers={})
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
                    headers={},
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
                response = client.get("/api/v1/jobs", headers={})
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
        from datetime import UTC, datetime

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_job.metadata.labels = {"app": "test"}
        mock_job.metadata.annotations = {}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
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
                response = client.get("/api/v1/jobs/default/test-job", headers={})
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
                    headers={},
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
                    headers={},
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
                response = client.get("/api/v1/jobs/default/test-job", headers={})
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
        raw_log_response = MagicMock()
        raw_log_response.data = b"step=1, train_loss=0.3941\nstep=15, train_loss=0.1024\n"
        mock_manifest_processor.core_v1.read_namespaced_pod_log.return_value = raw_log_response

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/logs",
                    headers={},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["job_name"] == "test-job"
                assert data["logs"] == ("step=1, train_loss=0.3941\nstep=15, train_loss=0.1024\n")
                assert data["logs"].count("\n") == 2
                call_kwargs = (
                    mock_manifest_processor.core_v1.read_namespaced_pod_log.call_args.kwargs
                )
                assert call_kwargs["_preload_content"] is False
                raw_log_response.release_conn.assert_called_once_with()

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
                    headers={},
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
                    headers={},
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
                    headers={},
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
                    headers={},
                )
                assert response.status_code == 500


class TestDecodePodLogResponse:
    """Branch coverage for the raw Kubernetes log-body decoder.

    The endpoint tests pin the happy path (bytes body decoded, connection
    released); these pin the decoder's contract directly: str/bytes handling,
    lossy decode of invalid UTF-8, fail-loud unsupported payloads, and the
    guarantee that the urllib3 connection is released on every path.
    """

    def _response(self, data):
        response = MagicMock()
        response.data = data
        return response

    def test_bytes_body_decodes_with_real_newlines(self):
        from gco.services.api_routes.jobs import _decode_pod_log_response

        response = self._response(b"step=1\nstep=2\n")
        assert _decode_pod_log_response(response) == "step=1\nstep=2\n"
        response.release_conn.assert_called_once_with()

    def test_bytearray_body_decodes(self):
        from gco.services.api_routes.jobs import _decode_pod_log_response

        response = self._response(bytearray(b"lines"))
        assert _decode_pod_log_response(response) == "lines"
        response.release_conn.assert_called_once_with()

    def test_str_body_passes_through(self):
        from gco.services.api_routes.jobs import _decode_pod_log_response

        response = self._response("already text\n")
        assert _decode_pod_log_response(response) == "already text\n"
        response.release_conn.assert_called_once_with()

    def test_invalid_utf8_decodes_lossily_instead_of_raising(self):
        from gco.services.api_routes.jobs import _decode_pod_log_response

        response = self._response(b"\xff\xfe ok")
        assert _decode_pod_log_response(response) == "\ufffd\ufffd ok"
        response.release_conn.assert_called_once_with()

    def test_unsupported_payload_raises_but_still_releases_connection(self):
        """A contract break must not leak the urllib3 connection."""
        from gco.services.api_routes.jobs import _decode_pod_log_response

        response = self._response(12345)
        with pytest.raises(TypeError, match="unsupported pod log payload"):
            _decode_pod_log_response(response)
        response.release_conn.assert_called_once_with()

    def test_raw_bytes_without_response_wrapper_decode(self):
        """Defensive path: a client returning the body directly still works."""
        from gco.services.api_routes.jobs import _decode_pod_log_response

        assert _decode_pod_log_response(b"bare bytes\n") == "bare bytes\n"

    def test_non_callable_release_conn_is_ignored(self):
        from gco.services.api_routes.jobs import _decode_pod_log_response

        response = MagicMock()
        response.data = b"ok"
        response.release_conn = "not-callable"
        assert _decode_pod_log_response(response) == "ok"

    def test_job_logs_endpoint_decodes_invalid_utf8_lossily(self, mock_manifest_processor):
        """End to end: a pod emitting non-UTF-8 bytes still yields 200 + text."""
        mock_container = MagicMock()
        mock_container.name = "main"
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.init_containers = []
        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods

        raw_log_response = MagicMock()
        raw_log_response.data = b"binary \xff garbage\nreadable line\n"
        mock_manifest_processor.core_v1.read_namespaced_pod_log.return_value = raw_log_response

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/logs",
                    headers={},
                )
                assert response.status_code == 200
                assert response.json()["logs"] == "binary \ufffd garbage\nreadable line\n"
                raw_log_response.release_conn.assert_called_once_with()


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
                response = client.delete("/api/v1/jobs/default/test-job", headers={})
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "deleted"
                assert data["job_name"] == "test-job"

    def test_delete_job_with_expected_uid_uses_kubernetes_precondition(
        self, mock_manifest_processor
    ):
        """Expected UID is re-read and passed atomically to Kubernetes delete."""
        mock_manifest_processor.batch_v1 = MagicMock()
        current = MagicMock()
        current.metadata.uid = "uid-123"
        mock_manifest_processor.batch_v1.read_namespaced_job.return_value = current

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/default/test-job?expected_uid=uid-123",
                    headers={},
                )

        assert response.status_code == 200
        assert response.json()["uid"] == "uid-123"
        mock_manifest_processor.batch_v1.read_namespaced_job.assert_called_once_with(
            name="test-job",
            namespace="default",
        )
        delete_kwargs = mock_manifest_processor.batch_v1.delete_namespaced_job.call_args.kwargs
        assert delete_kwargs["name"] == "test-job"
        assert delete_kwargs["namespace"] == "default"
        assert delete_kwargs["body"].propagation_policy == "Background"
        assert delete_kwargs["body"].preconditions.uid == "uid-123"

    def test_delete_job_rejects_changed_uid(self, mock_manifest_processor):
        """A same-name replacement cannot satisfy an older deletion authority."""
        mock_manifest_processor.batch_v1 = MagicMock()
        current = MagicMock()
        current.metadata.uid = "replacement-uid"
        mock_manifest_processor.batch_v1.read_namespaced_job.return_value = current

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/default/test-job?expected_uid=original-uid",
                    headers={},
                )

        assert response.status_code == 409
        mock_manifest_processor.batch_v1.delete_namespaced_job.assert_not_called()

    def test_delete_job_maps_kubernetes_uid_conflict(self, mock_manifest_processor):
        """A race after the UID read remains a conflict, not a generic 500."""
        mock_manifest_processor.batch_v1 = MagicMock()
        current = MagicMock()
        current.metadata.uid = "uid-123"
        mock_manifest_processor.batch_v1.read_namespaced_job.return_value = current
        conflict = RuntimeError("precondition failed")
        conflict.status = 409
        mock_manifest_processor.batch_v1.delete_namespaced_job.side_effect = conflict

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/jobs/default/test-job?expected_uid=uid-123",
                    headers={},
                )

        assert response.status_code == 409

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
                    headers={},
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
                    headers={},
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
                response = client.delete("/api/v1/jobs/default/test-job", headers={})
                assert response.status_code == 500


class TestParseJobToDict:
    """Tests for _parse_job_to_dict helper function."""

    def test_parse_job_to_dict_completed(self):
        """Test parsing a completed job."""
        from datetime import UTC, datetime

        from gco.services.api_shared import _parse_job_to_dict

        mock_job = MagicMock()
        mock_job.metadata.name = "test-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_job.metadata.labels = {"app": "test"}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 0
        mock_job.status.succeeded = 1
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_job.status.completion_time = datetime(2024, 1, 1, 0, 5, 0, tzinfo=UTC)

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
        from datetime import UTC, datetime

        from gco.services.api_shared import _parse_job_to_dict

        mock_job = MagicMock()
        mock_job.metadata.name = "failed-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 0
        mock_job.status.succeeded = 0
        mock_job.status.failed = 1
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
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
        from datetime import UTC, datetime

        from gco.services.api_shared import _parse_job_to_dict

        mock_job = MagicMock()
        mock_job.metadata.name = "running-job"
        mock_job.metadata.namespace = "default"
        mock_job.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_job.metadata.labels = {}
        mock_job.metadata.uid = "test-uid"
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 6
        mock_job.status.active = 1
        mock_job.status.succeeded = 0
        mock_job.status.failed = 0
        mock_job.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_job.status.completion_time = None
        mock_job.status.conditions = []

        result = _parse_job_to_dict(mock_job)

        assert result["metadata"]["name"] == "running-job"
        assert result["status"]["active"] == 1


def _on_event_loop() -> bool:
    """True only on the event loop's own thread; worker threads have no running loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TestKubernetesCallsRunOffTheEventLoop:
    """No route calls the synchronous Kubernetes client on the event loop.

    A blocking call there stalls every request the worker is serving, the
    health probes included, for as long as the API server takes to answer.
    The processor here is real; its client methods record whether they ran with
    an event loop in their thread, which is true only on the loop itself, and
    each route is driven through every client call it makes.
    """

    SANDBOXED_JOB = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "sandboxed", "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    # Makes validation read the RuntimeClass from the cluster.
                    "runtimeClassName": "isolated-sandbox",
                    "restartPolicy": "Never",
                    "containers": [{"name": "main", "image": "python:3.14"}],
                }
            }
        },
    }

    @pytest.fixture
    def harness(self):
        import json
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from fastapi.testclient import TestClient
        from kubernetes import client as k8s
        from kubernetes.client.rest import ApiException

        from gco.services.manifest_api import app
        from gco.services.manifest_processor import ManifestProcessor

        calls: list[tuple[str, bool]] = []

        def responds(label, answer):
            def side_effect(*args, **kwargs):
                calls.append((label, _on_event_loop()))
                return answer(*args, **kwargs)

            return side_effect

        created = datetime(2026, 1, 1, tzinfo=UTC)
        containers = [k8s.V1Container(name="main", image="python:3.14")]
        job = k8s.V1Job(
            metadata=k8s.V1ObjectMeta(
                name="train", namespace="gco-jobs", uid="job-uid", creation_timestamp=created
            ),
            spec=k8s.V1JobSpec(
                template=k8s.V1PodTemplateSpec(spec=k8s.V1PodSpec(containers=containers))
            ),
            status=k8s.V1JobStatus(active=1),
        )
        job_json = json.dumps(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "train", "namespace": "gco-jobs", "uid": "job-uid"},
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{"name": "main", "image": "python:3.14"}],
                        }
                    }
                },
            }
        ).encode()
        pod = k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(
                name="train-abcde",
                namespace="gco-jobs",
                labels={"job-name": "train"},
                creation_timestamp=created,
            ),
            spec=k8s.V1PodSpec(node_name="node-1", containers=containers),
            status=k8s.V1PodStatus(phase="Running"),
        )
        node = k8s.V1Node(metadata=k8s.V1ObjectMeta(name="node-1", labels={}))

        def log_response(*_args, **_kwargs):
            return SimpleNamespace(data=b"step=1\n", release_conn=lambda: None)

        with patch("gco.services.manifest_processor.config"):
            processor = ManifestProcessor("test-cluster", "us-east-1", {})
        processor.core_v1 = MagicMock()
        processor.batch_v1 = MagicMock()
        processor.custom_objects = MagicMock()
        processor.node_v1 = MagicMock()
        processor.admission_v1 = MagicMock()
        empty = SimpleNamespace(items=[])
        gpu_cap_policy = SimpleNamespace(
            metadata=SimpleNamespace(annotations={"gco.io/container-max-gpu": "8"})
        )
        for api, method, answer in (
            ("core_v1", "list_namespace", lambda *a, **k: empty),
            ("core_v1", "list_namespaced_pod", lambda *a, **k: SimpleNamespace(items=[pod])),
            ("core_v1", "read_namespaced_pod", lambda *a, **k: pod),
            ("core_v1", "read_node", lambda *a, **k: node),
            ("core_v1", "list_namespaced_event", lambda *a, **k: empty),
            ("core_v1", "read_namespaced_pod_log", log_response),
            ("core_v1", "list_namespaced_resource_quota", lambda *a, **k: empty),
            ("core_v1", "list_namespaced_limit_range", lambda *a, **k: empty),
            (
                "batch_v1",
                "read_namespaced_job",
                lambda *a, **k: (
                    SimpleNamespace(data=job_json, release_conn=lambda: None)
                    if k.get("_preload_content") is False
                    else job
                ),
            ),
            ("batch_v1", "list_namespaced_job", lambda *a, **k: SimpleNamespace(items=[job])),
            ("batch_v1", "delete_namespaced_job", lambda *a, **k: None),
            ("custom_objects", "list_cluster_custom_object", lambda *a, **k: {"items": []}),
            (
                "custom_objects",
                "get_namespaced_custom_object",
                lambda *a, **k: {
                    "containers": [{"name": "main", "usage": {"cpu": "1m", "memory": "1Mi"}}]
                },
            ),
            ("node_v1", "read_runtime_class", lambda *a, **k: object()),
            (
                "admission_v1",
                "read_validating_admission_policy",
                lambda *a, **k: gpu_cap_policy,
            ),
        ):
            getattr(getattr(processor, api), method).side_effect = responds(
                f"{api}.{method}", answer
            )

        def missing(*_args, **_kwargs):
            raise ApiException(status=404)

        api_resource = MagicMock(namespaced=True)
        api_resource.get.side_effect = responds("dynamic.get", missing)
        api_resource.create.side_effect = responds("dynamic.create", lambda *a, **k: None)
        api_resource.delete.side_effect = responds("dynamic.delete", lambda *a, **k: None)
        processor._get_api_resource = MagicMock(
            side_effect=responds("dynamic.discover", lambda *a, **k: api_resource)
        )

        template_store = MagicMock()
        template_store.get_template.return_value = {
            "name": "tpl",
            "manifest": json.loads(job_json),
            "parameters": {},
        }
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=processor,
            ),
            patch("gco.services.manifest_api.ManifestProcessorMetrics"),
            patch("gco.services.manifest_api.get_template_store", return_value=template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
            TestClient(app, raise_server_exceptions=False) as http,
        ):
            yield http, calls

    @pytest.mark.parametrize(
        ("method", "path", "body", "status", "expected_calls"),
        [
            ("GET", "/api/v1/jobs", None, 200, {"batch_v1.list_namespaced_job"}),
            (
                "GET",
                "/api/v1/jobs/gco-jobs/train",
                None,
                200,
                {
                    "batch_v1.read_namespaced_job",
                    "core_v1.list_namespaced_pod",
                    "core_v1.read_node",
                },
            ),
            (
                "GET",
                "/api/v1/jobs/gco-jobs/train/logs",
                None,
                200,
                {
                    "batch_v1.read_namespaced_job",
                    "core_v1.list_namespaced_pod",
                    "core_v1.read_namespaced_pod_log",
                },
            ),
            (
                "GET",
                "/api/v1/jobs/gco-jobs/train/events",
                None,
                200,
                {
                    "core_v1.list_namespaced_event",
                    "core_v1.list_namespaced_pod",
                    "batch_v1.read_namespaced_job",
                    "custom_objects.list_cluster_custom_object",
                },
            ),
            (
                "GET",
                "/api/v1/jobs/gco-jobs/train/pods",
                None,
                200,
                {"core_v1.list_namespaced_pod", "core_v1.read_node"},
            ),
            (
                "GET",
                "/api/v1/jobs/gco-jobs/train/pods/train-abcde/logs",
                None,
                200,
                {"core_v1.read_namespaced_pod", "core_v1.read_namespaced_pod_log"},
            ),
            (
                "GET",
                "/api/v1/jobs/gco-jobs/train/metrics",
                None,
                200,
                {"core_v1.list_namespaced_pod", "custom_objects.get_namespaced_custom_object"},
            ),
            (
                "DELETE",
                "/api/v1/jobs/gco-jobs/train?expected_uid=job-uid",
                None,
                200,
                {"batch_v1.read_namespaced_job", "batch_v1.delete_namespaced_job"},
            ),
            (
                "DELETE",
                "/api/v1/jobs",
                {"namespace": "gco-jobs"},
                200,
                {"batch_v1.list_namespaced_job", "batch_v1.delete_namespaced_job"},
            ),
            (
                "POST",
                "/api/v1/jobs/gco-jobs/train/retry",
                None,
                201,
                {"batch_v1.read_namespaced_job", "dynamic.discover", "dynamic.create"},
            ),
            (
                "POST",
                "/api/v1/jobs/from-template/tpl",
                {"name": "from-tpl", "namespace": "gco-jobs"},
                201,
                {"dynamic.discover", "dynamic.get", "dynamic.create"},
            ),
            (
                "POST",
                "/api/v1/manifests",
                {"manifests": [SANDBOXED_JOB]},
                200,
                {"node_v1.read_runtime_class", "dynamic.discover", "dynamic.get", "dynamic.create"},
            ),
            (
                "POST",
                "/api/v1/manifests/validate",
                {"manifests": [SANDBOXED_JOB]},
                200,
                {"node_v1.read_runtime_class"},
            ),
            (
                "GET",
                "/api/v1/manifests/gco-jobs/settings?api_version=v1&kind=ConfigMap",
                None,
                404,
                {"dynamic.discover", "dynamic.get"},
            ),
            (
                "DELETE",
                "/api/v1/manifests/gco-jobs/settings?api_version=v1&kind=ConfigMap",
                None,
                200,
                {"dynamic.discover", "dynamic.delete"},
            ),
            ("GET", "/api/v1/health", None, 200, {"core_v1.list_namespace"}),
            (
                "GET",
                "/api/v1/policy",
                None,
                200,
                {
                    "core_v1.list_namespaced_resource_quota",
                    "core_v1.list_namespaced_limit_range",
                    "admission_v1.read_validating_admission_policy",
                },
            ),
        ],
    )
    def test_route_makes_its_kubernetes_calls_in_worker_threads(
        self, harness, method, path, body, status, expected_calls
    ):
        http, calls = harness
        response = http.request(method, path, json=body)

        assert response.status_code == status, response.text
        assert expected_calls <= {label for label, _ in calls}
        assert sorted({label for label, on_loop in calls if on_loop}) == []
