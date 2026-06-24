"""
Tests for the /api/v1/queue/* endpoints on the Manifest API.

Exercises the job-queue surface: POST /api/v1/queue/jobs (submit with
priority/labels, returns a queued job record from the job store),
listing queued jobs, status retrieval, and the SQS consumer poll
endpoint. Uses a mock_manifest_processor fixture plus a mocked
job_store that's patched into the module global, and seeds the auth
middleware token cache with an autouse fixture.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Auth token used by all tests in this module.
_TEST_AUTH_TOKEN = "test-queue-endpoints-token"  # nosec B105 - test fixture token, not a real credential
_AUTH_HEADERS = {"x-gco-auth-token": _TEST_AUTH_TOKEN}


@pytest.fixture(autouse=True)
def _seed_auth_cache():
    """Seed the auth middleware token cache with a known token."""
    import gco.services.auth_middleware as auth_module

    original_tokens = auth_module._cached_tokens
    original_timestamp = auth_module._cache_timestamp
    auth_module._cached_tokens = {_TEST_AUTH_TOKEN}
    auth_module._cache_timestamp = time.time()
    yield
    auth_module._cached_tokens = original_tokens
    auth_module._cache_timestamp = original_timestamp


@pytest.fixture
def mock_manifest_processor():
    """Fixture to mock the manifest processor creation."""
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
# Submit Job to Queue Endpoint Tests
# =============================================================================


class TestSubmitJobToQueueEndpoint:
    """Tests for POST /api/v1/queue/jobs endpoint."""

    def test_submit_job_to_queue_success(self, mock_manifest_processor):
        """Test submitting job to queue returns success."""
        mock_job_store = MagicMock()
        mock_job_store.submit_job.return_value = {
            "job_id": "abc123-def456",
            "job_name": "test-job",
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "status": "queued",
            "priority": 10,
            "submitted_at": "2024-01-01T00:00:00Z",
        }

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/queue/jobs",
                    json={
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
                        "target_region": "us-east-1",
                        "namespace": "gco-jobs",
                        "priority": 10,
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 201
                data = response.json()
                assert "job" in data
                assert data["job"]["status"] == "queued"

    def test_submit_job_to_queue_with_labels(self, mock_manifest_processor):
        """Test submitting job to queue with labels."""
        mock_job_store = MagicMock()
        mock_job_store.submit_job.return_value = {
            "job_id": "abc123",
            "status": "queued",
            "labels": {"team": "ml", "env": "prod"},
        }

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/queue/jobs",
                    json={
                        "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                        "target_region": "us-east-1",
                        "labels": {"team": "ml", "env": "prod"},
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 201
                mock_job_store.submit_job.assert_called_once()
                call_kwargs = mock_job_store.submit_job.call_args.kwargs
                assert call_kwargs["labels"] == {"team": "ml", "env": "prod"}

    def test_submit_job_to_queue_store_not_initialized(self, mock_manifest_processor):
        """Test submitting job when job store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=None,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/queue/jobs",
                    json={
                        "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                        "target_region": "us-east-1",
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 503

    def test_submit_job_to_queue_error(self, mock_manifest_processor):
        """Test submitting job to queue with error."""
        mock_job_store = MagicMock()
        mock_job_store.submit_job.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/queue/jobs",
                    json={
                        "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                        "target_region": "us-east-1",
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 500


# =============================================================================
# List Queued Jobs Endpoint Tests
# =============================================================================


class TestListQueuedJobsEndpoint:
    """Tests for GET /api/v1/queue/jobs endpoint."""

    def test_list_queued_jobs_success(self, mock_manifest_processor):
        """Test listing queued jobs returns success."""
        mock_job_store = MagicMock()
        mock_job_store.list_jobs.return_value = [
            {
                "job_id": "abc123",
                "job_name": "test-job-1",
                "target_region": "us-east-1",
                "status": "queued",
            },
            {
                "job_id": "def456",
                "job_name": "test-job-2",
                "target_region": "us-west-2",
                "status": "running",
            },
        ]

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/jobs", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["count"] == 2
                assert len(data["jobs"]) == 2

    def test_list_queued_jobs_with_filters(self, mock_manifest_processor):
        """Test listing queued jobs with filters."""
        mock_job_store = MagicMock()
        mock_job_store.list_jobs.return_value = []

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/queue/jobs?target_region=us-east-1&status=queued&namespace=gco-jobs&limit=50",
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 200
                mock_job_store.list_jobs.assert_called_once_with(
                    target_region="us-east-1",
                    status="queued",
                    namespace="gco-jobs",
                    limit=50,
                )

    def test_list_queued_jobs_store_not_initialized(self, mock_manifest_processor):
        """Test listing jobs when job store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=None,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/jobs", headers=_AUTH_HEADERS)
                assert response.status_code == 503

    def test_list_queued_jobs_error(self, mock_manifest_processor):
        """Test listing queued jobs with error."""
        mock_job_store = MagicMock()
        mock_job_store.list_jobs.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/jobs", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Get Queued Job Endpoint Tests
# =============================================================================


class TestGetQueuedJobEndpoint:
    """Tests for GET /api/v1/queue/jobs/{job_id} endpoint."""

    def test_get_queued_job_success(self, mock_manifest_processor):
        """Test getting queued job returns success."""
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = {
            "job_id": "abc123",
            "job_name": "test-job",
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "status": "running",
            "priority": 10,
            "submitted_at": "2024-01-01T00:00:00Z",
            "claimed_by": "us-east-1",
            "status_history": [
                {"timestamp": "2024-01-01T00:00:00Z", "status": "queued", "message": "Job queued"},
                {
                    "timestamp": "2024-01-01T00:01:00Z",
                    "status": "claimed",
                    "message": "Claimed by us-east-1",
                },
            ],
        }

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/jobs/abc123", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["job"]["job_id"] == "abc123"
                assert data["job"]["status"] == "running"

    def test_get_queued_job_not_found(self, mock_manifest_processor):
        """Test getting non-existent queued job returns 404."""
        mock_job_store = MagicMock()
        mock_job_store.get_job.return_value = None

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/jobs/nonexistent", headers=_AUTH_HEADERS)
                assert response.status_code == 404

    def test_get_queued_job_error(self, mock_manifest_processor):
        """Test getting queued job with error."""
        mock_job_store = MagicMock()
        mock_job_store.get_job.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/jobs/abc123", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Cancel Queued Job Endpoint Tests
# =============================================================================


class TestCancelQueuedJobEndpoint:
    """Tests for DELETE /api/v1/queue/jobs/{job_id} endpoint."""

    def test_cancel_queued_job_success(self, mock_manifest_processor):
        """Test cancelling queued job returns success."""
        mock_job_store = MagicMock()
        mock_job_store.cancel_job.return_value = True

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/queue/jobs/abc123", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert "cancelled" in data["message"].lower()

    def test_cancel_queued_job_with_reason(self, mock_manifest_processor):
        """Test cancelling queued job with reason."""
        mock_job_store = MagicMock()
        mock_job_store.cancel_job.return_value = True

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete(
                    "/api/v1/queue/jobs/abc123?reason=No%20longer%20needed", headers=_AUTH_HEADERS
                )
                assert response.status_code == 200
                mock_job_store.cancel_job.assert_called_once_with(
                    "abc123", reason="No longer needed"
                )

    def test_cancel_queued_job_cannot_cancel(self, mock_manifest_processor):
        """Test cancelling job that cannot be cancelled returns 409."""
        mock_job_store = MagicMock()
        mock_job_store.cancel_job.return_value = False

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/queue/jobs/abc123", headers=_AUTH_HEADERS)
                assert response.status_code == 409

    def test_cancel_queued_job_error(self, mock_manifest_processor):
        """Test cancelling queued job with error."""
        mock_job_store = MagicMock()
        mock_job_store.cancel_job.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/queue/jobs/abc123", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Queue Stats Endpoint Tests
# =============================================================================


class TestQueueStatsEndpoint:
    """Tests for GET /api/v1/queue/stats endpoint."""

    def test_get_queue_stats_success(self, mock_manifest_processor):
        """Test getting queue stats returns success."""
        mock_job_store = MagicMock()
        mock_job_store.get_job_counts_by_region.return_value = {
            "us-east-1": {"queued": 5, "running": 3, "succeeded": 40, "failed": 2},
            "us-west-2": {"queued": 3, "running": 2, "succeeded": 30, "failed": 1},
        }

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/stats", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert "summary" in data
                assert "by_region" in data
                assert data["summary"]["total_queued"] == 8
                assert data["summary"]["total_running"] == 5

    def test_get_queue_stats_empty(self, mock_manifest_processor):
        """Test getting queue stats when empty."""
        mock_job_store = MagicMock()
        mock_job_store.get_job_counts_by_region.return_value = {}

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/stats", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["summary"]["total_jobs"] == 0

    def test_get_queue_stats_error(self, mock_manifest_processor):
        """Test getting queue stats with error."""
        mock_job_store = MagicMock()
        mock_job_store.get_job_counts_by_region.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/queue/stats", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Poll and Process Jobs Endpoint Tests
# =============================================================================


class TestPollAndProcessJobsEndpoint:
    """Tests for POST /api/v1/queue/poll endpoint."""

    def test_poll_and_process_jobs_success(self, mock_manifest_processor):
        """Test polling and processing jobs returns success."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        mock_response = ManifestSubmissionResponse(
            success=True,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="batch/v1",
                    kind="Job",
                    name="test-job",
                    namespace="gco-jobs",
                    status="created",
                    uid="k8s-uid-123",
                )
            ],
        )
        mock_manifest_processor.process_manifest_submission = AsyncMock(return_value=mock_response)

        mock_job_store = MagicMock()
        mock_job_store.get_queued_jobs_for_region.return_value = [
            {
                "job_id": "abc123",
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "gco-jobs"},
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
        ]
        mock_job_store.claim_job.return_value = True
        mock_job_store.update_job_status.return_value = None

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll?limit=5", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_polled"] == 1
                assert data["jobs_processed"] == 1
                assert data["results"][0]["status"] == "applied"

    def test_poll_and_process_jobs_no_jobs(self, mock_manifest_processor):
        """Test polling when no jobs available."""
        mock_job_store = MagicMock()
        mock_job_store.get_queued_jobs_for_region.return_value = []

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_polled"] == 0
                assert data["jobs_processed"] == 0

    def test_poll_and_process_jobs_claim_failed(self, mock_manifest_processor):
        """Test polling when job claim fails (already claimed)."""
        mock_job_store = MagicMock()
        mock_job_store.get_queued_jobs_for_region.return_value = [
            {
                "job_id": "abc123",
                "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                "namespace": "gco-jobs",
            }
        ]
        mock_job_store.claim_job.return_value = False  # Already claimed

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_polled"] == 1
                assert data["jobs_processed"] == 0  # Claim failed

    def test_poll_and_process_jobs_submission_failed(self, mock_manifest_processor):
        """Test polling when job submission fails."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        mock_response = ManifestSubmissionResponse(
            success=False,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="batch/v1",
                    kind="Job",
                    name="test-job",
                    namespace="gco-jobs",
                    status="failed",
                    message="Validation failed",
                )
            ],
            errors=["Validation failed"],
        )
        mock_manifest_processor.process_manifest_submission = AsyncMock(return_value=mock_response)

        mock_job_store = MagicMock()
        mock_job_store.get_queued_jobs_for_region.return_value = [
            {
                "job_id": "abc123",
                "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                "namespace": "gco-jobs",
            }
        ]
        mock_job_store.claim_job.return_value = True
        mock_job_store.update_job_status.return_value = None

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_processed"] == 1
                assert data["results"][0]["status"] == "failed"

    def test_poll_and_process_jobs_exception(self, mock_manifest_processor):
        """Test polling when processing throws exception."""
        mock_manifest_processor.process_manifest_submission = AsyncMock(
            side_effect=Exception("K8s API error")
        )

        mock_job_store = MagicMock()
        mock_job_store.get_queued_jobs_for_region.return_value = [
            {
                "job_id": "abc123",
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "test-job", "namespace": "gco-jobs"},
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
        ]
        mock_job_store.claim_job.return_value = True
        mock_job_store.update_job_status.return_value = None

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=mock_job_store,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = mock_job_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["results"][0]["status"] == "failed"
                assert "K8s API error" in data["results"][0]["error"]

    def test_poll_and_process_jobs_store_not_initialized(self, mock_manifest_processor):
        """Test polling when job store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=None,
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.job_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/queue/poll", headers=_AUTH_HEADERS)
                assert response.status_code == 503


# =============================================================================
# Route Existence Tests
# =============================================================================


class TestQueueRouteExistence:
    """Tests to verify all queue routes exist."""

    def test_app_has_queue_routes(self):
        """Test app has all queue routes."""
        from gco.services.manifest_api import app

        routes = [r.path for r in app.routes if hasattr(r, "path")] + [s.path for r in app.routes for s in getattr(getattr(r, "original_router", None), "routes", []) if hasattr(s, "path")]

        # Queue endpoints
        assert "/api/v1/queue/jobs" in routes
        assert "/api/v1/queue/jobs/{job_id}" in routes
        assert "/api/v1/queue/stats" in routes
        assert "/api/v1/queue/poll" in routes


# =============================================================================
# Additional Template and Webhook Tests
# =============================================================================


class TestTemplateStoreNotInitialized:
    """Tests for template endpoints when store is not initialized."""

    def test_list_templates_store_not_initialized(self, mock_manifest_processor):
        """Test listing templates when store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=None,
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.template_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/templates", headers=_AUTH_HEADERS)
                assert response.status_code == 503

    def test_create_template_store_not_initialized(self, mock_manifest_processor):
        """Test creating template when store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=None,
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.template_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/templates",
                    json={"name": "test", "manifest": {}},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 503


class TestWebhookStoreNotInitialized:
    """Tests for webhook endpoints when store is not initialized."""

    def test_list_webhooks_store_not_initialized(self, mock_manifest_processor):
        """Test listing webhooks when store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=None,
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/webhooks", headers=_AUTH_HEADERS)
                assert response.status_code == 503

    def test_create_webhook_store_not_initialized(self, mock_manifest_processor):
        """Test creating webhook when store is not initialized."""
        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=None,
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = None

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/webhooks",
                    json={"url": "https://example.com", "events": ["job.completed"]},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 503


class TestTemplateServerErrors:
    """Tests for template endpoint server errors."""

    def test_list_templates_server_error(self, mock_manifest_processor):
        """Test listing templates with server error."""
        mock_template_store = MagicMock()
        mock_template_store.list_templates.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=mock_template_store,
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.template_store = mock_template_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/templates", headers=_AUTH_HEADERS)
                assert response.status_code == 500

    def test_get_template_server_error(self, mock_manifest_processor):
        """Test getting template with server error."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=mock_template_store,
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.template_store = mock_template_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/templates/test", headers=_AUTH_HEADERS)
                assert response.status_code == 500

    def test_delete_template_server_error(self, mock_manifest_processor):
        """Test deleting template with server error."""
        mock_template_store = MagicMock()
        mock_template_store.delete_template.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=mock_template_store,
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.template_store = mock_template_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/templates/test", headers=_AUTH_HEADERS)
                assert response.status_code == 500


class TestWebhookServerErrors:
    """Tests for webhook endpoint server errors."""

    def test_list_webhooks_server_error(self, mock_manifest_processor):
        """Test listing webhooks with server error."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.list_webhooks.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=mock_webhook_store,
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = mock_webhook_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/webhooks", headers=_AUTH_HEADERS)
                assert response.status_code == 500

    def test_create_webhook_server_error(self, mock_manifest_processor):
        """Test creating webhook with server error."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.create_webhook.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=mock_webhook_store,
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = mock_webhook_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/webhooks",
                    json={"url": "https://example.com", "events": ["job.completed"]},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 500

    def test_delete_webhook_server_error(self, mock_manifest_processor):
        """Test deleting webhook with server error."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.delete_webhook.side_effect = Exception("DynamoDB error")

        with (
            patch(
                "gco.services.manifest_api.create_manifest_processor_from_env",
                return_value=mock_manifest_processor,
            ),
            patch(
                "gco.services.manifest_api.get_template_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.manifest_api.get_webhook_store",
                return_value=mock_webhook_store,
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = mock_webhook_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.delete("/api/v1/webhooks/abc123", headers=_AUTH_HEADERS)
                assert response.status_code == 500
