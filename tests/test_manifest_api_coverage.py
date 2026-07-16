"""
Coverage-focused tests for gco/services/manifest_api.py.

Targets edge-case branches the main manifest-api suites don't hit:
the health endpoint returning 503 when the Kubernetes API is
unreachable (list_namespace raises), job metrics when pod-metrics
retrieval errors out, and similar error-path branches. Authentication
is bypassed explicitly because its cryptographic behavior has dedicated tests.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests._auth import bypass_backend_auth

# Call sites retain a common header mapping for concise request construction;
# authentication itself is handled by the autouse fixture below.
_AUTH_HEADERS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _bypass_authentication():
    """Isolate route behavior from the separately tested HMAC verifier."""
    with bypass_backend_auth():
        yield


@pytest.fixture
def mock_manifest_processor():
    """Fixture to mock the manifest processor."""
    mock_processor = MagicMock()
    mock_processor.cluster_id = "test-cluster"
    mock_processor.region = "us-east-1"
    mock_processor.core_v1 = MagicMock()
    mock_processor.batch_v1 = MagicMock()
    mock_processor.custom_objects = MagicMock()
    mock_processor.max_cpu_per_manifest = 10000
    mock_processor.max_memory_per_manifest = 34359738368
    mock_processor.max_gpu_per_manifest = 4
    mock_processor.allowed_namespaces = {"default", "gco-jobs"}
    mock_processor.validation_enabled = True
    return mock_processor


# =============================================================================
# Health Check Edge Cases
# =============================================================================


class TestHealthCheckEdgeCases:
    """Tests for health check edge cases."""

    def test_health_check_kubernetes_disconnected(self, mock_manifest_processor):
        """Test health check when Kubernetes API is disconnected."""
        # Configure the mock to raise an exception when list_namespace is called
        mock_manifest_processor.core_v1.list_namespace.side_effect = Exception("Connection refused")

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

            # Also directly set the module variable to ensure it's our mock
            api_module.manifest_processor = mock_manifest_processor

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                # Use the correct endpoint path: /api/v1/health
                response = client.get("/api/v1/health", headers=_AUTH_HEADERS)
                assert response.status_code == 503
                data = response.json()
                assert data["status"] == "unhealthy"
                assert data["kubernetes_api"] == "disconnected"


# =============================================================================
# Job Metrics Edge Cases
# =============================================================================


class TestJobMetricsEdgeCases:
    """Tests for job metrics edge cases."""

    def test_get_job_metrics_pod_metrics_error(self, mock_manifest_processor):
        """Test job metrics when pod metrics fail."""
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pod.metadata.labels = {"job-name": "test-job"}

        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods

        # Metrics API fails
        mock_manifest_processor.custom_objects.get_namespaced_custom_object.side_effect = Exception(
            "Metrics not available"
        )

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

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/gco-jobs/test-job/metrics", headers=_AUTH_HEADERS
                )
                assert response.status_code == 200
                data = response.json()
                assert "pods" in data


# =============================================================================
# Template Endpoints Edge Cases
# =============================================================================


class TestTemplateEndpointsEdgeCases:
    """Tests for template endpoint edge cases."""

    def test_create_template_conflict(self, mock_manifest_processor):
        """Test creating template that already exists."""
        mock_template_store = MagicMock()
        mock_template_store.create_template.side_effect = ValueError("Template already exists")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/templates",
                    json={
                        "name": "existing-template",
                        "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 409

    def test_get_template_error(self, mock_manifest_processor):
        """Test getting template with error."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/templates/test-template", headers=_AUTH_HEADERS)
                assert response.status_code == 500

    def test_delete_template_error(self, mock_manifest_processor):
        """Test deleting template with error."""
        mock_template_store = MagicMock()
        mock_template_store.delete_template.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/templates/test-template", headers=_AUTH_HEADERS)
                assert response.status_code == 500

    def test_list_templates_error(self, mock_manifest_processor):
        """Test listing templates with error."""
        mock_template_store = MagicMock()
        mock_template_store.list_templates.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/templates", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Webhook Endpoints Edge Cases
# =============================================================================


class TestWebhookEndpointsEdgeCases:
    """Tests for webhook endpoint edge cases."""

    def test_create_webhook_conflict(self, mock_manifest_processor):
        """Test creating webhook that already exists."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.create_webhook.side_effect = ValueError("Webhook already exists")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=mock_webhook_store),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.webhook_store = mock_webhook_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/webhooks",
                    json={
                        "name": "existing-webhook",
                        "url": "https://example.com/webhook",
                        "events": ["job.completed"],
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 409

    def test_delete_webhook_error(self, mock_manifest_processor):
        """Test deleting webhook with error."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.delete_webhook.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=mock_webhook_store),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.webhook_store = mock_webhook_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/webhooks/test-webhook", headers=_AUTH_HEADERS)
                assert response.status_code == 500

    def test_list_webhooks_error(self, mock_manifest_processor):
        """Test listing webhooks with error."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.list_webhooks.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=mock_webhook_store),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.webhook_store = mock_webhook_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/webhooks", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Create Job From Template Edge Cases
# =============================================================================


class TestCreateJobFromTemplateEdgeCases:
    """Tests for create job from template edge cases."""

    def test_create_job_from_template_submission_failed(self, mock_manifest_processor):
        """Test creating job from template when submission fails."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = {
            "name": "test-template",
            "manifest": {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "main", "image": "test:latest"}],
                            "restartPolicy": "Never",
                        }
                    }
                },
            },
            "parameters": {},  # Parameters should be a dict, not a list
        }

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.errors = ["Resource limit exceeded"]

        mock_manifest_processor.process_manifest_submission = AsyncMock(return_value=mock_result)

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/jobs/from-template/test-template",
                    json={"name": "new-job", "namespace": "gco-jobs"},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 400
                data = response.json()
                assert not data["success"]

    def test_create_job_from_template_error(self, mock_manifest_processor):
        """Test creating job from template with error."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = {
            "name": "test-template",
            "manifest": {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "main", "image": "test:latest"}],
                            "restartPolicy": "Never",
                        }
                    }
                },
            },
            "parameters": {},  # Parameters should be a dict, not a list
        }

        mock_manifest_processor.process_manifest_submission = AsyncMock(
            side_effect=Exception("Processing error")
        )

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/jobs/from-template/test-template",
                    json={"name": "new-job", "namespace": "gco-jobs"},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 500


# =============================================================================
# Poll Jobs Endpoint Edge Cases
# =============================================================================


class TestPollJobsEdgeCases:
    """Tests for poll jobs endpoint edge cases."""

    @staticmethod
    def _claimed_job_store() -> MagicMock:
        """Return a store holding one successfully fenced queued-job claim."""
        queued_job = {
            "job_id": "abc123",
            "job_name": "test-job",
            "manifest": {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "test-job"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "main", "image": "test:latest"}],
                            "restartPolicy": "Never",
                        }
                    }
                },
            },
            "namespace": "gco-jobs",
        }
        mock_job_store = MagicMock(claim_lease_seconds=300)
        mock_job_store.migrate_legacy_records_for_region.return_value = {
            "evaluated": 0,
            "migrated": 0,
            "failed": 0,
            "complete": True,
        }
        mock_job_store.get_queued_jobs_for_region.return_value = [queued_job]
        mock_job_store.claim_job.return_value = {
            **queued_job,
            "claim_token": "claim-token",
            "claim_generation": 1,
        }
        mock_job_store.transition_job.return_value = {"job_id": "abc123"}
        return mock_job_store

    def test_poll_jobs_processing_error(self, mock_manifest_processor):
        """Test poll jobs when deterministic Kubernetes apply raises."""
        mock_job_store = self._claimed_job_store()
        mock_manifest_processor.apply_queued_job.side_effect = RuntimeError("Processing error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=mock_job_store),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.job_store = mock_job_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll?limit=5", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert len(data["results"]) == 1
                assert data["results"][0]["status"] == "failed"

    def test_poll_jobs_with_k8s_uid(self, mock_manifest_processor):
        """Test poll jobs when Kubernetes returns a UID."""
        mock_job_store = self._claimed_job_store()
        mock_resource = MagicMock()
        mock_resource.name = "test-job-queue-id"
        mock_resource.namespace = "gco-jobs"
        mock_resource.uid = "k8s-uid-12345"
        mock_manifest_processor.apply_queued_job.return_value = mock_resource

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=mock_job_store),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.job_store = mock_job_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll?limit=5", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert len(data["results"]) == 1
                assert data["results"][0]["status"] == "applied"
                assert data["results"][0]["k8s_job_uid"] == "k8s-uid-12345"

    def test_poll_jobs_submission_failed(self, mock_manifest_processor):
        """Test poll jobs when deterministic Job validation fails."""
        mock_job_store = self._claimed_job_store()
        mock_manifest_processor.apply_queued_job.side_effect = ValueError(
            "Queued Job validation failed: Validation failed; Resource limit exceeded"
        )

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=mock_job_store),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.job_store = mock_job_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll?limit=5", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert len(data["results"]) == 1
                assert data["results"][0]["status"] == "failed"
                assert "Validation failed" in data["results"][0]["error"]


# =============================================================================
# Service Status Edge Cases
# =============================================================================


class TestServiceStatusEdgeCases:
    """Tests for service status endpoint edge cases."""

    def test_service_status_store_error(self, mock_manifest_processor):
        """Test service status when store counts fail."""
        mock_template_store = MagicMock()
        mock_template_store.list_templates.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch("gco.services.manifest_api.get_template_store", return_value=mock_template_store),
            patch("gco.services.manifest_api.get_webhook_store", return_value=MagicMock()),
            patch("gco.services.manifest_api.get_job_store", return_value=MagicMock()),
        ):
            import gco.services.manifest_api as api_module

            api_module.manifest_processor = mock_manifest_processor
            api_module.template_store = mock_template_store

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/status", headers=_AUTH_HEADERS)
                # Should still return 200 with 0 counts
                assert response.status_code == 200
                data = response.json()
                assert data["templates_count"] == 0


# ============================================================================
# Jobs API route coverage — gco/services/api_routes/jobs.py
# ============================================================================


@pytest.fixture
def jobs_mock_processor():
    """Create a mock ManifestProcessor for jobs API tests."""
    proc = MagicMock()
    proc.cluster_id = "test-cluster"
    proc.region = "us-east-1"
    proc.allowed_namespaces = {"gco-jobs", "default"}
    return proc


@pytest.fixture
def jobs_api_client(jobs_mock_processor):
    """Create a TestClient with mocked processor for jobs API tests."""
    from starlette.testclient import TestClient

    from gco.services.manifest_api import app

    with patch("gco.services.manifest_api.manifest_processor", jobs_mock_processor):
        client = TestClient(app, raise_server_exceptions=False)
        yield client


class TestJobsListCoverage:
    """Cover list_jobs with label_selector, sorting, errors."""

    def test_label_selector(self, jobs_mock_processor, jobs_api_client):
        job1 = {
            "metadata": {
                "name": "j1",
                "creationTimestamp": "2025-01-01T00:00:00Z",
                "labels": {"app": "train"},
            },
            "status": {"active": 1},
        }
        job2 = {
            "metadata": {
                "name": "j2",
                "creationTimestamp": "2025-01-02T00:00:00Z",
                "labels": {"app": "infer"},
            },
            "status": {"active": 0},
        }
        jobs_mock_processor.list_jobs = AsyncMock(return_value=[job1, job2])

        resp = jobs_api_client.get("/api/v1/jobs?label_selector=app%3Dtrain", headers=_AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_sort_by_name(self, jobs_mock_processor, jobs_api_client):
        ja = {
            "metadata": {
                "name": "alpha",
                "creationTimestamp": "2025-01-02T00:00:00Z",
                "labels": {},
            },
            "status": {"active": 0},
        }
        jb = {
            "metadata": {"name": "beta", "creationTimestamp": "2025-01-01T00:00:00Z", "labels": {}},
            "status": {"active": 0},
        }
        jobs_mock_processor.list_jobs = AsyncMock(return_value=[ja, jb])

        resp = jobs_api_client.get("/api/v1/jobs?sort=name:asc", headers=_AUTH_HEADERS)
        assert resp.status_code == 200
        names = [j["metadata"]["name"] for j in resp.json()["jobs"]]
        assert names == ["alpha", "beta"]

    def test_error_500(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.list_jobs = AsyncMock(side_effect=RuntimeError("k8s down"))
        resp = jobs_api_client.get("/api/v1/jobs", headers=_AUTH_HEADERS)
        assert resp.status_code == 500


class TestJobsLogsCoverage:
    def test_pending_pod_409(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.batch_v1.read_namespaced_job.return_value = MagicMock()
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.creation_timestamp = datetime.now(UTC)
        mock_pod.status = MagicMock(phase="Pending")
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/logs", headers=_AUTH_HEADERS)
        assert resp.status_code == 409

    def test_no_pods_404(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.batch_v1.read_namespaced_job.return_value = MagicMock()
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[])

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/logs", headers=_AUTH_HEADERS)
        assert resp.status_code == 404

    def test_success(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.batch_v1.read_namespaced_job.return_value = MagicMock()
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.creation_timestamp = datetime.now(UTC)
        mock_pod.status = MagicMock(phase="Running")
        mock_container = MagicMock()
        mock_container.name = "main"
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.init_containers = []
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        jobs_mock_processor.core_v1.read_namespaced_pod_log.return_value = "line1\nline2"

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/logs", headers=_AUTH_HEADERS)
        assert resp.status_code == 200
        assert "line1" in resp.json()["logs"]

    def test_k8s_api_400(self, jobs_mock_processor, jobs_api_client):
        from kubernetes.client.rest import ApiException as K8sApiException

        jobs_mock_processor.batch_v1.read_namespaced_job.return_value = MagicMock()
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        mock_pod.metadata.creation_timestamp = datetime.now(UTC)
        mock_pod.status = MagicMock(phase="Running")
        mock_container = MagicMock()
        mock_container.name = "main"
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.init_containers = []
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])

        exc = K8sApiException(status=400, reason="Bad Request")
        exc.body = "container is waiting to start"
        jobs_mock_processor.core_v1.read_namespaced_pod_log.side_effect = exc

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/logs", headers=_AUTH_HEADERS)
        assert resp.status_code == 400


class TestJobsEventsCoverage:
    def test_older_than_handles_aware_and_naive_timestamps(
        self, jobs_mock_processor, jobs_api_client
    ):
        old_aware = (datetime.now(UTC) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        recent_naive = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None).isoformat()
        jobs_mock_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "old",
                        "namespace": "gco-jobs",
                        "creationTimestamp": old_aware,
                        "labels": {},
                    }
                },
                {
                    "metadata": {
                        "name": "recent",
                        "namespace": "gco-jobs",
                        "creationTimestamp": recent_naive,
                        "labels": {},
                    }
                },
            ]
        )

        resp = jobs_api_client.request(
            "DELETE",
            "/api/v1/jobs",
            json={"older_than_days": 5, "dry_run": True},
            headers=_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        assert [job["name"] for job in resp.json()["jobs"]] == ["old"]

    def test_error(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.core_v1.list_namespaced_event.side_effect = RuntimeError("fail")
        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/events", headers=_AUTH_HEADERS)
        assert resp.status_code == 500


class TestJobsMetricsCoverage:
    def test_success_millicores(self, jobs_mock_processor, jobs_api_client):
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        jobs_mock_processor.custom_objects.get_namespaced_custom_object.return_value = {
            "containers": [{"name": "main", "usage": {"cpu": "500m", "memory": "256Mi"}}]
        }

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/metrics", headers=_AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["summary"]["total_cpu_millicores"] == 500

    def test_no_pods(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/metrics", headers=_AUTH_HEADERS)
        assert resp.status_code == 404

    def test_per_pod_error(self, jobs_mock_processor, jobs_api_client):
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        jobs_mock_processor.custom_objects.get_namespaced_custom_object.side_effect = RuntimeError(
            "no"
        )

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/metrics", headers=_AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["pods"][0]["error"] == "Metrics not available"

    def test_nanoseconds_and_ki(self, jobs_mock_processor, jobs_api_client):
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        jobs_mock_processor.custom_objects.get_namespaced_custom_object.return_value = {
            "containers": [{"name": "m", "usage": {"cpu": "500000000n", "memory": "1024Ki"}}]
        }

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/metrics", headers=_AUTH_HEADERS)
        assert resp.json()["summary"]["total_cpu_millicores"] == 500
        assert resp.json()["summary"]["total_memory_bytes"] == 1024 * 1024

    def test_whole_cores_and_gi(self, jobs_mock_processor, jobs_api_client):
        mock_pod = MagicMock()
        mock_pod.metadata.name = "pod-1"
        jobs_mock_processor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[mock_pod])
        jobs_mock_processor.custom_objects.get_namespaced_custom_object.return_value = {
            "containers": [{"name": "m", "usage": {"cpu": "2", "memory": "1Gi"}}]
        }

        resp = jobs_api_client.get("/api/v1/jobs/gco-jobs/j/metrics", headers=_AUTH_HEADERS)
        assert resp.json()["summary"]["total_cpu_millicores"] == 2000
        assert resp.json()["summary"]["total_memory_bytes"] == 1024**3


class TestJobsBulkDeleteCoverage:
    def test_dry_run(self, jobs_mock_processor, jobs_api_client):
        job = {
            "metadata": {
                "name": "old",
                "namespace": "gco-jobs",
                "creationTimestamp": "2024-01-01T00:00:00+00:00",
                "labels": {},
            }
        }
        jobs_mock_processor.list_jobs = AsyncMock(return_value=[job])

        resp = jobs_api_client.request(
            "DELETE",
            "/api/v1/jobs",
            json={"namespace": "gco-jobs", "dry_run": True},
            headers=_AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True

    def test_label_selector(self, jobs_mock_processor, jobs_api_client):
        j1 = {
            "metadata": {
                "name": "j1",
                "namespace": "gco-jobs",
                "creationTimestamp": "2024-01-01T00:00:00+00:00",
                "labels": {"t": "ml"},
            }
        }
        j2 = {
            "metadata": {
                "name": "j2",
                "namespace": "gco-jobs",
                "creationTimestamp": "2024-01-01T00:00:00+00:00",
                "labels": {"t": "data"},
            }
        }
        jobs_mock_processor.list_jobs = AsyncMock(return_value=[j1, j2])

        resp = jobs_api_client.request(
            "DELETE",
            "/api/v1/jobs",
            json={"label_selector": "t=ml", "dry_run": True},
            headers=_AUTH_HEADERS,
        )
        assert resp.json()["total_matched"] == 1

    def test_invalid_label_selector_fails_closed(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "must-not-delete",
                        "namespace": "gco-jobs",
                        "labels": {"team": "ml"},
                    }
                }
            ]
        )

        resp = jobs_api_client.request(
            "DELETE",
            "/api/v1/jobs",
            json={"label_selector": "team", "dry_run": False},
            headers=_AUTH_HEADERS,
        )

        assert resp.status_code == 400
        jobs_mock_processor.batch_v1.delete_namespaced_job.assert_not_called()

    def test_error(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.list_jobs = AsyncMock(side_effect=RuntimeError("fail"))
        resp = jobs_api_client.request(
            "DELETE", "/api/v1/jobs", json={"dry_run": True}, headers=_AUTH_HEADERS
        )
        assert resp.status_code == 500


class TestJobsRetryCoverage:
    def test_success(self, jobs_mock_processor, jobs_api_client):
        mock_job = MagicMock()
        mock_job.metadata.name = "orig"
        mock_job.metadata.namespace = "gco-jobs"
        mock_job.metadata.labels = {"app": "train"}
        mock_job.metadata.annotations = {}
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 3
        mock_job.spec.template.to_dict.return_value = {
            "spec": {"containers": [{"name": "main", "image": "test:latest"}]},
        }
        jobs_mock_processor.batch_v1.read_namespaced_job.return_value = mock_job

        mock_result = MagicMock(success=True, errors=[])
        jobs_mock_processor.process_manifest_submission = AsyncMock(return_value=mock_result)

        resp = jobs_api_client.post("/api/v1/jobs/gco-jobs/orig/retry", headers=_AUTH_HEADERS)
        assert resp.status_code == 201
        assert resp.json()["success"] is True

    def test_submission_failure(self, jobs_mock_processor, jobs_api_client):
        mock_job = MagicMock()
        mock_job.metadata.name = "f"
        mock_job.metadata.namespace = "gco-jobs"
        mock_job.metadata.labels = {}
        mock_job.metadata.annotations = {}
        mock_job.spec.parallelism = 1
        mock_job.spec.completions = 1
        mock_job.spec.backoff_limit = 3
        mock_job.spec.template.to_dict.return_value = {"spec": {"containers": []}}
        jobs_mock_processor.batch_v1.read_namespaced_job.return_value = mock_job

        mock_result = MagicMock(success=False, errors=["validation failed"])
        jobs_mock_processor.process_manifest_submission = AsyncMock(return_value=mock_result)

        resp = jobs_api_client.post("/api/v1/jobs/gco-jobs/f/retry", headers=_AUTH_HEADERS)
        assert resp.status_code == 400

    def test_generic_error(self, jobs_mock_processor, jobs_api_client):
        jobs_mock_processor.batch_v1.read_namespaced_job.side_effect = RuntimeError("internal")
        resp = jobs_api_client.post("/api/v1/jobs/gco-jobs/broken/retry", headers=_AUTH_HEADERS)
        assert resp.status_code == 500


class TestJobsNamespaceCheckCoverage:
    def test_forbidden_namespace(self, jobs_mock_processor, jobs_api_client):
        resp = jobs_api_client.get("/api/v1/jobs/kube-system/some-job", headers=_AUTH_HEADERS)
        assert resp.status_code == 403
