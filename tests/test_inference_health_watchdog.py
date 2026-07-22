"""
Tests for the inference monitor health watchdog.

Covers `_check_health_watchdog`, which tracks per-endpoint unready timestamps
and reports when zero ready replicas have persisted beyond the configured
threshold. Traffic enters through the shared ``gco-system/gco-gateway``
``/inference`` HTTPRoute to ``gco-system/inference-proxy`` and then an endpoint
ClusterIP Service, so the watchdog does not create, delete, or recreate routing
resources. The tests verify timer start, grace-period behavior, degraded-state
reporting, recovery, cleanup, and independent tracking across endpoints.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException


@pytest.fixture
def monitor():
    """Create an InferenceMonitor with mocked K8s clients."""
    from kubernetes.config import ConfigException

    with (
        patch("kubernetes.config.load_incluster_config", side_effect=ConfigException),
        patch("kubernetes.config.load_kube_config"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        mock_store = MagicMock()
        m = InferenceMonitor(
            cluster_id="test-cluster",
            region="us-east-1",
            store=mock_store,
            namespace="gco-inference",
            reconcile_interval=15,
        )
        # Override the threshold for faster testing
        m._unhealthy_threshold_seconds = 300  # 5 minutes
        return m


class TestHealthWatchdogHealthyEndpoint:
    """Tests for endpoints that are healthy (ready_replicas > 0)."""

    def test_healthy_endpoint_returns_false(self, monitor):
        """Healthy endpoints should not have their Ingress removed."""
        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=1, desired_replicas=1, spec={}, endpoint={}
        )
        assert result is False

    def test_healthy_endpoint_clears_unready_tracker(self, monitor):
        """When an endpoint recovers, the unready tracker should be cleared."""
        # Simulate a previously unready endpoint
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=10)

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=1, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is False
        assert "my-llm" not in monitor._unready_since

    def test_healthy_endpoint_not_in_tracker_is_noop(self, monitor):
        """Healthy endpoints that were never unready should be a no-op."""
        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=2, desired_replicas=2, spec={}, endpoint={}
        )
        assert result is False
        assert "my-llm" not in monitor._unready_since


class TestHealthWatchdogUnhealthyEndpoint:
    """Tests for endpoints with zero ready replicas."""

    def test_first_unready_starts_timer(self, monitor):
        """First time seeing 0 ready replicas should start the timer."""
        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is False  # Still inside the grace period
        assert "my-llm" in monitor._unready_since

    def test_within_threshold_is_not_degraded(self, monitor):
        """Endpoints inside the grace period should not report degraded."""
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=2)

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is False

    def test_exceeds_threshold_reports_degraded_without_ingress_mutation(self, monitor):
        """A prolonged outage reports degraded but does not mutate ALB rules."""
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=10)
        monitor.networking_v1.delete_namespaced_ingress = MagicMock()

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is True
        monitor.networking_v1.delete_namespaced_ingress.assert_not_called()

    def test_repeated_checks_after_threshold_remain_degraded(self, monitor):
        """A prolonged outage remains degraded until a replica recovers."""
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=10)

        first = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )
        second = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert first is True
        assert second is True
        assert "my-llm" in monitor._unready_since

    def test_ingress_api_errors_are_irrelevant_to_watchdog(self, monitor):
        """The watchdog never invokes the legacy Ingress deletion API."""
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=10)
        monitor.networking_v1.delete_namespaced_ingress = MagicMock(
            side_effect=ApiException(status=500, reason="Internal Server Error")
        )

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is True
        monitor.networking_v1.delete_namespaced_ingress.assert_not_called()


class TestHealthWatchdogRecovery:
    """Tests for the recovery flow (endpoint becomes healthy again)."""

    def test_recovery_after_degraded_state(self, monitor):
        """Recovery clears the tracked outage."""
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=10)

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=1, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is False
        assert "my-llm" not in monitor._unready_since


class TestHealthWatchdogConfiguration:
    """Tests for the configurable threshold."""

    def test_custom_threshold(self, monitor):
        """Custom threshold should be respected."""
        monitor._unhealthy_threshold_seconds = 60  # 1 minute
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(seconds=90)
        monitor.networking_v1.delete_namespaced_ingress = MagicMock()

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is True  # 90s > 60s threshold

    def test_short_threshold_does_not_trigger_early(self, monitor):
        """Threshold should not trigger before the configured time."""
        monitor._unhealthy_threshold_seconds = 600  # 10 minutes
        monitor._unready_since["my-llm"] = datetime.now(UTC) - timedelta(minutes=5)

        result = monitor._check_health_watchdog(
            "my-llm", "gco-inference", ready_replicas=0, desired_replicas=1, spec={}, endpoint={}
        )

        assert result is False  # 5 min < 10 min threshold


class TestHealthWatchdogCleanup:
    """Tests for cleanup when endpoints are deleted."""

    def test_deleted_endpoint_clears_tracker(self, monitor):
        """Deleting an endpoint should clear its unready tracker entry."""
        monitor._unready_since["my-llm"] = datetime.now(UTC)
        monitor.apps_v1.read_namespaced_deployment = MagicMock(
            side_effect=ApiException(status=404, reason="Not Found")
        )

        monitor._reconcile_deleted("my-llm", "gco-inference")

        assert "my-llm" not in monitor._unready_since


class TestHealthWatchdogMultipleEndpoints:
    """Tests for multiple endpoints with different health states."""

    def test_independent_tracking(self, monitor):
        """Each endpoint should be tracked independently."""
        # Endpoint A is unready
        monitor._check_health_watchdog(
            "endpoint-a",
            "gco-inference",
            ready_replicas=0,
            desired_replicas=1,
            spec={},
            endpoint={},
        )
        # Endpoint B is healthy
        monitor._check_health_watchdog(
            "endpoint-b",
            "gco-inference",
            ready_replicas=1,
            desired_replicas=1,
            spec={},
            endpoint={},
        )

        assert "endpoint-a" in monitor._unready_since
        assert "endpoint-b" not in monitor._unready_since

    def test_one_unhealthy_does_not_affect_others(self, monitor):
        """One endpoint's degraded state does not affect another endpoint."""
        monitor._unready_since["endpoint-a"] = datetime.now(UTC) - timedelta(minutes=10)
        monitor.networking_v1.delete_namespaced_ingress = MagicMock()

        result_a = monitor._check_health_watchdog(
            "endpoint-a",
            "gco-inference",
            ready_replicas=0,
            desired_replicas=1,
            spec={},
            endpoint={},
        )
        # B is healthy
        result_b = monitor._check_health_watchdog(
            "endpoint-b",
            "gco-inference",
            ready_replicas=1,
            desired_replicas=1,
            spec={},
            endpoint={},
        )

        assert result_a is True
        assert result_b is False
        monitor.networking_v1.delete_namespaced_ingress.assert_not_called()
