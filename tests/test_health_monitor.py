"""
Tests for gco/services/health_monitor.HealthMonitor.

Covers construction against a patched kubernetes.config — in-cluster
config preferred with fallback to kubeconfig — plus the memory string
parser (Ki/Mi/Gi/Ti) and the broader health-calculation surface.
Uses a shared mock_k8s_config fixture that replaces both the config
loader and the kubernetes client, so tests never touch a real cluster.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes import config as k8s_config

from gco.models import RequestedResources, ResourceThresholds, ResourceUtilization
from gco.services.health_monitor import HealthMonitor, create_health_monitor_from_env


@pytest.fixture
def thresholds():
    """Create test thresholds."""
    return ResourceThresholds(cpu_threshold=80, memory_threshold=85, gpu_threshold=90)


@pytest.fixture
def mock_k8s_config():
    """Mock Kubernetes configuration loading."""
    with patch("gco.services.health_monitor.config") as mock_config:
        # Use the real ConfigException class for proper exception handling
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


class TestHealthMonitorInit:
    """Tests for HealthMonitor initialization."""

    def test_init_with_valid_params(self, mock_k8s_config, thresholds):
        """Test initialization with valid parameters."""
        with patch("gco.services.health_monitor.client"):
            monitor = HealthMonitor(
                cluster_id="test-cluster", region="us-east-1", thresholds=thresholds
            )
            assert monitor.cluster_id == "test-cluster"
            assert monitor.region == "us-east-1"
            assert monitor.thresholds == thresholds

    def test_init_loads_incluster_config_first(self, thresholds):
        """Test that in-cluster config is tried first."""
        with (
            patch("gco.services.health_monitor.config") as mock_config,
            patch("gco.services.health_monitor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.return_value = None
            HealthMonitor("test", "us-east-1", thresholds)
            mock_config.load_incluster_config.assert_called_once()

    def test_init_falls_back_to_kubeconfig(self, thresholds):
        """Test fallback to local kubeconfig."""
        with (
            patch("gco.services.health_monitor.config") as mock_config,
            patch("gco.services.health_monitor.client"),
        ):
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None
            HealthMonitor("test", "us-east-1", thresholds)
            mock_config.load_kube_config.assert_called_once()


class TestMemoryParsing:
    """Tests for memory string parsing."""

    def test_parse_ki(self, health_monitor):
        """Test parsing Ki units."""
        assert health_monitor._parse_memory_string("1024Ki") == 1024 * 1024

    def test_parse_mi(self, health_monitor):
        """Test parsing Mi units."""
        assert health_monitor._parse_memory_string("512Mi") == 512 * 1024 * 1024

    def test_parse_gi(self, health_monitor):
        """Test parsing Gi units."""
        assert health_monitor._parse_memory_string("4Gi") == 4 * 1024 * 1024 * 1024

    def test_parse_ti(self, health_monitor):
        """Test parsing Ti units."""
        assert health_monitor._parse_memory_string("1Ti") == 1024 * 1024 * 1024 * 1024

    def test_parse_decimal_k(self, health_monitor):
        """Test parsing decimal k units."""
        assert health_monitor._parse_memory_string("1000k") == 1000 * 1000

    def test_parse_decimal_m(self, health_monitor):
        """Test parsing decimal M units."""
        assert health_monitor._parse_memory_string("500M") == 500 * 1000 * 1000

    def test_parse_decimal_g(self, health_monitor):
        """Test parsing decimal G units."""
        assert health_monitor._parse_memory_string("2G") == 2 * 1000 * 1000 * 1000

    def test_parse_bytes(self, health_monitor):
        """Test parsing raw bytes."""
        assert health_monitor._parse_memory_string("1048576") == 1048576

    def test_parse_empty(self, health_monitor):
        """Test parsing empty string."""
        assert health_monitor._parse_memory_string("") == 0

    def test_parse_none(self, health_monitor):
        """Test parsing None."""
        assert health_monitor._parse_memory_string(None) == 0


class TestCpuCalculation:
    """Tests for CPU utilization calculation."""

    def test_calculate_cpu_utilization_normal(self, health_monitor):
        """Test CPU calculation with normal values."""
        # Mock node list
        mock_node = MagicMock()
        mock_node.metadata.name = "node-1"
        mock_node.status.allocatable = {"cpu": "4000m"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # Node metrics showing 50% usage
        node_metrics = {"items": [{"metadata": {"name": "node-1"}, "usage": {"cpu": "2000m"}}]}

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 50.0

    def test_calculate_cpu_utilization_nanocores(self, health_monitor):
        """Test CPU calculation with nanocores."""
        mock_node = MagicMock()
        mock_node.metadata.name = "node-1"
        mock_node.status.allocatable = {"cpu": "4"}  # 4 cores = 4000m
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # 1 billion nanocores = 1000 millicores = 1 core
        node_metrics = {
            "items": [{"metadata": {"name": "node-1"}, "usage": {"cpu": "1000000000n"}}]
        }

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 25.0  # 1 core out of 4

    def test_calculate_cpu_utilization_empty_metrics(self, health_monitor):
        """Test CPU calculation with empty metrics."""
        health_monitor.core_v1.list_node.return_value.items = []
        result = health_monitor._calculate_cpu_utilization({"items": []})
        assert result == 0.0


class TestMemoryCalculation:
    """Tests for memory utilization calculation."""

    def test_calculate_memory_utilization_normal(self, health_monitor):
        """Test memory calculation with normal values."""
        mock_node = MagicMock()
        mock_node.status.allocatable = {"memory": "8Gi"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        node_metrics = {"items": [{"usage": {"memory": "4Gi"}}]}

        result = health_monitor._calculate_memory_utilization(node_metrics)
        assert result == 50.0

    def test_calculate_memory_utilization_empty(self, health_monitor):
        """Test memory calculation with empty metrics."""
        health_monitor.core_v1.list_node.return_value.items = []
        result = health_monitor._calculate_memory_utilization({"items": []})
        assert result == 0.0


class TestHealthStatus:
    """Tests for health status determination."""

    @pytest.mark.asyncio
    async def test_healthy_status_within_thresholds(self, health_monitor):
        """Test healthy status when all metrics within thresholds."""
        # Mock get_cluster_metrics to return values within thresholds
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=60.0, gpu=30.0),
                5,  # active_jobs
                0,  # pending_pods
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "healthy"
        assert status.is_healthy()
        assert status.message is None

    @pytest.mark.asyncio
    async def test_unhealthy_status_cpu_exceeded(self, health_monitor):
        """Test unhealthy status when CPU exceeds threshold."""
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=90.0, memory=60.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert not status.is_healthy()
        assert "CPU" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_status_memory_exceeded(self, health_monitor):
        """Test unhealthy status when memory exceeds threshold."""
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=95.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "Memory" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_status_gpu_exceeded(self, health_monitor):
        """Test unhealthy status when GPU exceeds threshold."""
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=50.0, memory=60.0, gpu=95.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "GPU" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_status_multiple_violations(self, health_monitor):
        """Test unhealthy status with multiple threshold violations."""
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=90.0, memory=95.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "CPU" in status.message
        assert "Memory" in status.message

    @pytest.mark.asyncio
    async def test_unhealthy_on_error(self, health_monitor):
        """Test unhealthy status returned on error."""
        health_monitor.get_cluster_metrics = AsyncMock(side_effect=Exception("API error"))

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "Health check error" in status.message


class TestMetricsCaching:
    """Tests for metrics caching behavior."""

    @pytest.mark.asyncio
    async def test_metrics_cached(self, health_monitor):
        """Test that metrics are cached."""
        mock_metrics = {"items": []}
        health_monitor.metrics_v1beta1.list_cluster_custom_object = MagicMock(
            return_value=mock_metrics
        )

        # First call
        await health_monitor._get_node_metrics()

        # Second call should use cache
        await health_monitor._get_node_metrics()

        # Should only be called once due to caching
        assert health_monitor.metrics_v1beta1.list_cluster_custom_object.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires(self, health_monitor):
        """Test that cache expires after duration."""
        mock_metrics = {"items": []}
        health_monitor.metrics_v1beta1.list_cluster_custom_object = MagicMock(
            return_value=mock_metrics
        )
        health_monitor._cache_duration = 0  # Expire immediately

        # First call
        await health_monitor._get_node_metrics()

        # Second call should fetch fresh data
        await health_monitor._get_node_metrics()

        # Should be called twice since cache expired
        assert health_monitor.metrics_v1beta1.list_cluster_custom_object.call_count == 2


class TestCreateFromEnv:
    """Tests for create_health_monitor_from_env factory function."""

    def test_create_from_env_defaults(self):
        """Test creation with default environment values."""
        with patch("gco.services.health_monitor.config") as mock_config:
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None
            with (
                patch("gco.services.health_monitor.client"),
                patch.dict("os.environ", {}, clear=True),
            ):
                monitor = create_health_monitor_from_env()

                assert monitor.cluster_id == "unknown-cluster"
                assert monitor.region == "unknown-region"
                assert monitor.thresholds.cpu_threshold == 80
                assert monitor.thresholds.memory_threshold == 80
                assert monitor.thresholds.gpu_threshold == 60
                assert monitor.thresholds.pending_pods_threshold == 10
                assert monitor.thresholds.pending_requested_cpu_vcpus == 100
                assert monitor.thresholds.pending_requested_memory_gb == 200

    def test_create_from_env_custom_values(self):
        """Test creation with custom environment values."""
        env_vars = {
            "CLUSTER_NAME": "my-cluster",
            "REGION": "eu-west-1",
            "CPU_THRESHOLD": "70",
            "MEMORY_THRESHOLD": "75",
            "GPU_THRESHOLD": "80",
            "PENDING_PODS_THRESHOLD": "20",
            "PENDING_REQUESTED_CPU_VCPUS": "50",
            "PENDING_REQUESTED_MEMORY_GB": "100",
        }

        with patch("gco.services.health_monitor.config") as mock_config:
            mock_config.ConfigException = k8s_config.ConfigException
            mock_config.load_incluster_config.side_effect = k8s_config.ConfigException(
                "Not in cluster"
            )
            mock_config.load_kube_config.return_value = None
            with (
                patch("gco.services.health_monitor.client"),
                patch.dict("os.environ", env_vars, clear=True),
            ):
                monitor = create_health_monitor_from_env()

                assert monitor.cluster_id == "my-cluster"
                assert monitor.region == "eu-west-1"
                assert monitor.thresholds.cpu_threshold == 70
                assert monitor.thresholds.memory_threshold == 75
                assert monitor.thresholds.gpu_threshold == 80
                assert monitor.thresholds.pending_pods_threshold == 20
                assert monitor.thresholds.pending_requested_cpu_vcpus == 50
                assert monitor.thresholds.pending_requested_memory_gb == 100


class TestActiveJobsCount:
    """Tests for active jobs counting."""

    @pytest.mark.asyncio
    async def test_count_running_pods(self, health_monitor):
        """Test counting running pods as active jobs."""
        # Create mock pods
        mock_pod1 = MagicMock()
        mock_pod1.metadata.namespace = "default"
        mock_pod1.status.phase = "Running"

        mock_pod2 = MagicMock()
        mock_pod2.metadata.namespace = "gco-jobs"
        mock_pod2.status.phase = "Running"

        mock_pod3 = MagicMock()
        mock_pod3.metadata.namespace = "default"
        mock_pod3.status.phase = "Pending"

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [
            mock_pod1,
            mock_pod2,
            mock_pod3,
        ]

        count = await health_monitor._get_active_jobs_count()
        assert count == 2  # Only running pods

    @pytest.mark.asyncio
    async def test_exclude_system_namespaces(self, health_monitor):
        """Test that system namespace pods are excluded."""
        mock_pod1 = MagicMock()
        mock_pod1.metadata.namespace = "kube-system"
        mock_pod1.status.phase = "Running"

        mock_pod2 = MagicMock()
        mock_pod2.metadata.namespace = "default"
        mock_pod2.status.phase = "Running"

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [
            mock_pod1,
            mock_pod2,
        ]

        count = await health_monitor._get_active_jobs_count()
        assert count == 1  # Only non-system namespace pod


class TestGpuCalculation:
    """Tests for GPU utilization calculation."""

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_normal(self, health_monitor):
        """Test GPU calculation with normal values."""
        # Mock nodes with GPU capacity
        mock_node = MagicMock()
        mock_node.status.allocatable = {"nvidia.com/gpu": "4"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # Mock pods with GPU requests
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {"nvidia.com/gpu": "2"}
        mock_pod.spec.containers = [mock_container]
        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 50.0  # 2 out of 4 GPUs

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_no_gpus(self, health_monitor):
        """Test GPU calculation with no GPUs in cluster."""
        mock_node = MagicMock()
        mock_node.status.allocatable = {}  # No GPUs
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_no_requests(self, health_monitor):
        """Test GPU calculation with no GPU requests."""
        mock_node = MagicMock()
        mock_node.status.allocatable = {"nvidia.com/gpu": "4"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # Pod without GPU requests
        mock_pod = MagicMock()
        mock_pod.status.phase = "Running"
        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {}
        mock_pod.spec.containers = [mock_container]
        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_non_running_pods(self, health_monitor):
        """Test GPU calculation excludes non-running pods."""
        mock_node = MagicMock()
        mock_node.status.allocatable = {"nvidia.com/gpu": "4"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # Pending pod with GPU request
        mock_pod = MagicMock()
        mock_pod.status.phase = "Pending"
        mock_container = MagicMock()
        mock_container.resources = MagicMock()
        mock_container.resources.requests = {"nvidia.com/gpu": "2"}
        mock_pod.spec.containers = [mock_container]
        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 0.0  # Pending pods don't count

    @pytest.mark.asyncio
    async def test_calculate_gpu_utilization_error(self, health_monitor):
        """Test GPU calculation handles errors gracefully."""
        health_monitor.core_v1.list_node.side_effect = Exception("API error")

        result = await health_monitor._calculate_gpu_utilization()
        assert result == 0.0


class TestClusterMetrics:
    """Tests for get_cluster_metrics method."""

    @pytest.mark.asyncio
    async def test_get_cluster_metrics_success(self, health_monitor):
        """Test getting cluster metrics successfully."""
        # Mock node metrics
        health_monitor._get_node_metrics = AsyncMock(return_value={"items": []})
        health_monitor._calculate_cpu_utilization = MagicMock(return_value=50.0)
        health_monitor._calculate_memory_utilization = MagicMock(return_value=60.0)
        health_monitor._calculate_gpu_utilization = AsyncMock(return_value=30.0)
        health_monitor._get_pod_counts = AsyncMock(return_value=(10, 2))
        health_monitor._calculate_pending_requested_resources = AsyncMock(
            return_value=RequestedResources(cpu_vcpus=5.0, memory_gb=10.0)
        )

        (
            utilization,
            active_jobs,
            pending_pods,
            pending_requested,
        ) = await health_monitor.get_cluster_metrics()

        assert utilization.cpu == 50.0
        assert utilization.memory == 60.0
        assert utilization.gpu == 30.0
        assert active_jobs == 10
        assert pending_pods == 2
        assert pending_requested.cpu_vcpus == 5.0
        assert pending_requested.memory_gb == 10.0

    @pytest.mark.asyncio
    async def test_get_cluster_metrics_error(self, health_monitor):
        """Test getting cluster metrics re-raises errors so health status is unhealthy."""
        health_monitor._get_node_metrics = AsyncMock(side_effect=Exception("API error"))

        # Should re-raise so get_health_status returns "unhealthy"
        with pytest.raises(Exception, match="API error"):
            await health_monitor.get_cluster_metrics()


class TestCpuParsingEdgeCases:
    """Tests for CPU parsing edge cases."""

    def test_calculate_cpu_with_microcores(self, health_monitor):
        """Test CPU calculation with microcores (u suffix)."""
        mock_node = MagicMock()
        mock_node.metadata.name = "node-1"
        mock_node.status.allocatable = {"cpu": "4000m"}
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        # 1 million microcores = 1000 millicores = 1 core
        node_metrics = {"items": [{"metadata": {"name": "node-1"}, "usage": {"cpu": "1000000u"}}]}

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 25.0  # 1 core out of 4

    def test_calculate_cpu_with_cores(self, health_monitor):
        """Test CPU calculation with whole cores."""
        mock_node = MagicMock()
        mock_node.metadata.name = "node-1"
        mock_node.status.allocatable = {"cpu": "4"}  # 4 cores
        health_monitor.core_v1.list_node.return_value.items = [mock_node]

        node_metrics = {"items": [{"metadata": {"name": "node-1"}, "usage": {"cpu": "2"}}]}

        result = health_monitor._calculate_cpu_utilization(node_metrics)
        assert result == 50.0  # 2 cores out of 4


class TestNodeMetricsApiException:
    """Tests for node metrics API exception handling."""

    @pytest.mark.asyncio
    async def test_get_node_metrics_api_exception(self, health_monitor):
        """Test node metrics re-raises API exception and invalidates cache."""
        from kubernetes.client.rest import ApiException

        health_monitor.metrics_v1beta1.list_cluster_custom_object = MagicMock(
            side_effect=ApiException(status=500, reason="Internal Server Error")
        )

        with pytest.raises(ApiException):
            await health_monitor._get_node_metrics()

        # Cache should be invalidated
        assert health_monitor._cached_metrics is None
        assert health_monitor._last_metrics_time is None


class TestActiveJobsEdgeCases:
    """Tests for active jobs counting edge cases."""

    @pytest.mark.asyncio
    async def test_exclude_kube_public_namespace(self, health_monitor):
        """Test that kube-public namespace pods are excluded."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "kube-public"
        mock_pod.status.phase = "Running"

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        count = await health_monitor._get_active_jobs_count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_exclude_kube_node_lease_namespace(self, health_monitor):
        """Test that kube-node-lease namespace pods are excluded."""
        mock_pod = MagicMock()
        mock_pod.metadata.namespace = "kube-node-lease"
        mock_pod.status.phase = "Running"

        health_monitor.core_v1.list_pod_for_all_namespaces.return_value.items = [mock_pod]

        count = await health_monitor._get_active_jobs_count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_active_jobs_count_error(self, health_monitor):
        """Test active jobs count handles errors."""
        health_monitor.core_v1.list_pod_for_all_namespaces.side_effect = Exception("API error")

        count = await health_monitor._get_active_jobs_count()
        assert count == 0


class TestHealthStatusEdgeCases:
    """Tests for health status edge cases."""

    @pytest.mark.asyncio
    async def test_healthy_at_exact_threshold(self, health_monitor):
        """Test healthy status when exactly at threshold."""
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=80.0, memory=85.0, gpu=90.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0),
            )
        )

        status = await health_monitor.get_health_status()

        # At threshold should still be healthy (<=)
        assert status.status == "healthy"

    @pytest.mark.asyncio
    async def test_unhealthy_just_over_threshold(self, health_monitor):
        """Test unhealthy status when just over threshold."""
        health_monitor.get_cluster_metrics = AsyncMock(
            return_value=(
                ResourceUtilization(cpu=80.1, memory=60.0, gpu=30.0),
                5,
                0,
                RequestedResources(cpu_vcpus=0.0, memory_gb=0.0, gpus=0),
            )
        )

        status = await health_monitor.get_health_status()

        assert status.status == "unhealthy"
        assert "CPU" in status.message
