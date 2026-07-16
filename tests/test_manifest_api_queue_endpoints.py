"""
Tests for the /api/v1/queue/* endpoints on the Manifest API.

Exercises the job-queue surface: POST /api/v1/queue/jobs (submit with
priority/labels, returns a queued job record from the job store),
listing queued jobs, status retrieval, and the SQS consumer poll
endpoint. Uses a mock_manifest_processor fixture plus a mocked
job_store that's patched into the module global, and seeds the auth
middleware token cache with an autouse fixture.
"""

import hashlib
import hmac
import secrets
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_TEST_SIGNING_KEY = "test-queue-endpoints-signing-key"  # nosec B105 - test-only key
_REQUEST_HEADERS: dict[str, str] = {}


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
                    headers=_REQUEST_HEADERS,
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
                    headers=_REQUEST_HEADERS,
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
                    headers=_REQUEST_HEADERS,
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
                    headers=_REQUEST_HEADERS,
                )
                assert response.status_code == 500


# =============================================================================
# List Queued Jobs Endpoint Tests
# =============================================================================


class TestListQueuedJobsEndpoint:
    """Tests for GET /api/v1/queue/jobs endpoint."""

    def test_list_queued_jobs_success(self, mock_manifest_processor):
        """A bounded queue page exposes its opaque cursor and partial flag."""
        mock_job_store = MagicMock()
        mock_job_store.list_jobs_page.return_value = (
            [
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
            ],
            "next-page-token",
            True,
        )

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
                response = client.get("/api/v1/queue/jobs", headers=_REQUEST_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert len(data["jobs"]) == 2
        assert data["next_cursor"] == "next-page-token"
        assert data["partial"] is True
        mock_job_store.list_jobs_page.assert_called_once_with(
            target_region=None,
            status=None,
            namespace=None,
            limit=100,
            cursor=None,
        )

    def test_list_queued_jobs_with_filters(self, mock_manifest_processor):
        """Filters and an opaque cursor are forwarded as one page identity."""
        mock_job_store = MagicMock()
        mock_job_store.list_jobs_page.return_value = ([], None, False)

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
                    "/api/v1/queue/jobs?target_region=us-east-1&status=queued&namespace=gco-jobs&limit=50&cursor=opaque-page",
                    headers=_REQUEST_HEADERS,
                )

        assert response.status_code == 200
        assert response.json()["next_cursor"] is None
        assert response.json()["partial"] is False
        mock_job_store.list_jobs_page.assert_called_once_with(
            target_region="us-east-1",
            status="queued",
            namespace="gco-jobs",
            limit=50,
            cursor="opaque-page",
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
                response = client.get("/api/v1/queue/jobs", headers=_REQUEST_HEADERS)
                assert response.status_code == 503

    def test_list_queued_jobs_error(self, mock_manifest_processor):
        """Test listing queued jobs with error."""
        mock_job_store = MagicMock()
        mock_job_store.list_jobs_page.side_effect = Exception("DynamoDB error")

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
                response = client.get("/api/v1/queue/jobs", headers=_REQUEST_HEADERS)
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
                response = client.get("/api/v1/queue/jobs/abc123", headers=_REQUEST_HEADERS)
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
                response = client.get("/api/v1/queue/jobs/nonexistent", headers=_REQUEST_HEADERS)
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
                response = client.get("/api/v1/queue/jobs/abc123", headers=_REQUEST_HEADERS)
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
                response = client.delete("/api/v1/queue/jobs/abc123", headers=_REQUEST_HEADERS)
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
                    "/api/v1/queue/jobs/abc123?reason=No%20longer%20needed",
                    headers=_REQUEST_HEADERS,
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
                response = client.delete("/api/v1/queue/jobs/abc123", headers=_REQUEST_HEADERS)
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
                response = client.delete("/api/v1/queue/jobs/abc123", headers=_REQUEST_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Queue Stats Endpoint Tests
# =============================================================================


class TestQueueStatsEndpoint:
    """Tests for GET /api/v1/queue/stats endpoint."""

    def test_get_queue_stats_success(self, mock_manifest_processor):
        """Test getting queue stats returns success."""
        mock_job_store = MagicMock()
        mock_job_store.get_job_count_summary.return_value = (
            {
                "us-east-1": {"queued": 5, "running": 3, "succeeded": 40, "failed": 2},
                "us-west-2": {"queued": 3, "running": 2, "succeeded": 30, "failed": 1},
            },
            86,
            False,
        )

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
                response = client.get("/api/v1/queue/stats", headers=_REQUEST_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert "summary" in data
                assert "by_region" in data
                assert data["summary"]["total_queued"] == 8
                assert data["summary"]["total_running"] == 5
                assert data["summary"]["records_evaluated"] == 86
                assert data["summary"]["complete"] is True

    def test_get_queue_stats_empty(self, mock_manifest_processor):
        """Test getting queue stats when empty."""
        mock_job_store = MagicMock()
        mock_job_store.get_job_count_summary.return_value = ({}, 0, False)

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
                response = client.get("/api/v1/queue/stats", headers=_REQUEST_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["summary"]["total_jobs"] == 0

    def test_get_queue_stats_error(self, mock_manifest_processor):
        """Test getting queue stats with error."""
        mock_job_store = MagicMock()
        mock_job_store.get_job_count_summary.side_effect = Exception("DynamoDB error")

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
                response = client.get("/api/v1/queue/stats", headers=_REQUEST_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Poll and Process Jobs Endpoint Tests
# =============================================================================


@pytest.fixture
def mock_queue_worker():
    """Patch the route's shared fenced worker for endpoint-contract tests."""
    with patch(
        "gco.services.api_routes.queue.process_queued_jobs_once",
        new_callable=AsyncMock,
    ) as worker:
        yield worker


class TestPollAndProcessJobsEndpoint:
    """Tests for POST /api/v1/queue/poll endpoint."""

    def test_poll_and_process_jobs_success(self, mock_manifest_processor, mock_queue_worker):
        """The route delegates one bounded pass to the shared fenced worker."""
        mock_queue_worker.return_value = (
            1,
            [
                {
                    "job_id": "abc123",
                    "status": "applied",
                    "k8s_job_name": "test-job",
                    "k8s_job_uid": "k8s-uid-123",
                }
            ],
        )
        mock_job_store = MagicMock()

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
                response = client.post("/api/v1/queue/poll?limit=5", headers=_REQUEST_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_polled"] == 1
                assert data["jobs_processed"] == 1
                assert data["results"][0]["status"] == "applied"
                mock_queue_worker.assert_awaited_once_with(
                    mock_manifest_processor,
                    mock_job_store,
                    limit=5,
                )

    def test_poll_and_process_jobs_no_jobs(self, mock_manifest_processor, mock_queue_worker):
        """An empty worker pass is reported without synthetic processing."""
        mock_queue_worker.return_value = (0, [])
        mock_job_store = MagicMock()

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
                response = client.post("/api/v1/queue/poll", headers=_REQUEST_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_polled"] == 0
                assert data["jobs_processed"] == 0

    def test_poll_and_process_jobs_no_processed_results(
        self, mock_manifest_processor, mock_queue_worker
    ):
        """A pass can report polled records without completed processing results."""
        mock_queue_worker.return_value = (1, [])
        mock_job_store = MagicMock()

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
                response = client.post("/api/v1/queue/poll", headers=_REQUEST_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_polled"] == 1
                assert data["jobs_processed"] == 0  # Claim failed

    def test_poll_and_process_jobs_submission_failed(
        self, mock_manifest_processor, mock_queue_worker
    ):
        """Per-record worker failures remain visible in the bounded result list."""
        mock_queue_worker.return_value = (
            1,
            [{"job_id": "abc123", "status": "failed", "error": "Validation failed"}],
        )
        mock_job_store = MagicMock()

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
                response = client.post("/api/v1/queue/poll", headers=_REQUEST_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["jobs_processed"] == 1
                assert data["results"][0]["status"] == "failed"

    def test_poll_and_process_jobs_exception(self, mock_manifest_processor, mock_queue_worker):
        """A pass-level worker failure is surfaced as an HTTP 500."""
        mock_queue_worker.side_effect = RuntimeError("K8s API error")
        mock_job_store = MagicMock()

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
                response = client.post("/api/v1/queue/poll", headers=_REQUEST_HEADERS)
                assert response.status_code == 500
                assert "K8s API error" in response.json()["detail"]

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
                response = client.post("/api/v1/queue/poll", headers=_REQUEST_HEADERS)
                assert response.status_code == 503


# =============================================================================
# Route Existence Tests
# =============================================================================


class TestQueueRouteExistence:
    """Tests to verify all queue routes exist."""

    def test_app_has_queue_routes(self):
        """Test app has all queue routes."""
        from gco.services.manifest_api import app

        routes = [r.path for r in app.routes if hasattr(r, "path")] + [
            s.path
            for r in app.routes
            for s in getattr(getattr(r, "original_router", None), "routes", [])
            if hasattr(s, "path")
        ]

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
                response = client.get("/api/v1/templates", headers=_REQUEST_HEADERS)
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
                    headers=_REQUEST_HEADERS,
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
                response = client.get("/api/v1/webhooks", headers=_REQUEST_HEADERS)
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
                    headers=_REQUEST_HEADERS,
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
                response = client.get("/api/v1/templates", headers=_REQUEST_HEADERS)
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
                response = client.get("/api/v1/templates/test", headers=_REQUEST_HEADERS)
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
                response = client.delete("/api/v1/templates/test", headers=_REQUEST_HEADERS)
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
                response = client.get("/api/v1/webhooks", headers=_REQUEST_HEADERS)
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
                    headers=_REQUEST_HEADERS,
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
                response = client.delete("/api/v1/webhooks/abc123", headers=_REQUEST_HEADERS)
                assert response.status_code == 500
