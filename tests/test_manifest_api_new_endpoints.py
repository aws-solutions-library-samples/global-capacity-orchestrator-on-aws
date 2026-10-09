"""
Tests for the endpoint functions now living in gco/services/api_routes/.

Covers the newer manifest-API endpoints split out of manifest_api.py:
pagination on /api/v1/jobs, per-job /events, /pods, /metrics, bulk
delete, retry, and the templates and webhooks surfaces. Drives them
via TestClient with a mock_manifest_processor fixture that stubs
every Kubernetes client used by the handlers. Authentication is bypassed
explicitly because its cryptographic behavior has dedicated tests.
"""

import copy
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

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
# Job Events Endpoint Tests
# =============================================================================


class TestGetJobEventsEndpoint:
    """Tests for GET /api/v1/jobs/{namespace}/{name}/events endpoint."""

    def test_get_job_events_success(self, mock_manifest_processor):
        """Test getting job events returns success."""
        # Mock job events
        mock_event = MagicMock()
        mock_event.type = "Normal"
        mock_event.reason = "SuccessfulCreate"
        mock_event.message = "Created pod: test-job-abc123"
        mock_event.count = 1
        mock_event.first_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_event.last_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_event.source.component = "job-controller"
        mock_event.source.host = None
        mock_event.involved_object.kind = "Job"
        mock_event.involved_object.name = "test-job"
        mock_event.involved_object.namespace = "default"

        mock_events = MagicMock()
        mock_events.items = [mock_event]

        mock_manifest_processor.core_v1.list_namespaced_event.return_value = mock_events

        # Mock pods (empty for this test)
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
                response = client.get("/api/v1/jobs/default/test-job/events", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["job_name"] == "test-job"
                assert data["namespace"] == "default"
                assert "events" in data
                assert data["count"] >= 0

    def test_get_job_events_disallowed_namespace(self, mock_manifest_processor):
        """Test getting job events from disallowed namespace returns 403."""
        mock_manifest_processor.allowed_namespaces = {"default", "gco-jobs"}

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/kube-system/test-job/events", headers=_AUTH_HEADERS
                )
                assert response.status_code == 403

    def test_get_job_events_server_error(self, mock_manifest_processor):
        """Test getting job events with server error returns 500."""
        mock_manifest_processor.core_v1.list_namespaced_event.side_effect = Exception(
            "Internal error"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/jobs/default/test-job/events", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Job Pods Endpoint Tests
# =============================================================================


class TestGetJobPodsEndpoint:
    """Tests for GET /api/v1/jobs/{namespace}/{name}/pods endpoint."""

    def test_get_job_pods_success(self, mock_manifest_processor):
        """Test getting job pods returns success."""
        mock_container = MagicMock()
        mock_container.name = "main"
        mock_container.image = "test:latest"

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_pod.metadata.labels = {"job-name": "test-job"}
        mock_pod.metadata.uid = "pod-uid"
        mock_pod.spec.node_name = "node-1"
        mock_pod.spec.containers = [mock_container]
        mock_pod.spec.init_containers = []
        mock_pod.status.phase = "Running"
        mock_pod.status.host_ip = "10.0.0.1"
        mock_pod.status.pod_ip = "10.0.1.1"
        mock_pod.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_pod.status.container_statuses = []
        mock_pod.status.init_container_statuses = []

        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/jobs/default/test-job/pods", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["job_name"] == "test-job"
                assert data["count"] == 1
                assert "pods" in data

    def test_get_job_pods_disallowed_namespace(self, mock_manifest_processor):
        """Test getting job pods from disallowed namespace returns 403."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/kube-system/test-job/pods", headers=_AUTH_HEADERS
                )
                assert response.status_code == 403

    def test_get_job_pods_server_error(self, mock_manifest_processor):
        """Test getting job pods with server error returns 500."""
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
                response = client.get("/api/v1/jobs/default/test-job/pods", headers=_AUTH_HEADERS)
                assert response.status_code == 500


# =============================================================================
# Pod Logs Endpoint Tests
# =============================================================================


class TestGetPodLogsEndpoint:
    """Tests for GET /api/v1/jobs/{namespace}/{name}/pods/{pod}/logs endpoint."""

    def test_get_pod_logs_success(self, mock_manifest_processor):
        """Test getting specific pod logs returns success."""
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"
        mock_pod.metadata.labels = {"job-name": "test-job"}

        mock_manifest_processor.core_v1.read_namespaced_pod.return_value = mock_pod
        raw_log_response = MagicMock()
        raw_log_response.data = b"line1\nline2\nline3"
        mock_manifest_processor.core_v1.read_namespaced_pod_log.return_value = raw_log_response

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/pods/test-job-abc123/logs", headers=_AUTH_HEADERS
                )
                assert response.status_code == 200
                data = response.json()
                assert data["pod_name"] == "test-job-abc123"
                assert data["logs"] == "line1\nline2\nline3"
                call_kwargs = (
                    mock_manifest_processor.core_v1.read_namespaced_pod_log.call_args.kwargs
                )
                assert call_kwargs["_preload_content"] is False
                raw_log_response.release_conn.assert_called_once_with()

    def test_get_pod_logs_wrong_job(self, mock_manifest_processor):
        """Test getting pod logs for pod not belonging to job returns 400."""
        mock_pod = MagicMock()
        mock_pod.metadata.name = "other-job-abc123"
        mock_pod.metadata.labels = {"job-name": "other-job"}

        mock_manifest_processor.core_v1.read_namespaced_pod.return_value = mock_pod

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/pods/other-job-abc123/logs",
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 400

    def test_get_pod_logs_not_found(self, mock_manifest_processor):
        """Test getting logs for non-existent pod returns 404."""
        mock_manifest_processor.core_v1.read_namespaced_pod.side_effect = Exception(
            "NotFound: pod not found"
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/pods/nonexistent/logs", headers=_AUTH_HEADERS
                )
                assert response.status_code == 404


# =============================================================================
# Job Metrics Endpoint Tests
# =============================================================================


class TestGetJobMetricsEndpoint:
    """Tests for GET /api/v1/jobs/{namespace}/{name}/metrics endpoint."""

    def test_get_job_metrics_success(self, mock_manifest_processor):
        """Test getting job metrics returns success."""
        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-job-abc123"

        mock_pods = MagicMock()
        mock_pods.items = [mock_pod]
        mock_manifest_processor.core_v1.list_namespaced_pod.return_value = mock_pods

        # Mock metrics API response
        mock_manifest_processor.custom_objects.get_namespaced_custom_object.return_value = {
            "containers": [{"name": "main", "usage": {"cpu": "100m", "memory": "256Mi"}}]
        }

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/default/test-job/metrics", headers=_AUTH_HEADERS
                )
                assert response.status_code == 200
                data = response.json()
                assert data["job_name"] == "test-job"
                assert "summary" in data
                assert "pods" in data

    def test_get_job_metrics_no_pods(self, mock_manifest_processor):
        """Test getting metrics when no pods found returns 404."""
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
                    "/api/v1/jobs/default/test-job/metrics", headers=_AUTH_HEADERS
                )
                assert response.status_code == 404

    def test_get_job_metrics_disallowed_namespace(self, mock_manifest_processor):
        """Test getting metrics from disallowed namespace returns 403."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/jobs/kube-system/test-job/metrics", headers=_AUTH_HEADERS
                )
                assert response.status_code == 403


# =============================================================================
# Bulk Delete Jobs Endpoint Tests
# =============================================================================


class TestBulkDeleteJobsEndpoint:
    """Tests for DELETE /api/v1/jobs endpoint."""

    def test_bulk_delete_jobs_dry_run(self, mock_manifest_processor):
        """Test bulk delete with dry_run returns preview."""
        mock_manifest_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "job-1",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-01T00:00:00Z",
                        "labels": {},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
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
                response = client.request(
                    "DELETE",
                    "/api/v1/jobs",
                    json={"namespace": "default", "dry_run": True},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 200
                data = response.json()
                assert data["dry_run"] is True
                assert data["total_matched"] >= 0

    def test_bulk_delete_jobs_actual_delete(self, mock_manifest_processor):
        """Test bulk delete actually deletes jobs."""
        mock_manifest_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "job-1",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-01T00:00:00Z",
                        "labels": {},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
                }
            ]
        )
        mock_manifest_processor.batch_v1.delete_namespaced_job.return_value = None

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.request(
                    "DELETE",
                    "/api/v1/jobs",
                    json={"namespace": "default", "dry_run": False},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 200
                data = response.json()
                assert data["dry_run"] is False

    def test_bulk_delete_jobs_with_status_filter(self, mock_manifest_processor):
        """Test bulk delete with status filter."""
        mock_manifest_processor.list_jobs = AsyncMock(return_value=[])

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.request(
                    "DELETE",
                    "/api/v1/jobs",
                    json={"status": "completed", "dry_run": True},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 200

    def test_bulk_delete_jobs_server_error(self, mock_manifest_processor):
        """Test bulk delete with server error returns 500."""
        mock_manifest_processor.list_jobs = AsyncMock(side_effect=Exception("Database error"))

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.request(
                    "DELETE",
                    "/api/v1/jobs",
                    json={"dry_run": True},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 500


# =============================================================================
# Retry Job Endpoint Tests
# =============================================================================


class TestRetryJobEndpoint:
    """Tests for POST /api/v1/jobs/{namespace}/{name}/retry endpoint."""

    def test_retry_job_success(self, mock_manifest_processor):
        """Test retrying a job returns success."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        # The original Job, read raw (_preload_content=False) as the API server sent it
        original = {
            "metadata": {"name": "test-job", "namespace": "default", "labels": {"app": "test"}},
            "spec": {
                "backoffLimit": 6,
                "template": {
                    "spec": {
                        "containers": [{"name": "main", "image": "test:latest"}],
                        "restartPolicy": "Never",
                    }
                },
            },
        }
        mock_manifest_processor.batch_v1.read_namespaced_job.return_value = MagicMock(
            data=json.dumps(original).encode()
        )

        # Mock submission response
        mock_response = ManifestSubmissionResponse(
            success=True,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="batch/v1",
                    kind="Job",
                    name="test-job-retry-20240101000000",
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
                response = client.post("/api/v1/jobs/default/test-job/retry", headers=_AUTH_HEADERS)
                assert response.status_code == 201
                data = response.json()
                assert data["original_job"] == "test-job"
                assert data["success"] is True

    def test_retry_job_not_found(self, mock_manifest_processor):
        """Test retrying non-existent job returns 404."""
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
                response = client.post(
                    "/api/v1/jobs/default/nonexistent/retry", headers=_AUTH_HEADERS
                )
                assert response.status_code == 404

    def test_retry_job_disallowed_namespace(self, mock_manifest_processor):
        """Test retrying job from disallowed namespace returns 403."""
        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/jobs/kube-system/test-job/retry", headers=_AUTH_HEADERS
                )
                assert response.status_code == 403


_RETRY_UID = "6f1c2c1e-3d0a-4b8e-9f3c-2b7e4f6a9d10"
_RETRY_NAME = re.compile(r"trainer-retry-\d{14}")


def _failed_job_object() -> dict[str, Any]:
    """A failed Job exactly as the API server returns it from a GET.

    It carries everything the server adds: identity metadata, managedFields,
    defaulted spec fields, status, and the generated selector plus the
    controller-uid/job-name labels on the pod template. Every field is one the
    pinned kubernetes client's models define, so the round-trip check below
    can tell a real field from a misspelled one.
    """
    controller_labels = {
        "batch.kubernetes.io/controller-uid": _RETRY_UID,
        "batch.kubernetes.io/job-name": "trainer",
        "controller-uid": _RETRY_UID,
        "job-name": "trainer",
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "trainer",
            "namespace": "gco-jobs",
            "uid": _RETRY_UID,
            "resourceVersion": "48211",
            "generation": 1,
            "creationTimestamp": "2026-10-08T21:04:11Z",
            "labels": {"app": "ml-training", "kueue.x-k8s.io/queue-name": "user-queue"},
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": '{"kind":"Job"}\n',
                "team": "research",
            },
            "managedFields": [
                {
                    "manager": "kube-controller-manager",
                    "operation": "Update",
                    "apiVersion": "batch/v1",
                    "time": "2026-10-08T21:05:02Z",
                    "fieldsType": "FieldsV1",
                    "fieldsV1": {"f:status": {}},
                    "subresource": "status",
                }
            ],
        },
        "spec": {
            "parallelism": 1,
            "completions": 1,
            "backoffLimit": 0,
            "selector": {"matchLabels": {"batch.kubernetes.io/controller-uid": _RETRY_UID}},
            "template": {
                "metadata": {
                    "creationTimestamp": None,
                    "labels": {"app": "ml-training", **controller_labels},
                },
                "spec": {
                    "containers": [
                        {
                            "name": "trainer",
                            "image": "python:3.14.8-slim",
                            "command": ["python", "-c", "raise SystemExit(1)"],
                            "resources": {
                                "limits": {"cpu": "1", "memory": "1Gi"},
                                "requests": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "terminationMessagePath": "/dev/termination-log",
                            "terminationMessagePolicy": "File",
                            "imagePullPolicy": "IfNotPresent",
                            "securityContext": {
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                            },
                        }
                    ],
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 30,
                    "dnsPolicy": "ClusterFirst",
                    "automountServiceAccountToken": False,
                    "securityContext": {"runAsUser": 1000, "runAsNonRoot": True},
                    "schedulerName": "default-scheduler",
                },
            },
            "completionMode": "NonIndexed",
            "suspend": False,
            "podReplacementPolicy": "TerminatingOrFailed",
            "podFailurePolicy": {
                "rules": [
                    {
                        "action": "Ignore",
                        "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                    }
                ]
            },
            "ttlSecondsAfterFinished": 3600,
        },
        "status": {
            "conditions": [
                {
                    "type": "Failed",
                    "status": "True",
                    "lastProbeTime": "2026-10-08T21:05:02Z",
                    "lastTransitionTime": "2026-10-08T21:05:02Z",
                    "reason": "BackoffLimitExceeded",
                    "message": "Job has reached the specified backoff limit",
                }
            ],
            "startTime": "2026-10-08T21:04:11Z",
            "failed": 1,
            "terminating": 0,
            "ready": 0,
            "uncountedTerminatedPods": {},
        },
    }


async def _retry(original: dict[str, Any], name: str = "trainer") -> tuple[Any, dict[str, Any]]:
    """Run the retry route against ``original``; return the response and the new Job."""
    import gco.services.api_routes.jobs as routes
    from gco.models import ManifestSubmissionResponse

    processor = MagicMock(cluster_id="cluster", region="us-east-1")
    processor.batch_v1.read_namespaced_job.return_value = MagicMock(
        data=json.dumps(original).encode()
    )
    processor.process_manifest_submission = AsyncMock(
        return_value=ManifestSubmissionResponse(True, "cluster", "us-east-1", [])
    )
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.retry_job("gco-jobs", name)

    read = processor.batch_v1.read_namespaced_job
    read.assert_called_once_with(name=name, namespace="gco-jobs", _preload_content=False)
    read.return_value.release_conn.assert_called_once_with()
    (request,) = processor.process_manifest_submission.call_args.args
    assert request.validate is True
    assert request.dry_run is False
    (manifest,) = request.manifests
    return response, manifest


class TestRetryJobManifest:
    """The retry route copies the Job the API server holds into a Job it accepts.

    It used to rebuild the template from the typed model's ``to_dict()``,
    whose Python field names (``restart_policy``) the API server does not
    know, and it kept the labels that bind pods to the original Job's UID.
    """

    async def test_copies_the_spec_and_drops_what_the_server_generated(self):
        original = _failed_job_object()
        response, manifest = await _retry(original)

        assert response.status_code == 201
        new_name = manifest["metadata"]["name"]
        assert _RETRY_NAME.fullmatch(new_name)
        assert json.loads(response.body)["new_job"] == new_name
        expected_spec = copy.deepcopy(original["spec"])
        del expected_spec["selector"]
        del expected_spec["suspend"]
        expected_spec["template"]["metadata"] = {"labels": {"app": "ml-training"}}
        assert manifest == {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": new_name,
                "namespace": "gco-jobs",
                "labels": {
                    "app": "ml-training",
                    "kueue.x-k8s.io/queue-name": "user-queue",
                    "gco.io/retry-of": "trainer",
                },
                "annotations": {"team": "research", "gco.io/original-job": "trainer"},
            },
            "spec": expected_spec,
        }

    async def test_every_field_is_one_the_job_api_defines(self):
        # Deserializing through the client's V1Job model keeps only the fields
        # the Job API defines, so a misspelled field (restart_policy) vanishes
        # from the round trip and the comparison fails.
        _, manifest = await _retry(_failed_job_object())
        with ApiClient() as api:
            model = api.deserialize(SimpleNamespace(data=json.dumps(manifest)), "V1Job")
            assert api.sanitize_for_serialization(model) == manifest

    async def test_controller_labels_copied_onto_an_unlabeled_job_are_dropped(self):
        # A Job created without labels gets its template's labels, generated
        # ones included, as its own.
        original = _failed_job_object()
        original["metadata"]["labels"] = dict(original["spec"]["template"]["metadata"]["labels"])
        _, manifest = await _retry(original)
        assert manifest["metadata"]["labels"] == {
            "app": "ml-training",
            "gco.io/retry-of": "trainer",
        }

    async def test_a_manual_selector_and_its_labels_are_kept(self):
        original = _failed_job_object()
        original["metadata"]["labels"]["job-name"] = "trainer"
        original["spec"]["manualSelector"] = True
        original["spec"]["selector"] = {"matchLabels": {"run": "trainer-a"}}
        original["spec"]["template"]["metadata"]["labels"] = {
            "run": "trainer-a",
            "job-name": "trainer",
        }
        _, manifest = await _retry(original)
        assert manifest["metadata"]["labels"]["job-name"] == "trainer"
        assert manifest["spec"]["manualSelector"] is True
        assert manifest["spec"]["selector"] == {"matchLabels": {"run": "trainer-a"}}
        assert manifest["spec"]["template"]["metadata"]["labels"] == {
            "run": "trainer-a",
            "job-name": "trainer",
        }

    async def test_a_template_without_metadata_is_copied_unchanged(self):
        original = _failed_job_object()
        del original["spec"]["template"]["metadata"]
        _, manifest = await _retry(original)
        assert manifest["spec"]["template"] == original["spec"]["template"]

    def test_the_retry_name_fits_the_job_name_limit(self):
        from gco.services.api_routes.jobs import _retry_job_name

        now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
        assert _retry_job_name("trainer", now) == "trainer-retry-20261009120000"
        # 63 characters, the longest name a Job can have.
        longest = "a" * 40 + "-" + "b" * 22
        assert _retry_job_name(longest, now) == "a" * 40 + "-b-retry-20261009120000"
        assert len(_retry_job_name(longest, now)) == 63
        # A cut that lands on a separator does not leave it dangling.
        assert _retry_job_name("a" * 41 + "-" + "b" * 21, now) == "a" * 41 + "-retry-20261009120000"

    async def test_a_missing_job_is_404(self):
        from fastapi import HTTPException

        import gco.services.api_routes.jobs as routes

        processor = MagicMock()
        processor.batch_v1.read_namespaced_job.side_effect = ApiException(
            status=404, reason="Not Found"
        )
        with (
            patch.object(routes, "_check_processor", return_value=processor),
            patch.object(routes, "_check_namespace"),
            pytest.raises(HTTPException) as excinfo,
        ):
            await routes.retry_job("gco-jobs", "trainer")
        assert excinfo.value.status_code == 404
        processor.process_manifest_submission.assert_not_called()

    def test_a_real_api_server_accepts_the_copy_in_ci(self):
        # The doubles above cannot say what an API server accepts, so
        # integration:kind:cluster-e2e retries a real Job through this route
        # inside a manifest-processor pod and checks the Job it creates.
        from pathlib import Path

        import yaml

        workflow_path = (
            Path(__file__).resolve().parents[1] / ".github/workflows/integration-tests.yml"
        )
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["integration-kind-cluster-e2e"]["steps"]
        names = [step.get("name") for step in steps]
        name = "Retry the applied Job through the jobs API route"
        run = steps[names.index(name)]["run"]
        assert names.index(
            "Verify the submitted job was applied and the message acknowledged"
        ) < names.index(name)
        assert "exec -i deploy/manifest-processor -c manifest-processor" in run
        assert 'routes.retry_job("gco-jobs", "sqs-submitted-job")' in run
        assert '"batch.kubernetes.io/controller-uid": uid' in run
        assert 'pod_spec["restartPolicy"] == "Never"' in run
        assert "batch.kubernetes.io/job-name=${new_job}" in run

    async def test_a_payload_that_is_not_an_object_is_a_server_error(self):
        from fastapi import HTTPException

        import gco.services.api_routes.jobs as routes

        processor = MagicMock()
        # A response without release_conn, as a test double or another client may return.
        processor.batch_v1.read_namespaced_job.return_value = SimpleNamespace(data=b"[]")
        with (
            patch.object(routes, "_check_processor", return_value=processor),
            patch.object(routes, "_check_namespace"),
            pytest.raises(HTTPException) as excinfo,
        ):
            await routes.retry_job("gco-jobs", "trainer")
        assert excinfo.value.status_code == 500
        processor.process_manifest_submission.assert_not_called()


# =============================================================================
# Templates Endpoint Tests
# =============================================================================


class TestTemplatesEndpoints:
    """Tests for /api/v1/templates endpoints."""

    def test_list_templates_empty(self, mock_manifest_processor):
        """Test listing templates when none exist."""
        mock_template_store = MagicMock()
        mock_template_store.list_templates.return_value = []

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
                assert response.status_code == 200
                data = response.json()
                assert data["count"] == 0
                assert data["templates"] == []

    def test_create_template_success(self, mock_manifest_processor):
        """Test creating a template returns success."""
        mock_template_store = MagicMock()
        mock_template_store.create_template.return_value = {
            "name": "test-template",
            "description": "A test template",
            "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
            "parameters": {"image": "test:latest"},
            "created_at": "2024-01-01T00:00:00Z",
        }

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
                response = client.post(
                    "/api/v1/templates",
                    json={
                        "name": "test-template",
                        "description": "A test template",
                        "manifest": {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "metadata": {"name": "{{name}}"},
                            "spec": {"template": {"spec": {"containers": []}}},
                        },
                        "parameters": {"image": "test:latest"},
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 201
                data = response.json()
                assert data["template"]["name"] == "test-template"

    def test_create_template_duplicate(self, mock_manifest_processor):
        """Test creating duplicate template returns 409."""
        mock_template_store = MagicMock()
        mock_template_store.create_template.side_effect = ValueError(
            "Template 'existing-template' already exists"
        )

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
                response = client.post(
                    "/api/v1/templates",
                    json={
                        "name": "existing-template",
                        "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 409

    def test_get_template_success(self, mock_manifest_processor):
        """Test getting a template returns success."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = {
            "name": "my-template",
            "description": "Test",
            "manifest": {},
            "parameters": {},
        }

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
                response = client.get("/api/v1/templates/my-template", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["template"]["name"] == "my-template"

    def test_get_template_not_found(self, mock_manifest_processor):
        """Test getting non-existent template returns 404."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = None

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
                response = client.get("/api/v1/templates/nonexistent", headers=_AUTH_HEADERS)
                assert response.status_code == 404

    def test_delete_template_success(self, mock_manifest_processor):
        """Test deleting a template returns success."""
        mock_template_store = MagicMock()
        mock_template_store.delete_template.return_value = True

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
                response = client.delete("/api/v1/templates/to-delete", headers=_AUTH_HEADERS)
                assert response.status_code == 200

    def test_delete_template_not_found(self, mock_manifest_processor):
        """Test deleting non-existent template returns 404."""
        mock_template_store = MagicMock()
        mock_template_store.delete_template.return_value = False

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
                response = client.delete("/api/v1/templates/nonexistent", headers=_AUTH_HEADERS)
                assert response.status_code == 404


# =============================================================================
# Create Job From Template Endpoint Tests
# =============================================================================


class TestCreateJobFromTemplateEndpoint:
    """Tests for POST /api/v1/jobs/from-template/{name} endpoint."""

    def test_create_job_from_template_success(self, mock_manifest_processor):
        """Test creating job from template returns success."""
        from gco.models import ManifestSubmissionResponse, ResourceStatus

        mock_response = ManifestSubmissionResponse(
            success=True,
            cluster_id="test-cluster",
            region="us-east-1",
            resources=[
                ResourceStatus(
                    api_version="batch/v1",
                    kind="Job",
                    name="my-job",
                    namespace="default",
                    status="created",
                )
            ],
        )
        mock_manifest_processor.process_manifest_submission = AsyncMock(return_value=mock_response)

        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = {
            "name": "gpu-template",
            "manifest": {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "{{name}}"},
                "spec": {
                    "template": {"spec": {"containers": [{"name": "main", "image": "{{image}}"}]}}
                },
            },
            "parameters": {"image": "default:latest"},
        }

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
                response = client.post(
                    "/api/v1/jobs/from-template/gpu-template",
                    json={
                        "name": "my-job",
                        "namespace": "default",
                        "parameters": {"image": "custom:v1"},
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 201
                data = response.json()
                assert data["job_name"] == "my-job"
                assert data["success"] is True

    def test_create_job_from_template_not_found(self, mock_manifest_processor):
        """Test creating job from non-existent template returns 404."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = None

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
                response = client.post(
                    "/api/v1/jobs/from-template/nonexistent",
                    json={"name": "my-job", "namespace": "default"},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 404

    def test_create_job_from_template_disallowed_namespace(self, mock_manifest_processor):
        """Test creating job from template in disallowed namespace returns 403."""
        mock_template_store = MagicMock()
        mock_template_store.get_template.return_value = {
            "name": "my-template",
            "manifest": {"apiVersion": "batch/v1", "kind": "Job"},
            "parameters": {},
        }

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
                response = client.post(
                    "/api/v1/jobs/from-template/my-template",
                    json={"name": "my-job", "namespace": "kube-system"},
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 403


# =============================================================================
# Webhooks Endpoint Tests
# =============================================================================


class TestWebhooksEndpoints:
    """Tests for /api/v1/webhooks endpoints."""

    def test_list_webhooks_empty(self, mock_manifest_processor):
        """Test listing webhooks when none exist."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.list_webhooks.return_value = []

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
                assert response.status_code == 200
                data = response.json()
                assert data["count"] == 0
                assert data["webhooks"] == []

    def test_create_webhook_success(self, mock_manifest_processor):
        """Test creating a webhook returns success."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.create_webhook.return_value = {
            "id": "abc123",
            "url": "https://example.com/webhook",
            "events": ["job.completed", "job.failed"],
            "namespace": "default",
            "created_at": "2024-01-01T00:00:00Z",
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
                return_value=mock_webhook_store,
            ),
            patch(
                "gco.services.manifest_api.get_job_store",
                return_value=MagicMock(),
            ),
            patch(
                "gco.services.api_routes.webhooks.validate_webhook_url",
                return_value=(True, None),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = mock_webhook_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/webhooks",
                    json={
                        "url": "https://example.com/webhook",
                        "events": ["job.completed", "job.failed"],
                        "namespace": "default",
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 201
                data = response.json()
                assert "webhook" in data
                assert data["webhook"]["url"] == "https://example.com/webhook"

    def test_create_webhook_rejects_invalid_url(self, mock_manifest_processor):
        """Regression: the create endpoint must reject URLs that fail
        webhook_dispatcher.validate_webhook_url (e.g. plain HTTP, private IPs,
        IMDS address), so misconfigured webhooks are refused at registration
        time rather than failing silently on every job event."""
        mock_webhook_store = MagicMock()

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
            patch(
                "gco.services.api_routes.webhooks.validate_webhook_url",
                return_value=(False, "URL scheme must be 'https'"),
            ),
        ):
            import gco.services.manifest_api as api_module

            api_module.webhook_store = mock_webhook_store

            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/webhooks",
                    json={
                        "url": "http://example.com/webhook",
                        "events": ["job.completed"],
                    },
                    headers=_AUTH_HEADERS,
                )
                assert response.status_code == 400, (
                    f"expected 400 for invalid URL, got {response.status_code}: {response.json()!r}"
                )
                assert "Invalid webhook URL" in response.text
                # Store must NOT be reached when validation rejects.
                mock_webhook_store.create_webhook.assert_not_called()

    def test_delete_webhook_success(self, mock_manifest_processor):
        """Test deleting a webhook returns success."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.delete_webhook.return_value = True

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
                assert response.status_code == 200

    def test_delete_webhook_not_found(self, mock_manifest_processor):
        """Test deleting non-existent webhook returns 404."""
        mock_webhook_store = MagicMock()
        mock_webhook_store.delete_webhook.return_value = False

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
                response = client.delete("/api/v1/webhooks/nonexistent", headers=_AUTH_HEADERS)
                assert response.status_code == 404


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_parse_pod_to_dict(self):
        """Test _parse_pod_to_dict helper function."""
        from gco.services.api_shared import _parse_pod_to_dict

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_pod.metadata.labels = {"app": "test"}
        mock_pod.metadata.uid = "pod-uid"
        mock_pod.spec.node_name = "node-1"
        mock_pod.spec.containers = [MagicMock(name="main", image="test:latest")]
        mock_pod.spec.init_containers = None
        mock_pod.status.phase = "Running"
        mock_pod.status.host_ip = "10.0.0.1"
        mock_pod.status.pod_ip = "10.0.1.1"
        mock_pod.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_pod.status.container_statuses = []
        mock_pod.status.init_container_statuses = []

        result = _parse_pod_to_dict(mock_pod)

        assert result["metadata"]["name"] == "test-pod"
        assert result["metadata"]["namespace"] == "default"
        assert result["status"]["phase"] == "Running"

    def test_parse_pod_to_dict_with_container_statuses(self):
        """Test _parse_pod_to_dict with container statuses."""
        from gco.services.api_shared import _parse_pod_to_dict

        mock_pod = MagicMock()
        mock_pod.metadata.name = "test-pod"
        mock_pod.metadata.namespace = "default"
        mock_pod.metadata.creation_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_pod.metadata.labels = {}
        mock_pod.metadata.uid = "pod-uid"
        mock_pod.spec.node_name = "node-1"
        mock_pod.spec.containers = [MagicMock(name="main", image="test:latest")]
        mock_pod.spec.init_containers = []
        mock_pod.status.phase = "Running"
        mock_pod.status.host_ip = "10.0.0.1"
        mock_pod.status.pod_ip = "10.0.1.1"
        mock_pod.status.start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

        # Mock container status
        mock_cs = MagicMock()
        mock_cs.name = "main"
        mock_cs.ready = True
        mock_cs.restart_count = 0
        mock_cs.image = "test:latest"
        mock_cs.state.running = MagicMock()
        mock_cs.state.running.started_at = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_cs.state.waiting = None
        mock_cs.state.terminated = None
        mock_pod.status.container_statuses = [mock_cs]
        mock_pod.status.init_container_statuses = []

        result = _parse_pod_to_dict(mock_pod)

        assert len(result["status"]["containerStatuses"]) == 1
        assert result["status"]["containerStatuses"][0]["name"] == "main"
        assert result["status"]["containerStatuses"][0]["state"] == "running"

    def test_parse_event_to_dict(self):
        """Test _parse_event_to_dict helper function."""
        from gco.services.api_shared import _parse_event_to_dict

        mock_event = MagicMock()
        mock_event.type = "Normal"
        mock_event.reason = "Created"
        mock_event.message = "Created pod"
        mock_event.count = 1
        mock_event.first_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_event.last_timestamp = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        mock_event.source.component = "kubelet"
        mock_event.source.host = "node-1"
        mock_event.involved_object.kind = "Pod"
        mock_event.involved_object.name = "test-pod"
        mock_event.involved_object.namespace = "default"

        result = _parse_event_to_dict(mock_event)

        assert result["type"] == "Normal"
        assert result["reason"] == "Created"
        assert result["message"] == "Created pod"

    def test_apply_template_parameters(self):
        """Test _apply_template_parameters helper function."""
        from gco.services.api_shared import _apply_template_parameters

        manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "{{name}}"},
            "spec": {
                "template": {"spec": {"containers": [{"name": "main", "image": "{{image}}"}]}}
            },
        }

        parameters = {"name": "my-job", "image": "pytorch:latest"}

        result = _apply_template_parameters(manifest, parameters)

        assert result["metadata"]["name"] == "my-job"
        assert result["spec"]["template"]["spec"]["containers"][0]["image"] == "pytorch:latest"


# =============================================================================
# Pagination Tests
# =============================================================================


class TestListJobsPagination:
    """Tests for job listing pagination."""

    def test_list_jobs_with_pagination(self, mock_manifest_processor):
        """Test listing jobs with pagination parameters."""
        mock_manifest_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": f"job-{i}",
                        "namespace": "default",
                        "creationTimestamp": f"2024-01-0{i + 1}T00:00:00Z",
                        "labels": {},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
                }
                for i in range(5)
            ]
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/jobs?limit=2&offset=0", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                assert data["total"] == 5
                assert data["limit"] == 2
                assert data["offset"] == 0
                assert data["has_more"] is True
                assert data["count"] == 2

    def test_list_jobs_with_sort(self, mock_manifest_processor):
        """Test listing jobs with sort parameter."""
        mock_manifest_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "job-a",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-01T00:00:00Z",
                        "labels": {},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
                },
                {
                    "metadata": {
                        "name": "job-b",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-02T00:00:00Z",
                        "labels": {},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
                },
            ]
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/jobs?sort=name:asc", headers=_AUTH_HEADERS)
                assert response.status_code == 200

    def test_list_jobs_with_label_selector(self, mock_manifest_processor):
        """Test listing jobs with label selector."""
        mock_manifest_processor.list_jobs = AsyncMock(
            return_value=[
                {
                    "metadata": {
                        "name": "job-1",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-01T00:00:00Z",
                        "labels": {"app": "test"},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
                },
                {
                    "metadata": {
                        "name": "job-2",
                        "namespace": "default",
                        "creationTimestamp": "2024-01-02T00:00:00Z",
                        "labels": {"app": "other"},
                    },
                    "status": {"active": 0, "succeeded": 1, "failed": 0},
                },
            ]
        )

        with patch(
            "gco.services.manifest_api.create_manifest_processor_from_env",
            return_value=mock_manifest_processor,
        ):
            from fastapi.testclient import TestClient

            from gco.services.manifest_api import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/jobs?label_selector=app=test", headers=_AUTH_HEADERS)
                assert response.status_code == 200
                data = response.json()
                # Should filter to only jobs with app=test label
                assert data["count"] == 1


# =============================================================================
# Route Existence Tests
# =============================================================================


class TestRouteExistence:
    """Tests to verify all new routes exist."""

    def test_app_has_new_routes(self):
        """Test app has all new routes."""
        from gco.services.manifest_api import app

        routes = [r.path for r in app.routes if hasattr(r, "path")] + [
            s.path
            for r in app.routes
            for s in getattr(getattr(r, "original_router", None), "routes", [])
            if hasattr(s, "path")
        ]

        # Job endpoints
        assert "/api/v1/jobs" in routes
        assert "/api/v1/jobs/{namespace}/{name}" in routes
        assert "/api/v1/jobs/{namespace}/{name}/logs" in routes
        assert "/api/v1/jobs/{namespace}/{name}/events" in routes
        assert "/api/v1/jobs/{namespace}/{name}/pods" in routes
        assert "/api/v1/jobs/{namespace}/{name}/pods/{pod_name}/logs" in routes
        assert "/api/v1/jobs/{namespace}/{name}/metrics" in routes
        assert "/api/v1/jobs/{namespace}/{name}/retry" in routes

        # Template endpoints
        assert "/api/v1/templates" in routes
        assert "/api/v1/templates/{name}" in routes
        assert "/api/v1/jobs/from-template/{name}" in routes

        # Webhook endpoints
        assert "/api/v1/webhooks" in routes
        assert "/api/v1/webhooks/{webhook_id}" in routes
