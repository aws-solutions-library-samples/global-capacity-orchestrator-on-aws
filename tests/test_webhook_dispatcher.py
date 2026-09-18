"""
Tests for gco/services/webhook_dispatcher.WebhookDispatcher.

Exercises the full job-to-webhook pipeline end-to-end against mocked
HTTP and DynamoDB surfaces. Verifies job status is computed correctly
from V1Job.status (running / succeeded / failed transitions), that the
dispatcher emits exactly one event per transition (no duplicates from
the state cache), that payloads are signed with the per-webhook HMAC
secret and the signature header matches the bytes that go on the wire,
and that httpx failures are retried with backoff before the delivery
is marked failed. Also covers the SSRF guard on outbound URLs (private
RFC 1918 addresses rejected, public hostnames accepted) and the
lightweight WebhookStore cache used to keep DynamoDB reads off the
hot loop.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import shutil
import socket
import ssl
import subprocess
import textwrap
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gco.services.template_store import WebhookStore
from gco.services.webhook_dispatcher import (
    BLOCKED_NETWORKS,
    JobStateCache,
    WebhookDeliveryResult,
    WebhookDispatcher,
    WebhookEvent,
    create_webhook_dispatcher_from_env,
    validate_webhook_url,
)


class TestJobStateCache:
    """Tests for JobStateCache."""

    def test_get_state_empty(self):
        """Test getting state from empty cache."""
        cache = JobStateCache()
        assert cache.get_state("job-123") is None

    def test_set_and_get_state(self):
        """Test setting and getting state."""
        cache = JobStateCache()
        previous = cache.set_state("job-123", "running")
        assert previous is None
        assert cache.get_state("job-123") == "running"

    def test_set_state_returns_previous(self):
        """Test that set_state returns previous state."""
        cache = JobStateCache()
        cache.set_state("job-123", "pending")
        previous = cache.set_state("job-123", "running")
        assert previous == "pending"
        assert cache.get_state("job-123") == "running"

    def test_remove_state(self):
        """Test removing state from cache."""
        cache = JobStateCache()
        cache.set_state("job-123", "running")
        cache.remove("job-123")
        assert cache.get_state("job-123") is None

    def test_remove_nonexistent(self):
        """Test removing nonexistent state doesn't raise."""
        cache = JobStateCache()
        cache.remove("nonexistent")  # Should not raise


class TestWebhookDispatcher:
    """Tests for WebhookDispatcher."""

    @pytest.fixture
    def mock_k8s_config(self):
        """Mock Kubernetes configuration."""
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception("Not in cluster")
            mock_config.load_kube_config.return_value = None
            yield mock_config

    @pytest.fixture
    def mock_webhook_store(self):
        """Mock WebhookStore."""
        store = MagicMock()
        store.get_webhooks_for_event.return_value = []
        return store

    @pytest.fixture
    def dispatcher(self, mock_k8s_config, mock_webhook_store):
        """Create a WebhookDispatcher instance."""
        with patch("gco.services.webhook_dispatcher.client"):
            return WebhookDispatcher(
                cluster_id="test-cluster",
                region="us-east-1",
                webhook_store=mock_webhook_store,
                timeout=5,
                max_retries=2,
                retry_delay=1,
                namespaces=["gco-jobs", "default"],
            )

    def test_compute_job_status_succeeded(self, dispatcher):
        """Test computing succeeded job status."""
        job = MagicMock()
        job.status.conditions = [
            MagicMock(type="Complete", status="True"),
        ]
        job.status.active = 0
        job.status.succeeded = 1
        job.status.failed = 0

        assert dispatcher._compute_job_status(job) == "succeeded"

    def test_compute_job_status_failed(self, dispatcher):
        """Test computing failed job status."""
        job = MagicMock()
        job.status.conditions = [
            MagicMock(type="Failed", status="True"),
        ]
        job.status.active = 0
        job.status.succeeded = 0
        job.status.failed = 1

        assert dispatcher._compute_job_status(job) == "failed"

    def test_compute_job_status_running(self, dispatcher):
        """Test computing running job status."""
        job = MagicMock()
        job.status.conditions = []
        job.status.active = 1
        job.status.succeeded = 0
        job.status.failed = 0

        assert dispatcher._compute_job_status(job) == "running"

    def test_compute_job_status_pending(self, dispatcher):
        """Test computing pending job status."""
        job = MagicMock()
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 0
        job.status.failed = 0

        assert dispatcher._compute_job_status(job) == "pending"

    def test_determine_event_new_running(self, dispatcher):
        """Test event detection for new running job."""
        event = dispatcher._determine_event(None, "running")
        assert event == WebhookEvent.JOB_STARTED

    def test_determine_event_pending_to_running(self, dispatcher):
        """Test event detection for pending to running transition."""
        event = dispatcher._determine_event("pending", "running")
        assert event == WebhookEvent.JOB_STARTED

    def test_determine_event_running_to_succeeded(self, dispatcher):
        """Test event detection for running to succeeded transition."""
        event = dispatcher._determine_event("running", "succeeded")
        assert event == WebhookEvent.JOB_COMPLETED

    def test_determine_event_running_to_failed(self, dispatcher):
        """Test event detection for running to failed transition."""
        event = dispatcher._determine_event("running", "failed")
        assert event == WebhookEvent.JOB_FAILED

    def test_determine_event_no_change(self, dispatcher):
        """Test no event for same status."""
        event = dispatcher._determine_event("running", "running")
        assert event is None

    def test_determine_event_new_pending(self, dispatcher):
        """Test no event for new pending job."""
        event = dispatcher._determine_event(None, "pending")
        assert event is None

    def test_build_payload(self, dispatcher):
        """Test building webhook payload."""
        job = MagicMock()
        job.metadata.name = "test-job"
        job.metadata.namespace = "gco-jobs"
        job.metadata.uid = "job-uid-123"
        job.metadata.labels = {"app": "test"}
        job.status.conditions = []
        job.status.active = 1
        job.status.succeeded = 0
        job.status.failed = 0
        job.status.start_time = datetime(2026, 2, 4, 12, 0, 0, tzinfo=UTC)
        job.status.completion_time = None

        payload = dispatcher._build_payload(WebhookEvent.JOB_STARTED, job)

        assert payload["event"] == "job.started"
        assert payload["cluster_id"] == "test-cluster"
        assert payload["region"] == "us-east-1"
        assert payload["job"]["name"] == "test-job"
        assert payload["job"]["namespace"] == "gco-jobs"
        assert payload["job"]["uid"] == "job-uid-123"
        assert payload["job"]["status"] == "running"
        assert payload["job"]["labels"] == {"app": "test"}

    def test_sign_payload(self, dispatcher):
        """Test HMAC payload signing."""
        payload = '{"event": "job.completed"}'
        secret = "my-secret-key"  # nosec B105  # test fixture for HMAC signing test, not a real credential

        signature = dispatcher._sign_payload(payload, secret)

        # Verify signature format
        assert signature.startswith("sha256=")

        # Verify signature is correct
        expected = hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert signature == f"sha256={expected}"

    @pytest.mark.asyncio
    async def test_deliver_webhook_success(self, dispatcher):
        """Test successful webhook delivery."""
        webhook = {
            "id": "webhook-123",
            "url": "https://example.com/webhook",
            "secret": None,
        }
        payload = {"event": "job.completed", "job": {"name": "test"}}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is True
        assert result.status_code == 200
        assert result.webhook_id == "webhook-123"
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_deliver_webhook_with_signature(self, dispatcher):
        """Test webhook delivery with HMAC signature."""
        webhook = {
            "id": "webhook-123",
            "url": "https://example.com/webhook",
            "secret": "my-secret",
        }
        payload = {"event": "job.completed"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            await dispatcher._deliver_webhook(webhook, payload)

            # Verify signature header was included
            call_args = mock_client.post.call_args
            headers = call_args.kwargs["headers"]
            assert "X-GCO-Signature" in headers
            assert headers["X-GCO-Signature"].startswith("sha256=")

    @pytest.mark.asyncio
    async def test_dispatch_uses_secret_loaded_from_store(self, dispatcher):
        """The redacted public projection must not disable delivery signing."""
        table = MagicMock()
        table.query.return_value = {
            "Items": [
                {
                    "webhook_id": "wh-signed",
                    "url": "https://example.com/webhook",
                    "events": '["job.completed"]',
                    "namespace": "gco-jobs",
                    "secret": "stored-delivery-secret",
                }
            ]
        }
        table.scan.return_value = {"Items": []}
        with patch("gco.services.template_store.boto3.resource") as resource:
            resource.return_value.Table.return_value = table
            dispatcher.webhook_store = WebhookStore("webhooks", "us-east-1")

        job = MagicMock()
        job.metadata.namespace = "gco-jobs"
        job.metadata.name = "test-job"
        job.metadata.uid = "job-123"
        job.metadata.labels = {}
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 1
        job.status.failed = 0
        job.status.start_time = None
        job.status.completion_time = None

        with (
            patch(
                "gco.services.webhook_dispatcher.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.20", 443))],
            ),
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_client = AsyncMock()
            mock_response = MagicMock(status_code=200)
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)

        assert len(results) == 1
        call = mock_client.post.call_args
        payload = call.kwargs["content"]
        expected = hmac.new(
            b"stored-delivery-secret", payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert call.kwargs["headers"]["X-GCO-Signature"] == f"sha256={expected}"

    @pytest.mark.asyncio
    async def test_deliver_webhook_retry_on_500(self, dispatcher):
        """Test webhook delivery retries on 5xx errors."""
        webhook = {
            "id": "webhook-123",
            "url": "https://example.com/webhook",
            "secret": None,
        }
        payload = {"event": "job.completed"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            # First call fails with 500, second succeeds
            mock_response_500 = MagicMock()
            mock_response_500.status_code = 500
            mock_response_500.text = "Internal Server Error"
            mock_response_200 = MagicMock()
            mock_response_200.status_code = 200
            mock_client.post.side_effect = [mock_response_500, mock_response_200]
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is True
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_deliver_webhook_no_retry_on_400(self, dispatcher):
        """Test webhook delivery doesn't retry on 4xx errors."""
        webhook = {
            "id": "webhook-123",
            "url": "https://example.com/webhook",
            "secret": None,
        }
        payload = {"event": "job.completed"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.text = "Bad Request"
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is False
        assert result.status_code == 400
        assert result.attempts == 1  # No retry

    @pytest.mark.asyncio
    async def test_deliver_webhook_timeout(self, dispatcher):
        """Test webhook delivery handles timeout."""
        webhook = {
            "id": "webhook-123",
            "url": "https://example.com/webhook",
            "secret": None,
        }
        payload = {"event": "job.completed"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Timeout")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is False
        assert result.error == "Request timed out"
        assert result.attempts == 2  # Retried once

    @pytest.mark.asyncio
    async def test_dispatch_event_no_webhooks(self, dispatcher, mock_webhook_store):
        """Test dispatching event with no registered webhooks."""
        mock_webhook_store.get_webhooks_for_event.return_value = []

        job = MagicMock()
        job.metadata.namespace = "gco-jobs"
        job.metadata.name = "test-job"
        job.metadata.uid = "job-123"
        job.metadata.labels = {}
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 1
        job.status.failed = 0
        job.status.start_time = None
        job.status.completion_time = None

        results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)

        assert results == []

    @pytest.mark.asyncio
    async def test_dispatch_event_with_webhooks(self, dispatcher, mock_webhook_store):
        """Test dispatching event to multiple webhooks."""
        mock_webhook_store.get_webhooks_for_event.return_value = [
            {"id": "wh-1", "url": "https://example1.com/webhook", "namespace": "gco-jobs"},
            {"id": "wh-2", "url": "https://example2.com/webhook", "namespace": None},
        ]

        job = MagicMock()
        job.metadata.namespace = "gco-jobs"
        job.metadata.name = "test-job"
        job.metadata.uid = "job-123"
        job.metadata.labels = {}
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 1
        job.status.failed = 0
        job.status.start_time = None
        job.status.completion_time = None

        # Bypass the SSRF DNS-resolution gate so the test doesn't depend on
        # ``example1.com``/``example2.com`` resolving on the CI runner. The
        # dispatcher calls ``validate_webhook_url`` before any HTTP request,
        # and on hosted runners those bogus domains return ``gaierror``,
        # which marks the delivery as a validation failure before httpx is
        # ever touched.
        with (
            patch(
                "gco.services.webhook_dispatcher.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.20", 443))],
            ),
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)

        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_process_job_event_skip_system_namespace(self, dispatcher):
        """Test that system namespaces are skipped."""
        job = MagicMock()
        job.metadata.namespace = "kube-system"
        job.metadata.uid = "job-123"

        # Should not raise or process
        await dispatcher._process_job_event("MODIFIED", job)

        # Verify no state was cached
        assert dispatcher._job_state_cache.get_state("job-123") is None

    @pytest.mark.asyncio
    async def test_process_job_event_deleted(self, dispatcher):
        """Test processing deleted job event."""
        job = MagicMock()
        job.metadata.namespace = "gco-jobs"
        job.metadata.uid = "job-123"
        job.metadata.name = "test-job"
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 0
        job.status.failed = 0

        # Pre-populate cache
        dispatcher._job_state_cache.set_state("job-123", "running")

        await dispatcher._process_job_event("DELETED", job)

        # Verify state was removed
        assert dispatcher._job_state_cache.get_state("job-123") is None

    def test_get_metrics(self, dispatcher):
        """Test getting dispatcher metrics."""
        dispatcher._deliveries_total = 10
        dispatcher._deliveries_success = 8
        dispatcher._deliveries_failed = 2
        dispatcher._running = True

        metrics = dispatcher.get_metrics()

        assert metrics["deliveries_total"] == 10
        assert metrics["deliveries_success"] == 8
        assert metrics["deliveries_failed"] == 2
        assert metrics["running"] is True


class TestCreateWebhookDispatcherFromEnv:
    """Tests for create_webhook_dispatcher_from_env."""

    def test_create_from_env_defaults(self):
        """Test creating dispatcher with default env values."""
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None

            with (
                patch("gco.services.webhook_dispatcher.client"),
                patch.dict("os.environ", {}, clear=True),
            ):
                dispatcher = create_webhook_dispatcher_from_env()

        assert dispatcher.cluster_id == "unknown-cluster"
        assert dispatcher.region == "unknown-region"
        assert dispatcher.timeout == 30
        assert dispatcher.max_retries == 3
        assert dispatcher.retry_delay == 5

    def test_create_from_env_custom(self):
        """Test creating dispatcher with custom env values."""
        env = {
            "CLUSTER_NAME": "my-cluster",
            "REGION": "eu-west-1",
            "WEBHOOK_TIMEOUT": "60",
            "WEBHOOK_MAX_RETRIES": "5",
            "WEBHOOK_RETRY_DELAY": "10",
            "ALLOWED_NAMESPACES": "ns1,ns2,ns3",
        }

        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None

            with (
                patch("gco.services.webhook_dispatcher.client"),
                patch.dict("os.environ", env, clear=True),
            ):
                dispatcher = create_webhook_dispatcher_from_env()

        assert dispatcher.cluster_id == "my-cluster"
        assert dispatcher.region == "eu-west-1"
        assert dispatcher.timeout == 60
        assert dispatcher.max_retries == 5
        assert dispatcher.retry_delay == 10
        assert dispatcher.namespaces == ["ns1", "ns2", "ns3"]


class TestWebhookEventEnum:
    """Tests for WebhookEvent enum."""

    def test_event_values(self):
        """Test webhook event values."""
        assert WebhookEvent.JOB_STARTED.value == "job.started"
        assert WebhookEvent.JOB_COMPLETED.value == "job.completed"
        assert WebhookEvent.JOB_FAILED.value == "job.failed"


class TestWebhookDeliveryResult:
    """Tests for WebhookDeliveryResult dataclass."""

    def test_create_success_result(self):
        """Test creating a successful delivery result."""
        result = WebhookDeliveryResult(
            webhook_id="wh-123",
            url="https://example.com/webhook",
            event="job.completed",
            success=True,
            status_code=200,
            attempts=1,
            duration_ms=150.5,
        )

        assert result.webhook_id == "wh-123"
        assert result.success is True
        assert result.status_code == 200
        assert result.error is None

    def test_create_failure_result(self):
        """Test creating a failed delivery result."""
        result = WebhookDeliveryResult(
            webhook_id="wh-123",
            url="https://example.com/webhook",
            event="job.completed",
            success=False,
            status_code=500,
            error="Internal Server Error",
            attempts=3,
            duration_ms=5000.0,
        )

        assert result.success is False
        assert result.error == "Internal Server Error"
        assert result.attempts == 3


class TestWebhookDispatcherInitEdgeCases:
    """Tests for WebhookDispatcher __init__ edge cases."""

    def test_init_incluster_config_success(self):
        """Test that in-cluster config is used when available."""
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.return_value = None  # succeeds
            with patch("gco.services.webhook_dispatcher.client"):
                store = MagicMock()
                WebhookDispatcher(cluster_id="c", region="r", webhook_store=store)
                mock_config.load_incluster_config.assert_called_once()
                mock_config.load_kube_config.assert_not_called()

    def test_init_both_configs_fail(self):
        """Test that exception is raised when both k8s configs fail."""
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception("no cluster")
            mock_config.load_kube_config.side_effect = Exception("no kubeconfig")
            with (
                patch("gco.services.webhook_dispatcher.client"),
                pytest.raises(Exception, match="no kubeconfig"),
            ):
                WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                )


class TestComputeJobStatusFallbacks:
    """Tests for _compute_job_status fallback paths (no conditions)."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                )

    def test_succeeded_without_conditions(self, dispatcher):
        """Test succeeded status via status.succeeded when no conditions."""
        job = MagicMock()
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 2
        job.status.failed = 0
        assert dispatcher._compute_job_status(job) == "succeeded"

    def test_failed_without_conditions(self, dispatcher):
        """Test failed status via status.failed when no conditions."""
        job = MagicMock()
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 0
        job.status.failed = 3
        assert dispatcher._compute_job_status(job) == "failed"

    def test_conditions_none(self, dispatcher):
        """Test when conditions is None (not empty list)."""
        job = MagicMock()
        job.status.conditions = None
        job.status.active = 0
        job.status.succeeded = 0
        job.status.failed = 0
        assert dispatcher._compute_job_status(job) == "pending"


class TestDetermineEventEdgeCases:
    """Tests for _determine_event edge cases."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                )

    def test_new_job_succeeded_directly(self, dispatcher):
        """New job that is already succeeded fires no event."""
        assert dispatcher._determine_event(None, "succeeded") is None

    def test_new_job_failed_directly(self, dispatcher):
        """New job that is already failed fires no event."""
        assert dispatcher._determine_event(None, "failed") is None

    def test_pending_to_failed(self, dispatcher):
        """Pending to failed fires JOB_FAILED."""
        assert dispatcher._determine_event("pending", "failed") == WebhookEvent.JOB_FAILED

    def test_pending_to_succeeded(self, dispatcher):
        """Pending to succeeded fires JOB_COMPLETED."""
        assert dispatcher._determine_event("pending", "succeeded") == WebhookEvent.JOB_COMPLETED

    def test_succeeded_to_failed_no_event(self, dispatcher):
        """Succeeded to failed is not a recognized transition."""
        assert dispatcher._determine_event("succeeded", "failed") is None

    def test_failed_to_running_no_event(self, dispatcher):
        """Failed to running is not a recognized transition."""
        assert dispatcher._determine_event("failed", "running") is None


class TestDeliverWebhookEdgeCases:
    """Tests for _deliver_webhook error handling and edge cases."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                    timeout=5,
                    max_retries=2,
                    retry_delay=0,
                )

    @pytest.mark.asyncio
    async def test_request_error_retries_then_fails(self, dispatcher):
        """Test that RequestError triggers retries and eventually fails."""
        webhook = {"id": "wh-1", "url": "https://bad.host/hook", "secret": None}
        payload = {"event": "job.started"}

        # Mock DNS resolution to return a public IP so URL validation passes
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.1", 443))]
        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=fake_addrinfo),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_cls.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is False
        assert result.error == "ConnectError"
        assert result.attempts == 2
        assert dispatcher._deliveries_failed == 1
        assert dispatcher._deliveries_total == 1

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_on_500(self, dispatcher):
        """Test that all retries exhausted on persistent 500 returns failure."""
        webhook = {"id": "wh-1", "url": "https://example.com/hook", "secret": None}
        payload = {"event": "job.failed"}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            resp = MagicMock()
            resp.status_code = 503
            resp.text = "Service Unavailable"
            mock_client.post.return_value = resp
            mock_cls.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is False
        assert result.status_code == 503
        assert result.attempts == 2
        assert "503" in result.error

    @pytest.mark.asyncio
    async def test_timeout_then_success(self, dispatcher):
        """Test timeout on first attempt then success on retry."""
        webhook = {"id": "wh-1", "url": "https://example.com/hook", "secret": None}
        payload = {"event": "job.completed"}

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            ok_resp = MagicMock()
            ok_resp.status_code = 200
            mock_client.post.side_effect = [
                httpx.TimeoutException("timed out"),
                ok_resp,
            ]
            mock_cls.return_value.__aenter__.return_value = mock_client

            result = await dispatcher._deliver_webhook(webhook, payload)

        assert result.success is True
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_delivery_connects_only_to_validated_address_with_original_sni(
        self, dispatcher, caplog
    ):
        secret = "path-and-query-secret"
        webhook = {
            "id": "wh-pinned",
            "url": f"https://hooks.example/{secret}?token={secret}",
            "secret": None,
        }
        resolver = MagicMock(
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.10", 443))]
        )
        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", resolver),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            client = AsyncMock()
            client.post.return_value = MagicMock(status_code=204)
            mock_cls.return_value.__aenter__.return_value = client
            result = await dispatcher._deliver_webhook(webhook, {"event": "job.completed"})

        assert result.success is True
        resolver.assert_called_once_with("hooks.example", 443, proto=socket.IPPROTO_TCP)
        call = client.post.call_args
        assert call.args[0] == f"https://93.184.216.10:443/{secret}?token={secret}"
        assert call.kwargs["headers"]["Host"] == "hooks.example"
        assert call.kwargs["extensions"] == {"sni_hostname": "hooks.example"}
        assert secret not in caplog.text

    @pytest.mark.asyncio
    async def test_logs_redact_url_response_and_transport_secrets(self, dispatcher, caplog):
        secret = "reusable-webhook-secret"
        webhook = {
            "id": "wh-redacted",
            "url": f"https://hooks.example/{secret}?token={secret}",
            "secret": None,
        }
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.11", 443))]
        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=address),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            client = AsyncMock()
            client.post.side_effect = [
                MagicMock(status_code=503, text=f"remote {secret}"),
                httpx.ConnectError(f"transport {secret}"),
            ]
            mock_cls.return_value.__aenter__.return_value = client
            result = await dispatcher._deliver_webhook(webhook, {"event": "job.failed"})

        assert result.success is False
        assert secret not in caplog.text
        assert "wh-redacted" in caplog.text
        assert result.error == "ConnectError"

    @pytest.mark.asyncio
    async def test_pre_http_failures_are_counted_once(self, dispatcher, caplog):
        webhook = {"id": "wh-invalid", "url": "https://127.0.0.1/hook", "secret": None}
        result = await dispatcher._deliver_webhook(webhook, {"event": "job.started"})
        assert result.attempts == 0
        assert dispatcher.get_metrics()["deliveries_total"] == 1
        assert dispatcher.get_metrics()["deliveries_failed"] == 1

        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.12", 443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            side_effect=RuntimeError("resolver-secret"),
        ):
            result = await dispatcher._deliver_webhook(
                {"id": "wh-resolver", "url": "https://hooks.example/hook", "secret": None},
                {"event": "job.started"},
            )
        assert result.error == "Delivery failure: RuntimeError"

        with patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=public):
            result = await dispatcher._deliver_webhook(
                {"id": "wh-json", "url": "https://hooks.example/hook", "secret": None},
                {"event": "job.started", "invalid": object()},
            )
        assert result.error == "Delivery failure: TypeError"

        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=public),
            patch.object(
                dispatcher,
                "_sign_payload",
                side_effect=RuntimeError("signing-secret"),
            ),
        ):
            result = await dispatcher._deliver_webhook(
                {"id": "wh-sign", "url": "https://hooks.example/hook", "secret": "key"},
                {"event": "job.started"},
            )
        assert result.error == "Delivery failure: RuntimeError"

        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=public),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_cls.return_value.__aenter__.side_effect = RuntimeError("setup-secret")
            result = await dispatcher._deliver_webhook(
                {"id": "wh-client", "url": "https://hooks.example/hook", "secret": None},
                {"event": "job.started"},
            )
        assert result.success is False
        assert result.error == "Delivery failure: RuntimeError"
        assert dispatcher.get_metrics()["deliveries_total"] == 5
        assert dispatcher.get_metrics()["deliveries_failed"] == 5
        assert dispatcher.get_metrics()["deliveries_success"] == 0
        log_text = caplog.text
        assert "resolver-secret" not in log_text
        assert "signing-secret" not in log_text
        assert "setup-secret" not in log_text

    @pytest.mark.asyncio
    async def test_cancellation_is_propagated_and_accounted(self, dispatcher):
        entered_post = asyncio.Event()

        async def blocked_post(*_args, **_kwargs):
            entered_post.set()
            await asyncio.Event().wait()

        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.12", 443))]
        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=public),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            client = AsyncMock()
            client.post.side_effect = blocked_post
            mock_cls.return_value.__aenter__.return_value = client
            task = asyncio.create_task(
                dispatcher._deliver_webhook(
                    {"id": "wh-cancel", "url": "https://hooks.example/hook"},
                    {"event": "job.started"},
                )
            )
            await entered_post.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        metrics = dispatcher.get_metrics()
        assert metrics["deliveries_total"] == 1
        assert metrics["deliveries_success"] == 0
        assert metrics["deliveries_failed"] == 1

    @pytest.mark.asyncio
    async def test_cancellation_during_client_teardown_is_only_failure(self, dispatcher):
        entered_exit = asyncio.Event()

        class BlockingExitClient:
            async def __aenter__(self):
                return self

            async def post(self, *_args, **_kwargs):
                return MagicMock(status_code=204)

            async def __aexit__(self, *_args):
                entered_exit.set()
                await asyncio.Event().wait()

        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.12", 443))]
        with (
            patch("gco.services.webhook_dispatcher.socket.getaddrinfo", return_value=public),
            patch(
                "gco.services.webhook_dispatcher.httpx.AsyncClient",
                return_value=BlockingExitClient(),
            ),
        ):
            task = asyncio.create_task(
                dispatcher._deliver_webhook(
                    {"id": "wh-exit-cancel", "url": "https://hooks.example/hook"},
                    {"event": "job.completed"},
                )
            )
            await entered_exit.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        metrics = dispatcher.get_metrics()
        assert metrics["deliveries_total"] == 1
        assert metrics["deliveries_success"] == 0
        assert metrics["deliveries_failed"] == 1

    @pytest.mark.asyncio
    async def test_real_tls_transport_preserves_sni_host_and_rotates_addresses(
        self, dispatcher, tmp_path
    ):
        openssl = shutil.which("openssl")
        assert openssl is not None, "openssl is required for the local TLS contract"
        config_path = tmp_path / "openssl.cnf"
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        config_path.write_text(
            textwrap.dedent(
                """
                [req]
                prompt = no
                distinguished_name = dn
                x509_extensions = v3_req

                [dn]
                CN = hooks.example

                [v3_req]
                subjectAltName = DNS:hooks.example
                basicConstraints = critical,CA:TRUE
                keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
                extendedKeyUsage = serverAuth
                """
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-config",
                str(config_path),
                "-extensions",
                "v3_req",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        seen_sni: list[str | None] = []
        server_context.set_servername_callback(
            lambda _connection, server_name, _context: seen_sni.append(server_name)
        )
        requests: list[str] = []

        def handler(status: int):
            async def receive(reader, writer):
                request = await reader.readuntil(b"\r\n\r\n")
                requests.append(  # nosemgrep: python.django.security.injection.ssrf.ssrf-injection-requests.ssrf-injection-requests -- test-only loopback TLS server captures its own request
                    request.decode("ascii")
                )
                reason = "OK" if status == 200 else "Service Unavailable"
                body = b"ok"
                writer.write(
                    f"HTTP/1.1 {status} {reason}\r\n".encode()
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            return receive

        first = await asyncio.start_server(handler(503), "127.0.0.1", 0, ssl=server_context)
        port = first.sockets[0].getsockname()[1]
        second = await asyncio.start_server(handler(200), "::1", port, ssl=server_context)
        client_context = ssl.create_default_context(cafile=str(cert_path))
        real_async_client = httpx.AsyncClient

        def client_factory(**kwargs):
            return real_async_client(
                **kwargs,
                verify=client_context,
                trust_env=False,
            )

        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
        ]
        try:
            with (
                patch(
                    "gco.services.webhook_dispatcher.socket.getaddrinfo",
                    return_value=answers,
                ),
                patch(
                    "gco.services.webhook_dispatcher._globally_routable_unicast",
                    side_effect=ipaddress.ip_address,
                ),
                patch(
                    "gco.services.webhook_dispatcher.httpx.AsyncClient",
                    side_effect=client_factory,
                ),
            ):
                result = await dispatcher._deliver_webhook(
                    {
                        "id": "wh-tls",
                        "url": f"https://hooks.example:{port}/hook?token=secret",
                    },
                    {"event": "job.completed"},
                )
        finally:
            first.close()
            second.close()
            await first.wait_closed()
            await second.wait_closed()

        assert result.success is True
        assert result.attempts == 2
        assert seen_sni == ["hooks.example", "hooks.example"]
        assert len(requests) == 2
        assert all(
            request.startswith("POST /hook?token=secret HTTP/1.1\r\n") for request in requests
        )
        assert all(f"Host: hooks.example:{port}\r\n" in request for request in requests)


class TestDispatchEventEdgeCases:
    """Tests for _dispatch_event error handling and filtering."""

    @pytest.fixture
    def mock_webhook_store(self):
        return MagicMock()

    @pytest.fixture
    def dispatcher(self, mock_webhook_store):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=mock_webhook_store,
                    timeout=5,
                    max_retries=1,
                    retry_delay=0,
                )

    def _make_job(self, namespace="gco-jobs"):
        job = MagicMock()
        job.metadata.namespace = namespace
        job.metadata.name = "j1"
        job.metadata.uid = "uid-1"
        job.metadata.labels = {}
        job.status.conditions = []
        job.status.active = 0
        job.status.succeeded = 1
        job.status.failed = 0
        job.status.start_time = None
        job.status.completion_time = None
        return job

    @pytest.mark.asyncio
    async def test_webhook_store_exception_returns_empty(self, dispatcher, mock_webhook_store):
        """When webhook store raises, _dispatch_event returns []."""
        mock_webhook_store.get_webhooks_for_event.side_effect = RuntimeError("DDB down")
        job = self._make_job()
        results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)
        assert results == []

    @pytest.mark.asyncio
    async def test_dispatch_handles_gather_exception(self, dispatcher, mock_webhook_store, caplog):
        """Escaped delivery exceptions become sanitized, accounted failures."""
        mock_webhook_store.get_webhooks_for_event.return_value = [
            {"id": "wh-1", "url": "https://example.com/hook", "namespace": None},
        ]
        job = self._make_job()

        with patch.object(
            dispatcher,
            "_deliver_webhook",
            side_effect=RuntimeError("delivery-secret"),
        ):
            results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error == "Delivery failure: RuntimeError"
        assert dispatcher.get_metrics()["deliveries_total"] == 1
        assert dispatcher.get_metrics()["deliveries_failed"] == 1
        assert "delivery-secret" not in caplog.text

    @pytest.mark.asyncio
    async def test_dispatch_deduplicates_webhooks(self, dispatcher, mock_webhook_store):
        """Namespace and global webhooks are deduplicated by id."""
        wh = {"id": "wh-dup", "url": "https://example.com/hook", "namespace": None}
        mock_webhook_store.get_webhooks_for_event.return_value = [wh]
        job = self._make_job()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            resp = MagicMock()
            resp.status_code = 200
            mock_client.post.return_value = resp
            mock_cls.return_value.__aenter__.return_value = mock_client

            results = await dispatcher._dispatch_event(WebhookEvent.JOB_COMPLETED, job)

        # Should only deliver once despite appearing in both queries
        assert len(results) == 1


class TestProcessJobEventEdgeCases:
    """Tests for _process_job_event edge cases."""

    @pytest.fixture
    def mock_webhook_store(self):
        store = MagicMock()
        store.get_webhooks_for_event.return_value = []
        return store

    @pytest.fixture
    def dispatcher(self, mock_webhook_store):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=mock_webhook_store,
                    timeout=5,
                    max_retries=1,
                    retry_delay=0,
                    namespaces=["gco-jobs"],
                )

    def _make_job(self, namespace="gco-jobs", uid="uid-1", name="j1"):
        job = MagicMock()
        job.metadata.namespace = namespace
        job.metadata.uid = uid
        job.metadata.name = name
        job.status.conditions = []
        job.status.active = 1
        job.status.succeeded = 0
        job.status.failed = 0
        return job

    @pytest.mark.asyncio
    async def test_skip_unwatched_namespace(self, dispatcher):
        """Jobs in namespaces not in the watch list are skipped."""
        job = self._make_job(namespace="other-ns")
        await dispatcher._process_job_event("MODIFIED", job)
        assert dispatcher._job_state_cache.get_state("uid-1") is None

    @pytest.mark.asyncio
    async def test_skip_kube_public(self, dispatcher):
        """kube-public namespace is skipped."""
        job = self._make_job(namespace="kube-public")
        await dispatcher._process_job_event("MODIFIED", job)
        assert dispatcher._job_state_cache.get_state("uid-1") is None

    @pytest.mark.asyncio
    async def test_skip_kube_node_lease(self, dispatcher):
        """kube-node-lease namespace is skipped."""
        job = self._make_job(namespace="kube-node-lease")
        await dispatcher._process_job_event("MODIFIED", job)
        assert dispatcher._job_state_cache.get_state("uid-1") is None

    @pytest.mark.asyncio
    async def test_fires_event_on_transition(self, dispatcher):
        """A pending->running transition fires JOB_STARTED via _dispatch_event."""
        job_pending = self._make_job()
        job_pending.status.active = 0  # pending

        job_running = self._make_job()
        job_running.status.active = 1  # running

        # First event: new pending job -> no webhook event
        await dispatcher._process_job_event("ADDED", job_pending)
        assert dispatcher._job_state_cache.get_state("uid-1") == "pending"

        # Second event: now running -> should fire JOB_STARTED
        with patch.object(dispatcher, "_dispatch_event", new_callable=AsyncMock) as mock_dispatch:
            await dispatcher._process_job_event("MODIFIED", job_running)
            mock_dispatch.assert_called_once()
            args = mock_dispatch.call_args[0]
            assert args[0] == WebhookEvent.JOB_STARTED

    @pytest.mark.asyncio
    async def test_no_event_when_status_unchanged(self, dispatcher):
        """No event is fired when status doesn't change."""
        job = self._make_job()
        job.status.active = 1  # running

        await dispatcher._process_job_event("ADDED", job)

        with patch.object(dispatcher, "_dispatch_event", new_callable=AsyncMock) as mock_dispatch:
            await dispatcher._process_job_event("MODIFIED", job)
            mock_dispatch.assert_not_called()


class TestSyncWatchJobs:
    """Tests for _sync_watch_jobs."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                d = WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                )
                d._running = True
                return d

    def test_collects_events(self, dispatcher):
        """Test that _sync_watch_jobs collects events from the watch stream."""
        fake_job = MagicMock()
        events_iter = [
            {"type": "ADDED", "object": fake_job},
            {"type": "MODIFIED", "object": fake_job},
        ]

        with patch("gco.services.webhook_dispatcher.Watch") as MockWatch:
            MockWatch.return_value.stream.return_value = iter(events_iter)
            result = dispatcher._sync_watch_jobs()

        assert len(result) == 2
        assert result[0] == ("ADDED", fake_job)
        assert result[1] == ("MODIFIED", fake_job)

    def test_stops_when_not_running(self, dispatcher):
        """Test that watch stops when _running is set to False."""
        dispatcher._running = False
        fake_job = MagicMock()
        events_iter = [{"type": "ADDED", "object": fake_job}]

        with patch("gco.services.webhook_dispatcher.Watch") as MockWatch:
            MockWatch.return_value.stream.return_value = iter(events_iter)
            result = dispatcher._sync_watch_jobs()

        assert result == []

    def test_api_exception_410_gone(self, dispatcher):
        """Test that 410 Gone ApiException is handled gracefully."""
        from kubernetes.client.rest import ApiException

        exc = ApiException(status=410, reason="Gone")

        with patch("gco.services.webhook_dispatcher.Watch") as MockWatch:
            MockWatch.return_value.stream.side_effect = exc
            result = dispatcher._sync_watch_jobs()

        assert result == []

    def test_api_exception_other_raises(self, dispatcher):
        """Test that non-410 ApiException is re-raised."""
        from kubernetes.client.rest import ApiException

        exc = ApiException(status=403, reason="Forbidden")

        with patch("gco.services.webhook_dispatcher.Watch") as MockWatch:
            MockWatch.return_value.stream.side_effect = exc
            with pytest.raises(ApiException):
                dispatcher._sync_watch_jobs()

    def test_generic_exception_raises(self, dispatcher):
        """Test that generic exceptions are re-raised."""
        with patch("gco.services.webhook_dispatcher.Watch") as MockWatch:
            MockWatch.return_value.stream.side_effect = RuntimeError("oops")
            with pytest.raises(RuntimeError, match="oops"):
                dispatcher._sync_watch_jobs()

    def test_batch_limit(self, dispatcher):
        """Test that events are batched at 10."""
        fake_job = MagicMock()
        events_iter = [{"type": "ADDED", "object": fake_job} for _ in range(15)]

        with patch("gco.services.webhook_dispatcher.Watch") as MockWatch:
            MockWatch.return_value.stream.return_value = iter(events_iter)
            result = dispatcher._sync_watch_jobs()

        assert len(result) == 10


class TestWatchJobsAsync:
    """Tests for _watch_jobs async loop."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                d = WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                )
                return d

    @pytest.mark.asyncio
    async def test_watch_processes_events(self, dispatcher):
        """Test that _watch_jobs processes events from _sync_watch_jobs."""
        fake_job = MagicMock()
        fake_job.metadata.namespace = "gco-jobs"
        fake_job.metadata.uid = "uid-1"
        fake_job.metadata.name = "j1"
        fake_job.status.conditions = []
        fake_job.status.active = 1
        fake_job.status.succeeded = 0
        fake_job.status.failed = 0

        call_count = 0

        async def fake_sync(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("ADDED", fake_job)]
            # Stop after first iteration
            dispatcher._running = False
            return []

        dispatcher._running = True

        with (
            patch.object(dispatcher, "_process_job_event", new_callable=AsyncMock) as mock_proc,
            patch("asyncio.to_thread", side_effect=fake_sync),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await dispatcher._watch_jobs()
            mock_proc.assert_called_once_with("ADDED", fake_job)

    @pytest.mark.asyncio
    async def test_watch_handles_process_error(self, dispatcher):
        """Test that errors in _process_job_event don't crash the loop."""
        fake_job = MagicMock()
        call_count = 0

        async def fake_sync(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("ADDED", fake_job)]
            dispatcher._running = False
            return []

        dispatcher._running = True

        with (
            patch.object(
                dispatcher,
                "_process_job_event",
                new_callable=AsyncMock,
                side_effect=RuntimeError("process error"),
            ),
            patch("asyncio.to_thread", side_effect=fake_sync),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Should not raise
            await dispatcher._watch_jobs()

    @pytest.mark.asyncio
    async def test_watch_handles_sync_error(self, dispatcher):
        """Test that errors in _sync_watch_jobs are caught and retried."""
        call_count = 0

        async def fake_sync(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("watch error")
            dispatcher._running = False
            return []

        dispatcher._running = True

        with (
            patch("asyncio.to_thread", side_effect=fake_sync),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await dispatcher._watch_jobs()

    @pytest.mark.asyncio
    async def test_watch_sleeps_on_empty_events(self, dispatcher):
        """Test that _watch_jobs sleeps when no events are returned."""
        call_count = 0

        async def fake_sync(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                dispatcher._running = False
            return []

        dispatcher._running = True

        with (
            patch("asyncio.to_thread", side_effect=fake_sync),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await dispatcher._watch_jobs()
            # Should have slept at least once for empty events
            assert mock_sleep.called


class TestStartStop:
    """Tests for start() and stop() lifecycle."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                )

    @pytest.mark.asyncio
    async def test_start_already_running(self, dispatcher):
        """Calling start() when already running is a no-op."""
        dispatcher._running = True
        # Should not create a new task or re-initialize
        with patch.object(dispatcher, "_initialize_job_cache", new_callable=AsyncMock) as mock_init:
            await dispatcher.start()
            mock_init.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_and_stop(self, dispatcher):
        """Test full start/stop lifecycle."""
        with (
            patch.object(dispatcher, "_initialize_job_cache", new_callable=AsyncMock),
            patch.object(dispatcher, "_watch_jobs", new_callable=AsyncMock),
        ):
            await dispatcher.start()
            assert dispatcher._running is True
            assert dispatcher._watch_task is not None

            await dispatcher.stop()
            assert dispatcher._running is False
            assert dispatcher._watch_task is None

    @pytest.mark.asyncio
    async def test_stop_without_start(self, dispatcher):
        """Stopping without starting should not raise."""
        await dispatcher.stop()
        assert dispatcher._running is False


class TestInitializeJobCache:
    """Tests for _initialize_job_cache."""

    @pytest.fixture
    def dispatcher(self):
        with patch("gco.services.webhook_dispatcher.config") as mock_config:
            mock_config.ConfigException = Exception
            mock_config.load_incluster_config.side_effect = Exception()
            mock_config.load_kube_config.return_value = None
            with patch("gco.services.webhook_dispatcher.client"):
                return WebhookDispatcher(
                    cluster_id="c",
                    region="r",
                    webhook_store=MagicMock(),
                    namespaces=["gco-jobs"],
                )

    @pytest.mark.asyncio
    async def test_populates_cache(self, dispatcher):
        """Test that existing jobs are loaded into the cache."""
        job1 = MagicMock()
        job1.metadata.namespace = "gco-jobs"
        job1.metadata.uid = "uid-1"
        job1.status.conditions = []
        job1.status.active = 1
        job1.status.succeeded = 0
        job1.status.failed = 0

        job2 = MagicMock()
        job2.metadata.namespace = "gco-jobs"
        job2.metadata.uid = "uid-2"
        job2.status.conditions = [MagicMock(type="Complete", status="True")]
        job2.status.active = 0
        job2.status.succeeded = 1
        job2.status.failed = 0

        dispatcher.batch_v1.list_job_for_all_namespaces.return_value.items = [
            job1,
            job2,
        ]

        await dispatcher._initialize_job_cache()

        assert dispatcher._job_state_cache.get_state("uid-1") == "running"
        assert dispatcher._job_state_cache.get_state("uid-2") == "succeeded"

    @pytest.mark.asyncio
    async def test_skips_system_namespaces(self, dispatcher):
        """System namespace jobs are not cached."""
        job = MagicMock()
        job.metadata.namespace = "kube-system"
        job.metadata.uid = "uid-sys"
        job.status.conditions = []
        job.status.active = 1
        job.status.succeeded = 0
        job.status.failed = 0

        dispatcher.batch_v1.list_job_for_all_namespaces.return_value.items = [job]

        await dispatcher._initialize_job_cache()
        assert dispatcher._job_state_cache.get_state("uid-sys") is None

    @pytest.mark.asyncio
    async def test_skips_unwatched_namespaces(self, dispatcher):
        """Jobs in unwatched namespaces are not cached."""
        job = MagicMock()
        job.metadata.namespace = "other-ns"
        job.metadata.uid = "uid-other"
        job.status.conditions = []
        job.status.active = 1
        job.status.succeeded = 0
        job.status.failed = 0

        dispatcher.batch_v1.list_job_for_all_namespaces.return_value.items = [job]

        await dispatcher._initialize_job_cache()
        assert dispatcher._job_state_cache.get_state("uid-other") is None

    @pytest.mark.asyncio
    async def test_handles_api_error(self, dispatcher):
        """API errors during cache init are caught and logged."""
        dispatcher.batch_v1.list_job_for_all_namespaces.side_effect = RuntimeError("API down")
        # Should not raise
        await dispatcher._initialize_job_cache()
        assert len(dispatcher._job_state_cache.job_states) == 0


class TestMainFunction:
    """Tests for the standalone main() function."""

    @pytest.mark.asyncio
    async def test_main_keyboard_interrupt(self):
        """Test that main() handles KeyboardInterrupt gracefully."""
        from gco.services.webhook_dispatcher import main

        mock_dispatcher = MagicMock()
        mock_dispatcher.start = AsyncMock()
        mock_dispatcher.stop = AsyncMock()
        mock_dispatcher.get_metrics.return_value = {}

        with (
            patch(
                "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
                return_value=mock_dispatcher,
            ),
            patch(
                "asyncio.sleep",
                new_callable=AsyncMock,
                side_effect=KeyboardInterrupt,
            ),
        ):
            await main()

        mock_dispatcher.start.assert_called_once()
        mock_dispatcher.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_logs_metrics(self):
        """Test that main() logs metrics each iteration."""
        from gco.services.webhook_dispatcher import main

        mock_dispatcher = MagicMock()
        mock_dispatcher.start = AsyncMock()
        mock_dispatcher.stop = AsyncMock()
        mock_dispatcher.get_metrics.return_value = {"deliveries_total": 5}

        call_count = 0

        async def sleep_side_effect(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt

        with (
            patch(
                "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
                return_value=mock_dispatcher,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock, side_effect=sleep_side_effect),
        ):
            await main()

        assert mock_dispatcher.get_metrics.call_count >= 1


# Property: Webhook URL SSRF Prevention

# --- Strategies ---

# Non-HTTPS schemes
_non_https_schemes = st.sampled_from(["http", "ftp", "gopher", "file", "ssh", "telnet", "ws"])

# Blocked IPv4 networks
_blocked_ipv4_networks = [n for n in BLOCKED_NETWORKS if n.version == 4]

# Blocked IPv6 networks
_blocked_ipv6_networks = [n for n in BLOCKED_NETWORKS if n.version == 6]


def _ip_from_network(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> st.SearchStrategy:
    """Generate a random IP address within a given network."""
    start = int(network.network_address)
    end = int(network.broadcast_address)
    if network.version == 4:
        return st.integers(min_value=start, max_value=end).map(
            lambda i: str(ipaddress.IPv4Address(i))
        )
    else:
        return st.integers(min_value=start, max_value=end).map(
            lambda i: str(ipaddress.IPv6Address(i))
        )


# Strategy: a random IP from any blocked IPv4 network
_blocked_ipv4 = st.one_of(*[_ip_from_network(n) for n in _blocked_ipv4_networks])

# Strategy: a random IP from any blocked IPv6 network
_blocked_ipv6 = st.one_of(*[_ip_from_network(n) for n in _blocked_ipv6_networks])

# Strategy: any blocked IP (v4 or v6)
_blocked_ip = st.one_of(_blocked_ipv4, _blocked_ipv6)


def _is_globally_routable_unicast(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
    )


# Strategy: globally routable public-unicast IPv4 addresses.
_public_ipv4 = (
    st.tuples(
        st.integers(min_value=1, max_value=223),
        st.integers(min_value=0, max_value=255),
        st.integers(min_value=0, max_value=255),
        st.integers(min_value=1, max_value=254),
    )
    .map(lambda t: f"{t[0]}.{t[1]}.{t[2]}.{t[3]}")
    .filter(_is_globally_routable_unicast)
)


class TestWebhookSSRFPreventionProperty:
    """Property-based tests for webhook URL SSRF prevention.

    For any webhook URL that either (a) uses a non-HTTPS scheme,
    (b) resolves to an RFC1918 address, (c) resolves to a link-local address,
    or (d) resolves to a loopback address, the webhook URL validator SHALL
    reject the URL and return an error.
    """

    @given(scheme=_non_https_schemes)
    @settings(max_examples=100)
    def test_non_https_schemes_rejected(self, scheme: str):
        """Non-HTTPS schemes are always rejected."""
        url = f"{scheme}://example.com/webhook"
        is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert error is not None
        assert "HTTPS" in error

    @given(ip=_blocked_ip)
    @settings(max_examples=100)
    def test_blocked_ips_rejected(self, ip: str):
        """URLs resolving to blocked IPs are always rejected."""
        url = "https://blocked-host.example.com/webhook"
        ip_obj = ipaddress.ip_address(ip)
        family = socket.AF_INET if ip_obj.version == 4 else socket.AF_INET6
        fake_addrinfo = [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]

        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)

        assert is_valid is False
        assert error is not None
        assert "blocked" in error.lower() or "Blocked" in error

    @given(ip=_public_ipv4)
    @settings(max_examples=100)
    def test_valid_https_public_ips_accepted(self, ip: str):
        """Valid HTTPS URLs resolving to public IPs are accepted."""
        url = "https://public-host.example.com/webhook"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)

        assert is_valid is True
        assert error is None


# ── Unit tests for webhook URL validation ─────────────────────────────────
# Test specific edge cases: IPv6 loopback, DNS failure, domain allowlist
# filtering, port handling, IPv6 private addresses, mixed case scheme,
# URL with credentials/userinfo.
# _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_


class TestWebhookUrlValidationEdgeCases:
    """Unit tests for validate_webhook_url edge cases."""

    # --- IPv6 loopback (::1) rejection ---

    def test_ipv6_loopback_rejected(self):
        """IPv6 loopback address (::1) is rejected."""
        url = "https://ipv6-loopback.example.com/hook"
        fake_addrinfo = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "blocked" in error.lower()

    # --- DNS resolution failure handling ---

    def test_dns_resolution_failure(self):
        """DNS resolution failure returns a clear error."""
        url = "https://nonexistent.invalid/hook"
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "DNS resolution failed" in error

    def test_dns_resolution_empty_results(self):
        """DNS resolution returning no results is rejected."""
        url = "https://empty-dns.example.com/hook"
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=[],
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "no results" in error.lower()

    # --- Domain allowlist filtering ---

    def test_allowed_domain_passes(self):
        """A domain in the allowlist passes validation."""
        url = "https://hooks.slack.com/services/T00/B00/xxx"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("34.200.1.1", 443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url, allowed_domains=["hooks.slack.com"])
        assert is_valid is True
        assert error is None

    def test_domain_not_in_allowlist_rejected(self):
        """A domain NOT in the allowlist is rejected."""
        url = "https://evil.example.com/hook"
        is_valid, error = validate_webhook_url(
            url, allowed_domains=["hooks.slack.com", "api.pagerduty.com"]
        )
        assert is_valid is False
        assert "not in allowed domains" in error

    def test_empty_allowlist_allows_any_domain(self):
        """An empty allowlist does not restrict domains."""
        url = "https://any-domain.example.com/hook"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.50", 443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url, allowed_domains=[])
        assert is_valid is True
        assert error is None

    def test_none_allowlist_allows_any_domain(self):
        """A None allowlist does not restrict domains."""
        url = "https://any-domain.example.com/hook"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.50", 443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url, allowed_domains=None)
        assert is_valid is True
        assert error is None

    # --- Port handling (non-standard ports) ---

    def test_non_standard_port_public_ip_accepted(self):
        """HTTPS URL with a non-standard port and public IP is accepted."""
        url = "https://webhooks.example.com:8443/hook"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.10", 8443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is True
        assert error is None

    def test_non_standard_port_private_ip_rejected(self):
        """HTTPS URL with a non-standard port resolving to private IP is rejected."""
        url = "https://internal.example.com:9443/hook"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 9443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "blocked" in error.lower()

    # --- IPv6 private addresses (fc00::/7, fe80::/10) ---

    def test_ipv6_unique_local_fc00_rejected(self):
        """IPv6 unique-local address (fc00::/7) is rejected."""
        url = "https://ipv6-ula.example.com/hook"
        fake_addrinfo = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd12:3456:789a::1", 443, 0, 0))
        ]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "blocked" in error.lower()

    def test_ipv6_link_local_fe80_rejected(self):
        """IPv6 link-local address (fe80::/10) is rejected."""
        url = "https://ipv6-linklocal.example.com/hook"
        fake_addrinfo = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 443, 0, 0))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "blocked" in error.lower()

    # --- Mixed case scheme handling ---

    def test_mixed_case_http_rejected(self):
        """Mixed-case HTTP scheme (e.g. 'Http') is rejected."""
        url = "Http://example.com/hook"
        is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "HTTPS" in error

    def test_uppercase_https_rejected(self):
        """Uppercase HTTPS scheme is rejected (urlparse lowercases, but verify)."""
        # urlparse normalises the scheme to lowercase, so "HTTPS" becomes "https"
        url = "HTTPS://example.com/hook"
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        # urlparse lowercases the scheme, so this should pass
        assert is_valid is True
        assert error is None

    def test_mixed_case_ftp_rejected(self):
        """Mixed-case FTP scheme is rejected."""
        url = "Ftp://example.com/hook"
        is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "HTTPS" in error

    # --- URL with credentials / userinfo ---

    def test_url_with_userinfo_public_ip(self):
        """Reusable credentials in webhook URLs are rejected before DNS."""
        url = "https://admin:secret@webhooks.example.com/hook"
        is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "userinfo" in error

    def test_url_with_userinfo_private_ip(self):
        """Userinfo is rejected before hostname resolution or network policy."""
        url = "https://user:pass@internal.corp/hook"
        is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "userinfo" in error

    # --- Missing hostname ---

    def test_url_without_hostname_rejected(self):
        """URL without a hostname is rejected."""
        url = "https:///path/only"
        is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "hostname" in error.lower()

    # --- Multiple resolved IPs (one blocked) ---

    def test_mixed_resolved_ips_one_blocked(self):
        """If any resolved IP is blocked, the URL is rejected."""
        url = "https://dual-stack.example.com/hook"
        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.10", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=fake_addrinfo,
        ):
            is_valid, error = validate_webhook_url(url)
        assert is_valid is False
        assert "blocked" in error.lower()

    @pytest.mark.parametrize(
        "address",
        [
            "0.0.0.0",
            "100.64.0.1",
            "192.0.2.1",
            "224.0.0.1",
            "240.0.0.1",
            "::",
            "ff02::1",
            "2001:db8::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
        ],
    )
    def test_non_global_or_non_unicast_addresses_rejected(self, address):
        """Special-use and mapped-private addresses fail the public-unicast policy."""
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=[(family, socket.SOCK_STREAM, 6, "", sockaddr)],
        ):
            is_valid, error = validate_webhook_url("https://special.example/hook")
        assert is_valid is False
        assert "globally routable unicast" in error

    @pytest.mark.parametrize(
        "address",
        ["2001:4860:4860::8888", "::ffff:8.8.8.8"],
    )
    def test_global_ipv6_and_mapped_public_ipv4_are_accepted(self, address):
        family = socket.AF_INET6
        with patch(
            "gco.services.webhook_dispatcher.socket.getaddrinfo",
            return_value=[(family, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))],
        ):
            is_valid, error = validate_webhook_url("https://public-v6.example/hook")
        assert is_valid is True
        assert error is None
