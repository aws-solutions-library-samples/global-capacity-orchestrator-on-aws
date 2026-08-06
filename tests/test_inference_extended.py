"""
Extended tests for the inference endpoint feature.

Covers the leader-election path on InferenceMonitor (_try_acquire_lease
for renew-as-holder, claim-when-empty, claim-when-None, and the
not-leader branch), HPA creation, deployment creation with
model_source, the start/stop lifecycle, the monitor's main() entry
point, and InferenceManager.add_region / remove_region. Uses a
_make_monitor helper that patches every kubernetes client class at once
so the fixture surface stays small.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Helper to build a monitor fixture with all K8s clients mocked
# =============================================================================


def _make_monitor(mock_store=None):
    """Create an InferenceMonitor with all K8s clients mocked."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_net,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        store = mock_store or MagicMock()
        m = InferenceMonitor(
            cluster_id="test-cluster",
            region="us-east-1",
            store=store,
            namespace="gco-inference",
            reconcile_interval=5,
        )
        m.apps_v1 = mock_apps.return_value
        m.core_v1 = mock_core.return_value
        m.networking_v1 = mock_net.return_value
        return m


# =============================================================================
# Leader Election Tests
# =============================================================================


class TestLeaderElection:
    """Tests for _try_acquire_lease."""

    def test_lease_renew_as_current_holder(self):
        monitor = _make_monitor()
        mock_coord = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = "my-pod"
        lease.spec.renew_time = datetime.now(UTC)
        mock_coord.read_namespaced_lease.return_value = lease

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is True
        mock_coord.replace_namespaced_lease.assert_called_once()

    def test_lease_claim_when_no_holder(self):
        monitor = _make_monitor()
        mock_coord = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = ""
        lease.spec.renew_time = datetime.now(UTC)
        mock_coord.read_namespaced_lease.return_value = lease

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is True

    def test_lease_claim_when_holder_is_none(self):
        monitor = _make_monitor()
        mock_coord = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = None
        lease.spec.renew_time = datetime.now(UTC)
        mock_coord.read_namespaced_lease.return_value = lease

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is True

    def test_lease_not_leader_when_other_holds(self):
        monitor = _make_monitor()
        mock_coord = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = "other-pod"
        lease.spec.renew_time = datetime.now(UTC)
        mock_coord.read_namespaced_lease.return_value = lease

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is False

    def test_lease_takeover_when_expired(self):
        monitor = _make_monitor()
        mock_coord = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = "dead-pod"
        # Expired: renew_time is way in the past
        lease.spec.renew_time = datetime.now(UTC) - timedelta(minutes=10)
        mock_coord.read_namespaced_lease.return_value = lease

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is True

    def test_lease_create_when_404(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        mock_coord = MagicMock()
        mock_coord.read_namespaced_lease.side_effect = ApiException(status=404)

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is True
        mock_coord.create_namespaced_lease.assert_called_once()

    def test_lease_create_conflict_returns_false(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        mock_coord = MagicMock()
        mock_coord.read_namespaced_lease.side_effect = ApiException(status=404)
        mock_coord.create_namespaced_lease.side_effect = ApiException(status=409)

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is False

    def test_lease_api_error_returns_false(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        mock_coord = MagicMock()
        mock_coord.read_namespaced_lease.side_effect = ApiException(status=500, reason="Internal")

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is False

    def test_lease_no_renew_time(self):
        """When renew_time is None, should still check holder."""
        monitor = _make_monitor()
        mock_coord = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = "my-pod"
        lease.spec.renew_time = None
        mock_coord.read_namespaced_lease.return_value = lease

        with patch(
            "gco.services.inference_monitor.client.CoordinationV1Api", return_value=mock_coord
        ):
            result = monitor._try_acquire_lease("test-lease", "my-pod")

        assert result is True


# =============================================================================
# HPA Creation Tests
# =============================================================================


class TestHPACreation:
    """Tests for _create_or_update_hpa."""

    def test_hpa_create_cpu_metric(self):
        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        spec = {
            "autoscaling": {
                "enabled": True,
                "min_replicas": 2,
                "max_replicas": 8,
                "metrics": [{"type": "cpu", "target": 70}],
            }
        }
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep1", "ns", spec)

        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.assert_called_once()
        call_args = mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.call_args
        assert call_args[0][0] == "ns"

    def test_hpa_create_memory_metric(self):
        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        spec = {
            "autoscaling": {
                "enabled": True,
                "min_replicas": 1,
                "max_replicas": 5,
                "metrics": [{"type": "memory", "target": 80}],
            }
        }
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep2", "ns", spec)

        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.assert_called_once()

    def test_hpa_create_unknown_metric_defaults_to_cpu(self):
        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        spec = {
            "autoscaling": {
                "enabled": True,
                "metrics": [{"type": "disk", "target": 50}],
            }
        }
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep3", "ns", spec)

        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.assert_called_once()

    def test_hpa_update_on_conflict(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.side_effect = ApiException(
            status=409
        )
        spec = {
            "autoscaling": {
                "enabled": True,
                "metrics": [{"type": "cpu", "target": 70}],
            }
        }
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep4", "ns", spec)

        mock_hpa_api.patch_namespaced_horizontal_pod_autoscaler.assert_called_once()

    def test_hpa_not_enabled_noop(self):
        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        spec = {"autoscaling": {"enabled": False}}
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep5", "ns", spec)

        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.assert_not_called()

    def test_hpa_no_autoscaling_key_noop(self):
        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        spec = {}
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep6", "ns", spec)

        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.assert_not_called()

    def test_hpa_multiple_metrics(self):
        monitor = _make_monitor()
        mock_hpa_api = MagicMock()
        spec = {
            "autoscaling": {
                "enabled": True,
                "min_replicas": 1,
                "max_replicas": 10,
                "metrics": [
                    {"type": "cpu", "target": 70},
                    {"type": "memory", "target": 80},
                ],
            }
        }
        with patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api", return_value=mock_hpa_api
        ):
            monitor._create_or_update_hpa("ep7", "ns", spec)

        mock_hpa_api.create_namespaced_horizontal_pod_autoscaler.assert_called_once()


# =============================================================================
# Create Deployment Tests (model_source, init containers, etc.)
# =============================================================================


class TestCreateDeployment:
    """Tests for _create_deployment with various spec options."""

    def test_create_deployment_basic(self):
        monitor = _make_monitor()
        spec = {"image": "img:v1", "replicas": 1, "port": 8000, "gpu_count": 1}
        monitor._create_deployment("ep", "ns", spec)
        monitor.apps_v1.create_namespaced_deployment.assert_called_once()

    def test_create_deployment_with_model_source_s3(self):
        """S3 model_source should add init container for sync."""
        monitor = _make_monitor()
        spec = {
            "image": "img:v1",
            "replicas": 1,
            "gpu_count": 1,
            "model_source": "s3://bucket/models/llama3",
        }
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        init_containers = deployment.spec.template.spec.init_containers
        assert init_containers is not None
        assert len(init_containers) == 1
        init_container = init_containers[0]
        assert init_container.name == "model-sync"
        assert init_container.command == ["aws"]
        assert init_container.args == [
            "s3",
            "sync",
            "s3://bucket/models/llama3",
            "/models/ep",
            "--quiet",
        ]

    def test_create_deployment_with_model_path(self):
        """model_path should add volume mounts."""
        monitor = _make_monitor()
        spec = {
            "image": "img:v1",
            "replicas": 1,
            "gpu_count": 1,
            "model_path": "/models/llama",
        }
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        volumes = deployment.spec.template.spec.volumes
        assert volumes is not None
        assert any(v.name == "model-storage" for v in volumes)

    def test_create_deployment_with_env_vars(self):
        monitor = _make_monitor()
        spec = {
            "image": "img:v1",
            "replicas": 1,
            "gpu_count": 1,
            "env": {"MODEL": "llama", "MAX_LEN": "4096"},
        }
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        container = deployment.spec.template.spec.containers[0]
        assert container.env is not None
        # 2 user env vars + the injected PYTHONHASHSEED default
        assert len(container.env) == 3
        env_names = {e.name for e in container.env}
        assert "MODEL" in env_names
        assert "MAX_LEN" in env_names
        # PYTHONHASHSEED=0 is defaulted for deterministic prefix-cache hashing.
        env_by_name = {e.name: e.value for e in container.env}
        assert env_by_name["PYTHONHASHSEED"] == "0"

    def test_create_deployment_with_command_and_args(self):
        monitor = _make_monitor()
        spec = {
            "image": "img:v1",
            "replicas": 1,
            "gpu_count": 0,
            "command": ["python"],
            "args": ["-m", "serve"],
        }
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        container = deployment.spec.template.spec.containers[0]
        assert container.command == ["python"]
        assert container.args == ["-m", "serve"]

    def test_create_deployment_zero_gpu(self):
        """Zero GPU should not add GPU to resource requests."""
        monitor = _make_monitor()
        spec = {"image": "img:v1", "replicas": 1, "gpu_count": 0}
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        container = deployment.spec.template.spec.containers[0]
        assert "nvidia.com/gpu" not in (container.resources.limits or {})

    def test_create_deployment_custom_node_selector(self):
        monitor = _make_monitor()
        spec = {
            "image": "img:v1",
            "replicas": 1,
            "gpu_count": 1,
            "node_selector": {"zone": "us-east-1a"},
        }
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        assert deployment.spec.template.spec.node_selector == {"zone": "us-east-1a"}

    def test_create_deployment_custom_resources(self):
        monitor = _make_monitor()
        spec = {
            "image": "img:v1",
            "replicas": 1,
            "gpu_count": 1,
            "resources": {
                "requests": {"cpu": "8", "memory": "32Gi"},
                "limits": {"cpu": "16", "memory": "64Gi"},
            },
        }
        monitor._create_deployment("ep", "ns", spec)
        monitor.apps_v1.create_namespaced_deployment.assert_called_once()


# =============================================================================
# Create Service Tests
# =============================================================================


class TestCreateService:
    """Tests for _create_service."""

    def test_create_service_success(self):
        monitor = _make_monitor()
        spec = {"port": 8000}
        with patch("gco.services.inference_monitor.client.CustomObjectsApi") as custom_api:
            monitor._create_service("ep", "ns", spec)

        monitor.core_v1.create_namespaced_service.assert_called_once()
        service = monitor.core_v1.create_namespaced_service.call_args.args[1]
        assert service.spec.type == "ClusterIP"
        monitor.networking_v1.create_namespaced_ingress.assert_not_called()
        monitor.networking_v1.patch_namespaced_ingress.assert_not_called()
        custom_api.return_value.create_namespaced_custom_object.assert_not_called()

    def test_create_service_already_exists(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=409)
        spec = {"port": 8000}
        # Should not raise
        monitor._create_service("ep", "ns", spec)


# =============================================================================
# Reconcile Running — autoscaling, in-sync, promote to running
# =============================================================================


class TestReconcileRunningExtended:
    """Extended tests for _reconcile_running edge cases."""

    @pytest.mark.asyncio
    async def test_reconcile_creates_with_autoscaling(self):
        from kubernetes.client.rest import ApiException

        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)

        endpoint = {
            "endpoint_name": "auto-ep",
            "desired_state": "deploying",
            "target_regions": ["us-east-1"],
            "spec": {
                "image": "img:v1",
                "replicas": 2,
                "gpu_count": 1,
                "autoscaling": {"enabled": True, "metrics": [{"type": "cpu", "target": 70}]},
            },
            "namespace": "gco-inference",
        }
        with patch("gco.services.inference_monitor.client.AutoscalingV2Api"):
            result = await monitor._reconcile_endpoint(endpoint)

        assert result["action"] == "create"

    @pytest.mark.asyncio
    async def test_reconcile_in_sync_reports_running(self):
        """When replicas match and image matches, should report status."""
        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)

        deployment = MagicMock()
        deployment.spec.replicas = 2
        deployment.status.ready_replicas = 2
        deployment.spec.template.spec.containers = [MagicMock(image="img:v1")]
        monitor.apps_v1.read_namespaced_deployment.return_value = deployment

        endpoint = {
            "endpoint_name": "sync-ep",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1", "replicas": 2},
            "namespace": "gco-inference",
            "region_status": {},
        }
        result = await monitor._reconcile_endpoint(endpoint)
        # No action needed — everything in sync
        assert result is None
        mock_store.update_region_status.assert_called()

    @pytest.mark.asyncio
    async def test_reconcile_promotes_deploying_to_running(self):
        """When all regions running and state is deploying, promote."""
        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)

        deployment = MagicMock()
        deployment.spec.replicas = 1
        deployment.status.ready_replicas = 1
        deployment.spec.template.spec.containers = [MagicMock(image="img:v1")]
        monitor.apps_v1.read_namespaced_deployment.return_value = deployment

        endpoint = {
            "endpoint_name": "promote-ep",
            "desired_state": "deploying",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1", "replicas": 1},
            "namespace": "gco-inference",
            "region_status": {"us-east-1": {"state": "running"}},
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is None
        mock_store.update_desired_state.assert_called_with("promote-ep", "running")

    @pytest.mark.asyncio
    async def test_reconcile_stopped_already_zero(self):
        """Stopped endpoint already at 0 replicas — no action."""
        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)

        deployment = MagicMock()
        deployment.spec.replicas = 0
        monitor.apps_v1.read_namespaced_deployment.return_value = deployment

        endpoint = {
            "endpoint_name": "stopped-ep",
            "desired_state": "stopped",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is None

    @pytest.mark.asyncio
    async def test_reconcile_stopped_no_deployment(self):
        """Stopped endpoint with no deployment — no action."""
        from kubernetes.client.rest import ApiException

        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)

        endpoint = {
            "endpoint_name": "gone-ep",
            "desired_state": "stopped",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is None

    @pytest.mark.asyncio
    async def test_reconcile_deleted_no_deployment(self):
        """Deleted endpoint with no deployment — just update status."""
        from kubernetes.client.rest import ApiException

        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)

        endpoint = {
            "endpoint_name": "del-ep",
            "desired_state": "deleted",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is None
        mock_store.update_region_status.assert_called_with("del-ep", "us-east-1", "deleted")

    @pytest.mark.asyncio
    async def test_reconcile_not_target_region_no_deployment(self):
        """Not a target region and no deployment — skip."""
        from kubernetes.client.rest import ApiException

        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)

        endpoint = {
            "endpoint_name": "other-ep",
            "desired_state": "running",
            "target_regions": ["eu-west-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is None

    @pytest.mark.asyncio
    async def test_reconcile_unknown_desired_state(self):
        """Unknown desired state — no action."""
        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)

        endpoint = {
            "endpoint_name": "weird-ep",
            "desired_state": "unknown_state",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is None


# =============================================================================
# Start / Stop lifecycle
# =============================================================================


class TestStartStop:
    """Tests for start() and stop()."""

    def test_stop(self):
        monitor = _make_monitor()
        monitor._running = True
        monitor.stop()
        assert monitor._running is False

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        monitor = _make_monitor()
        monitor._running = True
        # Should return immediately
        await monitor.start()

    @pytest.mark.asyncio
    async def test_start_runs_reconcile_loop(self):
        """Start should run reconcile loop and stop when _running is set to False."""
        mock_store = MagicMock()
        mock_store.list_endpoints.return_value = []
        monitor = _make_monitor(mock_store)

        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                monitor._running = False

        with (
            patch.object(monitor, "_try_acquire_lease", return_value=True),
            patch("gco.services.inference_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            await monitor.start()

        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_start_handles_reconcile_error(self):
        """When reconcile() raises (not caught internally), errors_count increments."""
        mock_store = MagicMock()
        monitor = _make_monitor(mock_store)

        call_count = 0

        async def bad_reconcile():
            raise RuntimeError("unexpected crash")

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                monitor._running = False

        with (
            patch.object(monitor, "_try_acquire_lease", return_value=True),
            patch.object(monitor, "reconcile", side_effect=bad_reconcile),
            patch("gco.services.inference_monitor.asyncio.sleep", side_effect=fake_sleep),
        ):
            await monitor.start()

        assert monitor._errors_count >= 1

    @pytest.mark.asyncio
    async def test_start_sleep_interrupted(self):
        mock_store = MagicMock()
        mock_store.list_endpoints.return_value = []
        monitor = _make_monitor(mock_store)

        async def fail_sleep(seconds):
            raise Exception("interrupted")

        with (
            patch.object(monitor, "_try_acquire_lease", return_value=True),
            patch("gco.services.inference_monitor.asyncio.sleep", side_effect=fail_sleep),
        ):
            await monitor.start()

        # Should have broken out of the loop
        assert monitor._running is True  # was set to True at start


# =============================================================================
# Delete Resources extended
# =============================================================================


class TestDeleteResourcesExtended:
    """Extended tests for _delete_resources."""

    def test_delete_resources_logs_non_404_errors(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        monitor.apps_v1.delete_namespaced_deployment.side_effect = ApiException(status=500)
        monitor.core_v1.delete_namespaced_service.side_effect = ApiException(status=500)

        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api") as mock_hpa,
            patch("gco.services.inference_monitor.client.CustomObjectsApi") as mock_custom,
        ):
            mock_hpa.return_value.delete_namespaced_horizontal_pod_autoscaler.side_effect = (
                ApiException(status=500)
            )
            mock_custom.return_value.delete_namespaced_custom_object.side_effect = ApiException(
                status=500
            )
            # Should not raise, just log errors
            monitor._delete_resources("ep", "ns")


# =============================================================================
# Deployment helpers
# =============================================================================


class TestDeploymentHelpers:
    """Tests for _get_deployment, _get_deployment_image, _scale, _update_image."""

    def test_get_deployment_found(self):
        monitor = _make_monitor()
        mock_dep = MagicMock()
        monitor.apps_v1.read_namespaced_deployment.return_value = mock_dep
        assert monitor._get_deployment("ep", "ns") is mock_dep

    def test_get_deployment_not_found(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        assert monitor._get_deployment("ep", "ns") is None

    def test_get_deployment_raises_on_other_error(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=500)
        with pytest.raises(ApiException):
            monitor._get_deployment("ep", "ns")

    def test_get_deployment_image_no_containers(self):
        monitor = _make_monitor()
        deployment = MagicMock()
        deployment.spec.template.spec.containers = []
        assert monitor._get_deployment_image(deployment) is None

    def test_scale_deployment(self):
        monitor = _make_monitor()
        monitor._scale_deployment("ep", "ns", 5)
        monitor.apps_v1.patch_namespaced_deployment.assert_called_once_with(
            "ep", "ns", body={"spec": {"replicas": 5}}, _request_timeout=30
        )

    def test_update_deployment_image(self):
        monitor = _make_monitor()
        monitor._update_deployment_image("ep", "ns", "new:v2")
        monitor.apps_v1.patch_namespaced_deployment.assert_called_once()

    def test_deployment_exists_raises_on_non_404(self):
        from kubernetes.client.rest import ApiException

        monitor = _make_monitor()
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=500)
        with pytest.raises(ApiException):
            monitor._deployment_exists("ep", "ns")


# =============================================================================
# Main entry point
# =============================================================================


class TestMainFunction:
    """Tests for the main() async entry point."""

    @pytest.fixture(autouse=True)
    def _stub_metrics_server(self):
        # main() starts a Prometheus metrics HTTP server; entry-point unit tests
        # must not bind a real socket, so stub it out for the whole class.
        with patch("gco.services.service_metrics.start_metrics_server"):
            yield

    @pytest.mark.asyncio
    async def test_main_keyboard_interrupt(self):
        from gco.services.inference_monitor import main

        mock_monitor = MagicMock()
        mock_monitor.start = MagicMock(side_effect=KeyboardInterrupt)
        mock_monitor.get_metrics.return_value = {"cluster_id": "test"}

        with patch(
            "gco.services.inference_monitor.create_inference_monitor_from_env",
            return_value=mock_monitor,
        ):
            await main()

        mock_monitor.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_logs_metrics(self):
        from gco.services.inference_monitor import main

        call_count = 0

        async def fake_start():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt

        mock_monitor = MagicMock()
        mock_monitor.start = fake_start
        mock_monitor.get_metrics.return_value = {"cluster_id": "test"}
        mock_monitor._running = False

        with (
            patch(
                "gco.services.inference_monitor.create_inference_monitor_from_env",
                return_value=mock_monitor,
            ),
            patch("gco.services.inference_monitor.asyncio.sleep", side_effect=KeyboardInterrupt),
        ):
            await main()

    @pytest.mark.asyncio
    async def test_main_sigterm_stops_cleanly(self):
        """SIGTERM must stop the loop and return (pod rotations send SIGTERM).

        The monitor runs as PID 1 in its distroless container, where an
        unhandled SIGTERM is silently ignored and the kubelet escalates to
        SIGKILL after the grace period. main() installs a loop signal handler
        that flips the monitor's stop flag; this drives it end to end by
        delivering a real SIGTERM to the test process.
        """
        import asyncio
        import os
        import signal

        from gco.services.inference_monitor import main

        running = {"flag": True}

        async def fake_start():
            while running["flag"]:
                await asyncio.sleep(0.01)

        mock_monitor = MagicMock()
        mock_monitor.start = fake_start
        mock_monitor.stop = MagicMock(side_effect=lambda: running.update(flag=False))
        mock_monitor.get_metrics.return_value = {"cluster_id": "test"}

        with patch(
            "gco.services.inference_monitor.create_inference_monitor_from_env",
            return_value=mock_monitor,
        ):
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
            # A hang here (the pre-fix behavior: nothing ever breaks the loop)
            # fails via the timeout instead of wedging the suite.
            await asyncio.wait_for(main(), timeout=10)

        mock_monitor.stop.assert_called()


# =============================================================================
# InferenceManager add_region / remove_region
# =============================================================================


class TestInferenceManagerRegions:
    """Tests for InferenceManager add_region and remove_region."""

    @pytest.fixture
    def manager(self):
        with (
            patch("cli.inference.get_aws_client"),
            patch("cli.inference.get_config"),
        ):
            from cli.inference import InferenceManager

            mgr = InferenceManager(config=MagicMock())
        mgr._get_store = MagicMock()
        return mgr

    def test_add_region_success(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}
        manager._get_store.return_value = mock_store

        result = manager.add_region("ep", "eu-west-1")
        assert result is not None

    def test_add_region_already_present(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1", "eu-west-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}
        manager._get_store.return_value = mock_store

        result = manager.add_region("ep", "eu-west-1")
        assert result is not None

    def test_add_region_not_found(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None
        manager._get_store.return_value = mock_store

        result = manager.add_region("ghost", "us-east-1")
        assert result is None

    def test_add_region_error(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.side_effect = Exception("DDB error")
        manager._get_store.return_value = mock_store

        result = manager.add_region("ep", "eu-west-1")
        assert result is None

    def test_remove_region_success(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1", "eu-west-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}
        manager._get_store.return_value = mock_store

        result = manager.remove_region("ep", "eu-west-1")
        assert result is not None

    def test_remove_region_not_present(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}
        manager._get_store.return_value = mock_store

        result = manager.remove_region("ep", "eu-west-1")
        assert result is not None

    def test_remove_region_not_found(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = None
        manager._get_store.return_value = mock_store

        result = manager.remove_region("ghost", "us-east-1")
        assert result is None

    def test_remove_region_error(self, manager):
        mock_store = MagicMock()
        mock_store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "target_regions": ["us-east-1"],
        }
        mock_store._table.update_item.side_effect = Exception("DDB error")
        manager._get_store.return_value = mock_store

        result = manager.remove_region("ep", "us-east-1")
        assert result is None


# =============================================================================
# InferenceManager deploy with all optional params
# =============================================================================


class TestInferenceManagerDeployExtended:
    """Extended deploy tests for InferenceManager."""

    @pytest.fixture
    def manager(self):
        with (
            patch("cli.inference.get_aws_client"),
            patch("cli.inference.get_config"),
        ):
            from cli.inference import InferenceManager

            mgr = InferenceManager(config=MagicMock())
        mgr._get_store = MagicMock()
        return mgr

    def test_deploy_with_all_options(self, manager):
        mock_store = MagicMock()
        mock_store.create_endpoint.return_value = {"endpoint_name": "full-ep"}
        manager._get_store.return_value = mock_store

        result = manager.deploy(
            endpoint_name="full-ep",
            image="img:v1",
            target_regions=["us-east-1"],
            replicas=3,
            gpu_count=4,
            gpu_type="p5.48xlarge",
            port=9090,
            model_path="/models/llama",
            model_source="s3://bucket/models/llama",
            health_check_path="/healthz",
            env={"KEY": "VAL"},
            namespace="custom-ns",
            labels={"team": "ml"},
            autoscaling={"enabled": True, "min_replicas": 1, "max_replicas": 10},
        )

        assert result["endpoint_name"] == "full-ep"
        call_kwargs = mock_store.create_endpoint.call_args.kwargs
        spec = call_kwargs["spec"]
        assert spec["gpu_type"] == "p5.48xlarge"
        assert spec["model_path"] == "/models/llama"
        assert spec["model_source"] == "s3://bucket/models/llama"
        assert spec["env"] == {"KEY": "VAL"}
        assert spec["autoscaling"]["enabled"] is True


# ---------------------------------------------------------------------------
# _resolve_image_for_region — per-region URI selection on pod materialisation
# ---------------------------------------------------------------------------
#
# ``cli.inference.InferenceManager.deploy`` builds a per-region image-URI map
# under ``spec["region_image_uris"]``. The monitor running in each region
# must pick its own entry rather than always using the flat ``spec["image"]``
# URI, so each cluster pulls from its local ECR replica instead of crossing
# the WAN.


class TestResolveImageForRegion:
    """``_resolve_image_for_region`` picks the in-region replica when set."""

    def test_returns_per_region_uri_when_present(self) -> None:
        monitor = _make_monitor()
        # Default ``_make_monitor`` builds the monitor in ``us-east-1``.
        spec = {
            "image": "111111111111.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v1",
            "region_image_uris": {
                "us-east-1": "111111111111.dkr.ecr.us-east-1.amazonaws.com/gco/svc:v1",
                "us-west-2": "111111111111.dkr.ecr.us-west-2.amazonaws.com/gco/svc:v1",
            },
        }
        resolved = monitor._resolve_image_for_region(spec)
        assert resolved == "111111111111.dkr.ecr.us-east-1.amazonaws.com/gco/svc:v1"

    def test_falls_back_to_flat_image_when_region_absent(self) -> None:
        monitor = _make_monitor()
        spec = {
            "image": "global-registry/svc:v1",
            "region_image_uris": {
                "us-west-2": "us-west-2-replica/svc:v1",
            },
        }
        resolved = monitor._resolve_image_for_region(spec)
        assert resolved == "global-registry/svc:v1"

    def test_falls_back_when_no_map_present(self) -> None:
        monitor = _make_monitor()
        spec = {"image": "docker.io/library/python:3.14"}
        resolved = monitor._resolve_image_for_region(spec)
        assert resolved == "docker.io/library/python:3.14"

    def test_ignores_non_dict_region_image_uris(self) -> None:
        """A malformed map (e.g. accidentally serialised as a list) must
        not blow up — the helper falls back to the flat URI."""
        monitor = _make_monitor()
        spec = {
            "image": "fallback/img:v1",
            "region_image_uris": ["us-east-1", "us-west-2"],  # type: ignore[dict-item]
        }
        resolved = monitor._resolve_image_for_region(spec)
        assert resolved == "fallback/img:v1"

    def test_ignores_non_string_per_region_value(self) -> None:
        monitor = _make_monitor()
        spec = {
            "image": "fallback/img:v1",
            "region_image_uris": {"us-east-1": None},
        }
        resolved = monitor._resolve_image_for_region(spec)
        assert resolved == "fallback/img:v1"

    def test_ignores_empty_string_per_region_value(self) -> None:
        monitor = _make_monitor()
        spec = {
            "image": "fallback/img:v1",
            "region_image_uris": {"us-east-1": ""},
        }
        resolved = monitor._resolve_image_for_region(spec)
        assert resolved == "fallback/img:v1"

    def test_create_deployment_uses_per_region_uri(self) -> None:
        """The full pipeline through ``_create_deployment`` picks the
        regional URI — verifies the helper is wired into the spot where
        the TODO previously lived."""
        monitor = _make_monitor()
        spec = {
            "image": "111111111111.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v1",
            "region_image_uris": {
                "us-east-1": "111111111111.dkr.ecr.us-east-1.amazonaws.com/gco/svc:v1",
            },
            "replicas": 1,
            "port": 8000,
            "gpu_count": 0,
        }
        monitor._create_deployment("ep", "ns", spec)
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        container = deployment.spec.template.spec.containers[0]
        assert container.image == "111111111111.dkr.ecr.us-east-1.amazonaws.com/gco/svc:v1"
