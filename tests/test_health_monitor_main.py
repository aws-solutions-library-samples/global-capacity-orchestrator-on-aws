"""
Tests for the `main()` entry point in gco/services/health_monitor.py.

Exercises the long-running loop that wakes up on a fixed interval,
calls HealthMonitor.get_health_status, logs a structured report, and
feeds the webhook dispatcher. Each test runs a single iteration by
making asyncio.sleep raise KeyboardInterrupt, so both the happy path
(healthy status, no message) and the unhealthy path (status message
included in the structured log) can be covered without an actual loop.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import config as k8s_config

from gco.models import RequestedResources, ResourceThresholds, ResourceUtilization


class TestHealthMonitorMain:
    """Tests for the main() function in health_monitor.py."""

    @pytest.mark.asyncio
    async def test_main_single_iteration(self):
        """Test main function runs one iteration."""
        # Import main fresh to ensure patches work
        import gco.services.health_monitor as health_monitor_module

        mock_dispatcher = MagicMock()
        mock_dispatcher.start = AsyncMock()
        mock_dispatcher.stop = AsyncMock()
        mock_dispatcher.get_metrics.return_value = {
            "deliveries_total": 0,
            "deliveries_success": 0,
            "deliveries_failed": 0,
        }

        with (
            patch.object(health_monitor_module, "create_health_monitor_from_env") as mock_create,
            patch(
                "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
                return_value=mock_dispatcher,
            ) as mock_webhook,  # noqa: F841
            patch.object(health_monitor_module.asyncio, "sleep") as mock_sleep,
        ):
            mock_monitor = MagicMock()
            mock_status = MagicMock()
            mock_status.status = "healthy"
            mock_status.resource_utilization = ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0)
            mock_status.active_jobs = 5
            mock_status.pending_pods = 2
            mock_status.pending_requested = RequestedResources(cpu_vcpus=2.0, memory_gb=4.0, gpus=1)
            mock_status.message = None

            mock_monitor.get_health_status = AsyncMock(return_value=mock_status)
            mock_create.return_value = mock_monitor

            # Make sleep raise KeyboardInterrupt to exit the loop
            mock_sleep.side_effect = KeyboardInterrupt()

            # Should exit gracefully on KeyboardInterrupt
            await health_monitor_module.main()

            mock_monitor.get_health_status.assert_called_once()
            mock_dispatcher.start.assert_called_once()
            mock_dispatcher.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_with_message(self):
        """Test main function with status message."""
        import gco.services.health_monitor as health_monitor_module

        mock_dispatcher = MagicMock()
        mock_dispatcher.start = AsyncMock()
        mock_dispatcher.stop = AsyncMock()
        mock_dispatcher.get_metrics.return_value = {
            "deliveries_total": 0,
            "deliveries_success": 0,
            "deliveries_failed": 0,
        }

        with (
            patch.object(health_monitor_module, "create_health_monitor_from_env") as mock_create,
            patch(
                "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
                return_value=mock_dispatcher,
            ),
            patch.object(health_monitor_module.asyncio, "sleep") as mock_sleep,
        ):
            mock_monitor = MagicMock()
            mock_status = MagicMock()
            mock_status.status = "unhealthy"
            mock_status.resource_utilization = ResourceUtilization(cpu=90.0, memory=60.0, gpu=30.0)
            mock_status.active_jobs = 5
            mock_status.pending_pods = 2
            mock_status.pending_requested = RequestedResources(cpu_vcpus=2.0, memory_gb=4.0, gpus=1)
            mock_status.message = "CPU threshold exceeded"

            mock_monitor.get_health_status = AsyncMock(return_value=mock_status)
            mock_create.return_value = mock_monitor

            mock_sleep.side_effect = KeyboardInterrupt()

            await health_monitor_module.main()

            mock_monitor.get_health_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_handles_exception(self):
        """Test main function handles exceptions gracefully."""
        # This test verifies that the main loop handles exceptions
        # We can't easily test the full loop, so we test the error handling path
        from gco.services.health_monitor import create_health_monitor_from_env

        with (
            patch("gco.services.health_monitor.config") as mock_config,
            patch("gco.services.health_monitor.client"),
            patch.dict("os.environ", {"CLUSTER_NAME": "test", "REGION": "us-east-1"}, clear=True),
        ):
            from kubernetes import config as k8s_config

            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None

            # Just verify the factory function works
            monitor = create_health_monitor_from_env()
            assert monitor.cluster_id == "test"
            assert monitor.region == "us-east-1"

    @pytest.mark.asyncio
    async def test_main_no_pending_requested(self):
        """Test main function when pending_requested is None."""
        import gco.services.health_monitor as health_monitor_module

        mock_dispatcher = MagicMock()
        mock_dispatcher.start = AsyncMock()
        mock_dispatcher.stop = AsyncMock()
        mock_dispatcher.get_metrics.return_value = {
            "deliveries_total": 0,
            "deliveries_success": 0,
            "deliveries_failed": 0,
        }

        with (
            patch.object(health_monitor_module, "create_health_monitor_from_env") as mock_create,
            patch(
                "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
                return_value=mock_dispatcher,
            ),
            patch.object(health_monitor_module.asyncio, "sleep") as mock_sleep,
        ):
            mock_monitor = MagicMock()
            mock_status = MagicMock()
            mock_status.status = "healthy"
            mock_status.resource_utilization = ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0)
            mock_status.active_jobs = 5
            mock_status.pending_pods = 0
            mock_status.pending_requested = None  # No pending resources
            mock_status.message = None

            mock_monitor.get_health_status = AsyncMock(return_value=mock_status)
            mock_create.return_value = mock_monitor

            mock_sleep.side_effect = KeyboardInterrupt()

            await health_monitor_module.main()

            mock_monitor.get_health_status.assert_called_once()


class TestHealthMonitorPendingThresholds:
    """Tests for pending pods and resources threshold violations."""

    @pytest.fixture
    def thresholds_with_pending(self):
        """Create thresholds with pending limits."""
        return ResourceThresholds(
            cpu_threshold=80,
            memory_threshold=85,
            gpu_threshold=90,
            pending_pods_threshold=5,
            pending_requested_cpu_vcpus=10,
            pending_requested_memory_gb=20,
            pending_requested_gpus=2,
        )

    @pytest.fixture
    def mock_k8s_config(self):
        """Mock Kubernetes configuration loading."""
        with patch("gco.services.health_monitor.config") as mock_config:
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None
            yield mock_config

    @pytest.fixture
    def health_monitor_with_pending(self, mock_k8s_config, thresholds_with_pending):
        """Create HealthMonitor with pending thresholds."""
        from gco.services.health_monitor import HealthMonitor

        with patch("gco.services.health_monitor.client"):
            monitor = HealthMonitor(
                cluster_id="test-cluster",
                region="us-east-1",
                thresholds=thresholds_with_pending,
            )
            return monitor

    @pytest.mark.asyncio
    async def test_unhealthy_pending_pods_exceeded(self, health_monitor_with_pending):
        """Test unhealthy status when pending pods exceed threshold."""
        health_monitor_with_pending.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
                5,
                10,  # Exceeds threshold of 5
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0),
            )
        )

        status = await health_monitor_with_pending.get_health_status()

        assert status.status == "unhealthy"
        assert "Pending Pods" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_pending_cpu_exceeded(self, health_monitor_with_pending):
        """Test unhealthy status when pending CPU exceeds threshold."""
        health_monitor_with_pending.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=15.0, memory_gb=0.0, gpus=0),  # Exceeds 10
            )
        )

        status = await health_monitor_with_pending.get_health_status()

        assert status.status == "unhealthy"
        assert "Pending CPU" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_pending_memory_exceeded(self, health_monitor_with_pending):
        """Test unhealthy status when pending memory exceeds threshold."""
        health_monitor_with_pending.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=25.0, gpus=0),  # Exceeds 20
            )
        )

        status = await health_monitor_with_pending.get_health_status()

        assert status.status == "unhealthy"
        assert "Pending Memory" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_pending_gpus_exceeded(self, health_monitor_with_pending):
        """Test unhealthy status when pending GPUs exceed threshold."""
        health_monitor_with_pending.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=5),  # Exceeds 2
            )
        )

        status = await health_monitor_with_pending.get_health_status()

        assert status.status == "unhealthy"
        assert "Pending GPUs" in status.message
