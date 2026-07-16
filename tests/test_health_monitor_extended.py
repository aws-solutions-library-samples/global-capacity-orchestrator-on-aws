"""
Extended tests for gco/services/health_monitor.HealthMonitor.

Covers the async internals that test_health_monitor.py doesn't reach:
_get_pod_counts (active vs pending across namespaces, with graceful
degradation when the Kubernetes API throws) and
_calculate_pending_requested_resources which sums CPU/memory/GPU
requests from pending pods. Uses the same shared kubernetes-config
mocking pattern as the base health monitor suite.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import config as k8s_config

from gco.models import RequestedResources, ResourceThresholds, ResourceUtilization
from gco.services.health_monitor import HealthMonitor


@pytest.fixture
def thresholds():
    """Create test thresholds."""
    return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)


@pytest.fixture
def mock_k8s_config():
    """Mock Kubernetes configuration loading."""
    with patch("gco.services.health_monitor.config") as mock_config:
        mock_config.ConfigException = k8s_config.ConfigException
        mock_config.load_incluster_config.side_effect = k8s_config.ConfigException("Not in cluster")
        mock_config.load_kube_config.return_value = None
        yield mock_config


@pytest.fixture
def health_monitor(mock_k8s_config, thresholds):
    """Create HealthMonitor with mocked Kubernetes clients."""
    with patch("gco.services.health_monitor.client"):
        monitor = HealthMonitor(
            cluster_id="test-cluster", region="us-east-1", thresholds=thresholds
        )
        return monitor


class TestPodCountsMethod:
    """Tests for _get_pod_counts method."""

    @pytest.mark.asyncio
    async def test_get_pod_counts_success(self, health_monitor):
        """Test getting pod counts successfully."""
        mock_pod1 = MagicMock()
        mock_pod1.metadata.namespace = "default"
        mock_pod1.status.phase = "Running"

        mock_pod2 = MagicMock()
        mock_pod2.metadata.namespace = "gco-jobs"
        mock_pod2.status.phase = "Pending"

        mock_pod3 = MagicMock()
        mock_pod3.metadata.namespace = "kube-system"
        mock_pod3.status.phase = "Running"

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [
            mock_pod1,
            mock_pod2,
            mock_pod3,
        ]

        active, pending = await health_monitor._get_pod_counts()
        assert active == 1
        assert pending == 1

    @pytest.mark.asyncio
    async def test_get_pod_counts_error(self, health_monitor):
        """Test pod counts handles errors gracefully."""
        health_monitor.core_v1.list_pod_for_all_namespaces.side_effect = Exception("API error")

        active, pending = await health_monitor._get_pod_counts()
        assert active == 0
        assert pending == 0


class TestPendingResourcesMethod:
    """Tests for _calculate_pending_requested_resources method."""

    @pytest.mark.asyncio
    async def test_calculate_pending_resources_success(self, health_monitor):
        """Test calculating pending resources successfully."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "default"
        mock_pod.status.phase = "Pending"

        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {
            "cpu": "500m",
            "memory": "1Gi",
            "nvidia.com/gpu": "1",
        }
        mock_pod.spec.containers = [mock_container]

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_pending_requested_resources()

        assert result.cpu_vcpus == 0.5
        assert result.memory_gb == 1.0
        assert result.gpus == 1

    @pytest.mark.asyncio
    async def test_calculate_pending_resources_nanocores(self, health_monitor):
        """Test calculating pending resources with nanocores."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "default"
        mock_pod.status.phase = "Pending"

        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {
            "cpu": "1000000000n",
            "memory": "512Mi",
        }
        mock_pod.spec.containers = [mock_container]

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_pending_requested_resources()

        assert result.cpu_vcpus == 1.0

    @pytest.mark.asyncio
    async def test_calculate_pending_resources_whole_cores(self, health_monitor):
        """Test calculating pending resources with whole cores."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "default"
        mock_pod.status.phase = "Pending"

        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {
            "cpu": "2",
            "memory": "4Gi",
        }
        mock_pod.spec.containers = [mock_container]

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_pending_requested_resources()

        assert result.cpu_vcpus == 2.0

    @pytest.mark.asyncio
    async def test_calculate_pending_resources_skips_running(self, health_monitor):
        """Test that running pods are not counted."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "default"
        mock_pod.status.phase = "Running"

        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {"cpu": "1000m", "memory": "2Gi"}
        mock_pod.spec.containers = [mock_container]

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_pending_requested_resources()

        assert result.cpu_vcpus == 0.0
        assert result.memory_gb == 0.0

    @pytest.mark.asyncio
    async def test_calculate_pending_resources_skips_system_namespaces(self, health_monitor):
        """Test that system namespace pods are not counted."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "kube-system"
        mock_pod.status.phase = "Pending"

        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {"cpu": "1000m", "memory": "2Gi"}
        mock_pod.spec.containers = [mock_container]

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_pending_requested_resources()

        assert result.cpu_vcpus == 0.0
        assert result.memory_gb == 0.0

    @pytest.mark.asyncio
    async def test_calculate_pending_resources_error(self, health_monitor):
        """Test pending resources handles errors gracefully."""
        health_monitor.core_v1.list_pod_for_all_namespaces.side_effect = Exception("API error")

        result = await health_monitor._calculate_pending_requested_resources()

        assert result.cpu_vcpus == 0.0
        assert result.memory_gb == 0.0
        assert result.gpus == 0


class TestKubeConfigLoadingFailure:
    """Tests for Kubernetes config loading edge cases."""

    def test_init_fails_when_both_configs_fail(self, thresholds):
        """Test initialization fails when both config methods fail."""
        from kubernetes import config as k8s_config

        with (
            patch("gco.services.health_monitor.config") as mock_config,
            patch("gco.services.health_monitor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.side_effect = k8s_config.ConfigException(
                "No kubeconfig found"
            )

            with pytest.raises(k8s_config.ConfigException):
                HealthMonitor("test", "us-east-1", thresholds)


class TestCpuCalculationErrors:
    """Additional tests for CPU calculation edge cases."""

    def test_calculate_cpu_error_handling(self, health_monitor):
        """Test CPU calculation handles errors gracefully."""
        health_monitor.core_v1.list_node.side_effect = Exception("API error")

        result = health_monitor._calculate_cpu_utilization({"items": []})
        assert result == 0.0


class TestMemoryCalculationErrors:
    """Additional tests for memory calculation edge cases."""

    def test_calculate_memory_error_handling(self, health_monitor):
        """Test memory calculation handles errors gracefully."""
        health_monitor.core_v1.list_node.side_effect = Exception("API error")

        result = health_monitor._calculate_memory_utilization({"items": []})
        assert result == 0.0


class TestAllViolationsMessage:
    """Tests for health status violation message generation."""

    @pytest.mark.asyncio
    async def test_all_violations_message(self, health_monitor):
        """Test message contains all violation types."""
        health_monitor.thresholds.pending_pods_threshold = 5
        health_monitor.thresholds.pending_requested_cpu_vcpus = 10
        health_monitor.thresholds.pending_requested_memory_gb = 20
        health_monitor.thresholds.pending_requested_gpus = 2

        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=90.0, memory=95.0, gpu=95.0),
                5,
                10,
                RequestedResources(cpu_vcpus=50.0, memory_gb=100.0, gpus=8),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "CPU" in status.message
        assert "Memory" in status.message
        assert "GPU" in status.message
        assert "Pending Pods" in status.message
        assert "Pending CPU" in status.message
        assert "Pending Memory" in status.message
        assert "Pending GPUs" in status.message


class TestGetActiveJobsCount:
    """Tests for _get_active_jobs_count method."""

    @pytest.mark.asyncio
    async def test_get_active_jobs_count_success(self, health_monitor):
        """Test getting active jobs count successfully."""
        mock_pod1 = MagicMock()
        mock_pod1.metadata.namespace = "default"
        mock_pod1.status.phase = "Running"

        mock_pod2 = MagicMock()
        mock_pod2.metadata.namespace = "gco-jobs"
        mock_pod2.status.phase = "Running"

        mock_pod3 = MagicMock()
        mock_pod3.metadata.namespace = "kube-system"
        mock_pod3.status.phase = "Running"

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [
            mock_pod1,
            mock_pod2,
            mock_pod3,
        ]

        count = await health_monitor._get_active_jobs_count()
        assert count == 2  # Excludes kube-system

    @pytest.mark.asyncio
    async def test_get_active_jobs_count_error(self, health_monitor):
        """Test active jobs count handles errors gracefully."""
        health_monitor.core_v1.list_pod_for_all_namespaces.side_effect = Exception("API error")

        count = await health_monitor._get_active_jobs_count()
        assert count == 0


class TestGpuUtilization:
    """Tests for _calculate_gpu_utilization method."""

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_success(self, health_monitor):
        """Test calculating GPU utilization successfully."""
        # Mock nodes with GPU capacity
        mock_node = MagicMock()
        mock_node.status.allocatable = {"nvidia.com/gpu": "4"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # Mock running pods with GPU requests
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {"nvidia.com/gpu": "2"}
        mock_pod.spec.containers = [mock_container]
        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 50.0  # 2/4 = 50%

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_no_gpus(self, health_monitor):
        """Test GPU utilization when no GPUs available."""
        mock_node = MagicMock()
        mock_node.status.allocatable = {"nvidia.com/gpu": "0"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = []

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_error(self, health_monitor):
        """Test GPU utilization handles errors gracefully."""
        health_monitor.core_v1.list_pod_for_all_namespaces.side_effect = Exception("API error")

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 0.0


class TestNodeMetricsCache:
    """Tests for node metrics caching."""

    @pytest.mark.asyncio
    async def test_get_node_metrics_uses_cache(self, health_monitor):
        """Test that node metrics uses cache when available."""
        from datetime import datetime

        # Set up cached metrics
        health_monitor._cached_metrics = {"items": [{"test": "data"}]}
        health_monitor._last_metrics_time = datetime.now()

        result = await health_monitor._get_node_metrics()

        # Should return cached data without calling API
        assert result == {"items": [{"test": "data"}]}
        health_monitor.metrics_v1beta1.list_cluster_custom_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_node_metrics_api_error(self, health_monitor):
        """Test node metrics re-raises API errors and invalidates cache."""
        from kubernetes.client.rest import ApiException

        health_monitor._cached_metrics = None
        health_monitor._last_metrics_time = None
        health_monitor.metrics_v1beta1.list_cluster_custom_object.side_effect = ApiException(
            status=500, reason="Internal Server Error"
        )

        with pytest.raises(ApiException):
            await health_monitor._get_node_metrics()

        assert health_monitor._cached_metrics is None
        assert health_monitor._last_metrics_time is None


class TestCpuUtilizationEdgeCases:
    """Tests for CPU utilization calculation edge cases."""

    def test_calculate_cpu_with_millicores_capacity(self, health_monitor):
        """Test CPU calculation with millicores capacity."""
        mock_node = MagicMock()
        mock_node.metadata.name = "node1"
        mock_node.status.allocatable = {"cpu": "4000m"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        node_metrics = {
            "items": [
                {
                    "metadata": {"name": "node1"},
                    "usage": {"cpu": "2000m"},
                }
            ]
        }

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 50.0  # 2000/4000 = 50%

    def test_calculate_cpu_with_microcores_usage(self, health_monitor):
        """Test CPU calculation with microcores usage."""
        mock_node = MagicMock()
        mock_node.metadata.name = "node1"
        mock_node.status.allocatable = {"cpu": "4"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        node_metrics = {
            "items": [
                {
                    "metadata": {"name": "node1"},
                    "usage": {"cpu": "2000000u"},  # 2000 millicores in microcores
                }
            ]
        }

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 50.0  # 2000/4000 = 50%

    def test_calculate_cpu_with_whole_cores_usage(self, health_monitor):
        """Test CPU calculation with whole cores usage."""
        mock_node = MagicMock()
        mock_node.metadata.name = "node1"
        mock_node.status.allocatable = {"cpu": "4"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        node_metrics = {
            "items": [
                {
                    "metadata": {"name": "node1"},
                    "usage": {"cpu": "2"},  # 2 whole cores
                }
            ]
        }

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 50.0  # 2000/4000 = 50%


class TestMemoryParsing:
    """Tests for memory string parsing."""

    def test_parse_memory_terabytes(self, health_monitor):
        """Test parsing terabyte memory strings."""
        result = health_monitor._parse_memory_string("1Ti")
        assert result == 1024 * 1024 * 1024 * 1024

    def test_parse_memory_decimal_kilobytes(self, health_monitor):
        """Test parsing decimal kilobyte memory strings."""
        result = health_monitor._parse_memory_string("1000k")
        assert result == 1000 * 1000

    def test_parse_memory_decimal_megabytes(self, health_monitor):
        """Test parsing decimal megabyte memory strings."""
        result = health_monitor._parse_memory_string("1000M")
        assert result == 1000 * 1000 * 1000

    def test_parse_memory_decimal_gigabytes(self, health_monitor):
        """Test parsing decimal gigabyte memory strings."""
        result = health_monitor._parse_memory_string("1G")
        assert result == 1000 * 1000 * 1000

    def test_parse_memory_empty_string(self, health_monitor):
        """Test parsing empty memory string."""
        result = health_monitor._parse_memory_string("")
        assert result == 0

    def test_parse_memory_bytes_only(self, health_monitor):
        """Test parsing bytes-only memory string."""
        result = health_monitor._parse_memory_string("1024")
        assert result == 1024


class TestGetClusterMetricsError:
    """Tests for get_cluster_metrics error handling."""

    @pytest.mark.asyncio
    async def test_get_cluster_metrics_error_returns_defaults(self, health_monitor):
        """Test that get_cluster_metrics re-raises errors for unhealthy status."""
        health_monitor.metrics_v1beta1.list_cluster_custom_object.side_effect = Exception(
            "API error"
        )
        health_monitor.core_v1.list_node.side_effect = Exception("API error")
        health_monitor.core_v1.list_pod_for_all_namespaces.side_effect = Exception("API error")

        with pytest.raises(Exception, match="API error"):
            await health_monitor.get_cluster_metrics()


class TestGetHealthStatusError:
    """Tests for get_health_status error handling."""

    @pytest.mark.asyncio
    async def test_get_health_status_error_returns_unhealthy(self, health_monitor):
        """Test that get_health_status returns unhealthy on error."""
        health_monitor.get_cluster_metrics = AsyncMock(side_effect=Exception("API error"))

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "Health check error" in status.message


class TestCreateHealthMonitorFromEnv:
    """Tests for create_health_monitor_from_env function."""

    def test_create_health_monitor_from_env_defaults(self, mock_k8s_config):
        """Test creating health monitor with default environment values."""
        import os
        from unittest.mock import patch

        with (
            patch("gco.services.health_monitor.client"),
            patch.dict(os.environ, {}, clear=True),
        ):
            from gco.services.health_monitor import create_health_monitor_from_env

            monitor = create_health_monitor_from_env()

            assert monitor.cluster_id == "unknown-cluster"
            assert monitor.region == "unknown-region"
            assert monitor.thresholds.cpu_threshold == 60
            assert monitor.thresholds.memory_threshold == 60
            assert monitor.thresholds.gpu_threshold == 60

    def test_create_health_monitor_from_env_custom(self, mock_k8s_config):
        """Test creating health monitor with custom environment values."""
        import os
        from unittest.mock import patch

        env_vars = {
            "CLUSTER_NAME": "test-cluster",
            "REGION": "us-west-2",
            "CPU_THRESHOLD": "80",
            "MEMORY_THRESHOLD": "85",
            "GPU_THRESHOLD": "90",
            "PENDING_PODS_THRESHOLD": "20",
            "PENDING_REQUESTED_CPU_VCPUS": "50",
            "PENDING_REQUESTED_MEMORY_GB": "100",
            "PENDING_REQUESTED_GPUS": "4",
        }

        with (
            patch("gco.services.health_monitor.client"),
            patch.dict(os.environ, env_vars, clear=True),
        ):
            from gco.services.health_monitor import create_health_monitor_from_env

            monitor = create_health_monitor_from_env()

            assert monitor.cluster_id == "test-cluster"
            assert monitor.region == "us-west-2"
            assert monitor.thresholds.cpu_threshold == 80
            assert monitor.thresholds.memory_threshold == 85
            assert monitor.thresholds.gpu_threshold == 90
            assert monitor.thresholds.pending_pods_threshold == 20


# =============================================================================
# ALB Registration Sync Tests
# =============================================================================


class TestSyncAlbRegistration:
    """Tests for the self-healing ALB hostname sync."""

    @pytest.fixture
    def monitor(self):
        with (
            patch("gco.services.health_monitor.config.load_incluster_config"),
            patch("gco.services.health_monitor.client.CoreV1Api"),
            patch("gco.services.health_monitor.client.NetworkingV1Api") as mock_net,
            patch("gco.services.health_monitor.client.CoordinationV1Api") as mock_coord,
            patch("gco.services.health_monitor.client.CustomObjectsApi"),
        ):
            from gco.models.cluster_models import ResourceThresholds

            m = HealthMonitor(
                cluster_id="test-cluster",
                region="us-east-1",
                thresholds=ResourceThresholds(
                    cpu_threshold=80, memory_threshold=85, gpu_threshold=-1
                ),
            )
            m.networking_v1 = mock_net.return_value
            m.coordination_v1 = mock_coord.return_value
            lease = MagicMock()
            lease.spec.holder_identity = m._alb_sync_holder
            lease.spec.renew_time = datetime.now(UTC)
            lease.spec.lease_duration_seconds = 90
            lease.spec.lease_transitions = 0
            m.coordination_v1.read_namespaced_lease.return_value = lease
            yield m

    @pytest.mark.asyncio
    async def test_sync_offloads_blocking_reconciliation(self, monitor):
        """Kubernetes and AWS SDK calls must not block FastAPI's event loop."""
        with (
            patch.object(monitor, "_sync_alb_registration") as mock_sync,
            patch(
                "gco.services.health_monitor.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread,
        ):
            await monitor.sync_alb_registration()

        mock_to_thread.assert_awaited_once_with(mock_sync)

    @pytest.mark.asyncio
    async def test_follower_keeps_serving_but_does_not_self_heal(self, monitor):
        """A replica that does not hold the Lease must not touch Ingress or SSM."""
        lease = monitor.coordination_v1.read_namespaced_lease.return_value
        lease.spec.holder_identity = "another-health-monitor-pod"
        lease.spec.renew_time = datetime.now(UTC)

        with patch("boto3.client") as mock_boto:
            await monitor.sync_alb_registration()

        monitor.networking_v1.read_namespaced_ingress.assert_not_called()
        mock_boto.assert_not_called()

    def test_expired_lease_is_claimed_with_optimistic_replace(self, monitor):
        """An expired holder is replaced using the read object's resourceVersion."""
        lease = monitor.coordination_v1.read_namespaced_lease.return_value
        lease.spec.holder_identity = "dead-health-monitor-pod"
        lease.spec.renew_time = datetime.now(UTC) - timedelta(minutes=5)

        assert monitor._try_acquire_alb_sync_lease() is True
        assert lease.spec.holder_identity == monitor._alb_sync_holder
        monitor.coordination_v1.replace_namespaced_lease.assert_called_once()

    @pytest.mark.asyncio
    async def test_lease_conflict_fails_closed(self, monitor):
        """A 409 election race must suppress mutation on the losing replica."""
        from kubernetes.client.rest import ApiException

        monitor.coordination_v1.replace_namespaced_lease.side_effect = ApiException(status=409)

        with patch("boto3.client") as mock_boto:
            await monitor.sync_alb_registration()

        monitor.networking_v1.read_namespaced_ingress.assert_not_called()
        mock_boto.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_precreated_lease_fails_closed(self, monitor):
        """No broad create permission is needed; a missing Lease disables mutation."""
        from kubernetes.client.rest import ApiException

        monitor.coordination_v1.read_namespaced_lease.side_effect = ApiException(status=404)

        with patch("boto3.client") as mock_boto:
            await monitor.sync_alb_registration()

        monitor.networking_v1.read_namespaced_ingress.assert_not_called()
        monitor.coordination_v1.create_namespaced_lease.assert_not_called()
        mock_boto.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_skips_when_recently_synced(self, monitor):
        """Should skip if last sync was less than 5 minutes ago."""
        from datetime import datetime

        monitor._last_alb_sync = datetime.now()
        await monitor.sync_alb_registration()
        # networking_v1 should NOT be called
        monitor.networking_v1.read_namespaced_ingress.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_updates_ssm_when_stale(self, monitor):
        """Should update SSM when hostname doesn't match."""
        ingress = MagicMock()
        ingress.status.load_balancer.ingress = [MagicMock(hostname="new-alb.elb.amazonaws.com")]
        monitor.networking_v1.read_namespaced_ingress.return_value = ingress

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "old-alb.elb.amazonaws.com"}}
        mock_ssm.exceptions.ParameterNotFound = type("ParameterNotFound", (Exception,), {})

        with (
            patch("boto3.client", return_value=mock_ssm) as mock_boto,
            patch.dict("os.environ", {"GLOBAL_REGION": "us-east-2", "PROJECT_NAME": "gco"}),
        ):
            await monitor.sync_alb_registration()

        mock_ssm.put_parameter.assert_called_once_with(
            Name="/gco/alb-hostname-us-east-1",
            Value="new-alb.elb.amazonaws.com",
            Type="String",
            Overwrite=True,
        )
        # Acquire once before the read and renew optimistically again
        # immediately before the only mutation.
        assert monitor.coordination_v1.replace_namespaced_lease.call_count == 2
        client_config = mock_boto.call_args.kwargs["config"]
        assert client_config.connect_timeout == 3
        assert client_config.read_timeout == 10
        assert client_config.retries["total_max_attempts"] == 2

    @pytest.mark.asyncio
    async def test_sync_loses_lease_before_put_and_fails_closed(self, monitor):
        """A former leader must not write after the pre-mutation renewal fails."""
        ingress = MagicMock()
        ingress.status.load_balancer.ingress = [MagicMock(hostname="new-alb.elb.amazonaws.com")]
        monitor.networking_v1.read_namespaced_ingress.return_value = ingress
        monitor._try_acquire_alb_sync_lease = MagicMock(side_effect=[True, False])

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "old-alb"}}
        with patch("boto3.client", return_value=mock_ssm):
            await monitor.sync_alb_registration()

        assert monitor._try_acquire_alb_sync_lease.call_count == 2
        mock_ssm.put_parameter.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_noop_when_hostname_matches(self, monitor):
        """Should not update SSM when hostname already matches."""
        ingress = MagicMock()
        ingress.status.load_balancer.ingress = [MagicMock(hostname="same-alb.elb.amazonaws.com")]
        monitor.networking_v1.read_namespaced_ingress.return_value = ingress

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "same-alb.elb.amazonaws.com"}}
        mock_ssm.exceptions.ParameterNotFound = type("ParameterNotFound", (Exception,), {})

        with (
            patch("boto3.client", return_value=mock_ssm),
            patch.dict("os.environ", {"GLOBAL_REGION": "us-east-2", "PROJECT_NAME": "gco"}),
        ):
            await monitor.sync_alb_registration()

        mock_ssm.put_parameter.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_handles_no_ingress_gracefully(self, monitor):
        """Should not crash if ingress has no load balancer status."""
        ingress = MagicMock()
        ingress.status.load_balancer.ingress = []
        monitor.networking_v1.read_namespaced_ingress.return_value = ingress

        await monitor.sync_alb_registration()
        # Should complete without error

    @pytest.mark.asyncio
    async def test_sync_handles_exception_gracefully(self, monitor):
        """Should log warning but not crash on errors."""
        monitor.networking_v1.read_namespaced_ingress.side_effect = Exception("k8s down")

        await monitor.sync_alb_registration()
        # Should complete without raising
