"""
Tests for canary deployment reconciliation in gco/services/inference_monitor.py.

Covers _reconcile_canary — validating the desired canary, creating or
updating its Deployment and Service, deriving an isolated canary spec,
and publishing observed image/readiness status for routing behind the shared
``gco-system/gco-gateway`` ``/inference`` HTTPRoute to
``gco-system/inference-proxy`` — plus removal of historical direct Ingresses and
cleanup when the canary field is removed. Kubernetes APIs are fully mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException


@pytest.fixture
def monitor():
    """Create an InferenceMonitor with mocked K8s clients."""
    with patch("gco.services.inference_monitor.config"):
        from gco.services.inference_monitor import InferenceMonitor

        m = InferenceMonitor.__new__(InferenceMonitor)
        m.apps_v1 = MagicMock()
        m.core_v1 = MagicMock()
        m.networking_v1 = MagicMock()
        m.store = MagicMock()
        m.region = "us-east-1"
        m.namespace = "gco-inference"
        m.cluster_name = "gco-us-east-1"
        m._k8s_timeout = 30
        return m


class TestReconcileCanary:
    """Tests for _reconcile_canary method."""

    def test_creates_canary_deployment_with_isolated_spec_when_missing(self, monitor):
        canary = {"image": " new:v2 ", "weight": 20, "replicas": 1}
        original_spec = {
            "image": "old:v1",
            "port": 8000,
            "replicas": 2,
            "gpu_count": 1,
            "region_image_uris": {"us-east-1": "regional-old:v1"},
            "canary": canary,
        }
        endpoint = {"ingress_path": "/inference/ep"}

        with (
            patch.object(monitor, "_get_deployment", return_value=None),
            patch.object(monitor, "_create_deployment") as mock_create,
            patch.object(monitor, "_create_service") as mock_svc,
            patch.object(monitor, "_cleanup_legacy_canary_ingress") as mock_cleanup,
        ):
            status = monitor._reconcile_canary("ep", "ns", original_spec, canary, endpoint)

        submitted_spec = mock_create.call_args.args[2]
        assert mock_create.call_args.args[:2] == ("ep-canary", "ns")
        assert mock_svc.call_args.args[:2] == ("ep-canary", "ns")
        assert submitted_spec["image"] == "new:v2"
        assert submitted_spec["replicas"] == 1
        assert "canary" not in submitted_spec
        assert "region_image_uris" not in submitted_spec
        assert "canary" in original_spec
        assert "region_image_uris" in original_spec
        mock_cleanup.assert_called_once_with("ep", "ns")
        assert status == {
            "state": "creating",
            "image": "new:v2",
            "weight": 20,
            "replicas_ready": 0,
            "replicas_desired": 1,
        }

    def test_updates_canary_image_when_changed(self, monitor):
        mock_deployment = MagicMock()
        mock_deployment.spec.replicas = 1
        mock_deployment.status.ready_replicas = 1

        with (
            patch.object(monitor, "_get_deployment", return_value=mock_deployment),
            patch.object(monitor, "_get_deployment_image", return_value="old:v1"),
            patch.object(monitor, "_update_deployment_image") as mock_update,
            patch.object(monitor, "_scale_deployment"),
            patch.object(monitor, "_cleanup_legacy_canary_ingress"),
        ):
            status = monitor._reconcile_canary(
                "ep",
                "ns",
                {"image": "old:v1", "canary": {"image": "new:v2"}},
                {"image": "new:v2", "weight": 10, "replicas": 1},
                {"ingress_path": "/inference/ep"},
            )

        mock_update.assert_called_once_with("ep-canary", "ns", "new:v2")
        assert status["state"] == "updating"
        assert status["replicas_ready"] == 0
        assert status["image"] == "new:v2"

    def test_scales_canary_when_replicas_changed(self, monitor):
        mock_deployment = MagicMock()
        mock_deployment.spec.replicas = 1
        mock_deployment.status.ready_replicas = 1

        with (
            patch.object(monitor, "_get_deployment", return_value=mock_deployment),
            patch.object(monitor, "_get_deployment_image", return_value="new:v2"),
            patch.object(monitor, "_scale_deployment") as mock_scale,
            patch.object(monitor, "_cleanup_legacy_canary_ingress"),
        ):
            status = monitor._reconcile_canary(
                "ep",
                "ns",
                {"image": "old:v1", "canary": {"image": "new:v2"}},
                {"image": "new:v2", "weight": 10, "replicas": 3},
                {"ingress_path": "/inference/ep"},
            )

        mock_scale.assert_called_once_with("ep-canary", "ns", 3)
        assert status["state"] == "updating"
        assert status["replicas_ready"] == 1
        assert status["replicas_desired"] == 3

    def test_reports_running_only_when_all_canary_replicas_are_ready(self, monitor):
        mock_deployment = MagicMock()
        mock_deployment.spec.replicas = 2
        mock_deployment.status.ready_replicas = 2

        with (
            patch.object(monitor, "_get_deployment", return_value=mock_deployment),
            patch.object(monitor, "_get_deployment_image", return_value="new:v2"),
            patch.object(monitor, "_cleanup_legacy_canary_ingress"),
        ):
            status = monitor._reconcile_canary(
                "ep",
                "ns",
                {"image": "old:v1", "canary": {"image": "new:v2"}},
                {"image": "new:v2", "weight": 25, "replicas": 2},
                {"ingress_path": "/inference/ep"},
            )

        assert status == {
            "state": "running",
            "image": "new:v2",
            "weight": 25,
            "replicas_ready": 2,
            "replicas_desired": 2,
        }

    @pytest.mark.parametrize(
        ("canary", "message"),
        [
            ({"image": "", "weight": 10, "replicas": 1}, "canary.image must be a non-empty string"),
            (
                {"image": None, "weight": 10, "replicas": 1},
                "canary.image must be a non-empty string",
            ),
            (
                {"image": "new:v2", "weight": 10, "replicas": 0},
                "canary.replicas must be a positive integer",
            ),
            (
                {"image": "new:v2", "weight": 10, "replicas": True},
                "canary.replicas must be a positive integer",
            ),
            (
                {"image": "new:v2", "weight": 0, "replicas": 1},
                "canary.weight must be an integer between 1 and 99",
            ),
            (
                {"image": "new:v2", "weight": 100, "replicas": 1},
                "canary.weight must be an integer between 1 and 99",
            ),
            (
                {"image": "new:v2", "weight": True, "replicas": 1},
                "canary.weight must be an integer between 1 and 99",
            ),
        ],
    )
    def test_rejects_invalid_canary_fields(self, monitor, canary, message):
        with pytest.raises(ValueError, match=message):
            monitor._reconcile_canary(
                "ep",
                "ns",
                {"image": "old:v1", "canary": canary},
                canary,
                {"ingress_path": "/inference/ep"},
            )

        monitor.apps_v1.create_namespaced_deployment.assert_not_called()
        monitor.core_v1.create_namespaced_service.assert_not_called()


class TestCleanupCanary:
    """Tests for _cleanup_canary method."""

    def test_deletes_canary_resources(self, monitor):
        monitor._cleanup_canary("ep", "ns")

        monitor.apps_v1.delete_namespaced_deployment.assert_called_once_with(
            "ep-canary", "ns", _request_timeout=30
        )
        monitor.core_v1.delete_namespaced_service.assert_called_once_with(
            "ep-canary", "ns", _request_timeout=30
        )

    def test_handles_404_gracefully(self, monitor):
        monitor.apps_v1.delete_namespaced_deployment.side_effect = ApiException(status=404)
        monitor.core_v1.delete_namespaced_service.side_effect = ApiException(status=404)

        # Should not raise
        monitor._cleanup_canary("ep", "ns")

    def test_logs_non_404_errors(self, monitor):
        monitor.apps_v1.delete_namespaced_deployment.side_effect = ApiException(status=500)

        # Should not raise, just log
        monitor._cleanup_canary("ep", "ns")


class TestLegacyCanaryIngressCleanup:
    """Canary reconciliation removes the historical unauthenticated rule."""

    def test_deletes_only_the_legacy_primary_ingress(self, monitor):
        with patch.object(monitor, "_delete_legacy_inference_ingress") as mock_delete:
            monitor._cleanup_legacy_canary_ingress("ep", "ns")

        mock_delete.assert_called_once_with("inference-ep", "ns")
        monitor.networking_v1.patch_namespaced_ingress.assert_not_called()
        monitor.networking_v1.create_namespaced_ingress.assert_not_called()

    def test_propagates_legacy_ingress_cleanup_errors(self, monitor):
        with (
            patch.object(
                monitor,
                "_delete_legacy_inference_ingress",
                side_effect=ApiException(status=500),
            ),
            pytest.raises(ApiException),
        ):
            monitor._cleanup_legacy_canary_ingress("ep", "ns")


class TestCapacityTypeNodeSelector:
    """Tests for capacity_type node selector in _create_deployment."""

    def test_spot_capacity_type_sets_node_selector(self, monitor):
        spec = {
            "image": "img:v1",
            "port": 8000,
            "replicas": 1,
            "gpu_count": 1,
            "health_check_path": "/health",
            "capacity_type": "spot",
        }

        monitor._create_deployment("ep", "ns", spec)

        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        node_selector = deployment.spec.template.spec.node_selector
        assert node_selector["karpenter.sh/capacity-type"] == "spot"

    def test_on_demand_capacity_type_sets_node_selector(self, monitor):
        spec = {
            "image": "img:v1",
            "port": 8000,
            "replicas": 1,
            "gpu_count": 1,
            "health_check_path": "/health",
            "capacity_type": "on-demand",
        }

        monitor._create_deployment("ep", "ns", spec)

        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        node_selector = deployment.spec.template.spec.node_selector
        assert node_selector["karpenter.sh/capacity-type"] == "on-demand"

    def test_no_capacity_type_no_karpenter_selector(self, monitor):
        spec = {
            "image": "img:v1",
            "port": 8000,
            "replicas": 1,
            "gpu_count": 1,
            "health_check_path": "/health",
        }

        monitor._create_deployment("ep", "ns", spec)

        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args[0][1]
        node_selector = deployment.spec.template.spec.node_selector
        assert "karpenter.sh/capacity-type" not in (node_selector or {})


class TestInferenceEndpointSpecCapacityType:
    """Tests for capacity_type in InferenceEndpointSpec."""

    def test_spec_with_capacity_type(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec(image="img:v1", capacity_type="spot")
        d = spec.to_dict()
        assert d["capacity_type"] == "spot"

    def test_spec_without_capacity_type(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec(image="img:v1")
        d = spec.to_dict()
        assert "capacity_type" not in d

    def test_spec_from_dict_with_capacity_type(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec.from_dict({"image": "img:v1", "capacity_type": "on-demand"})
        assert spec.capacity_type == "on-demand"

    def test_spec_from_dict_without_capacity_type(self):
        from gco.models.inference_models import InferenceEndpointSpec

        spec = InferenceEndpointSpec.from_dict({"image": "img:v1"})
        assert spec.capacity_type is None


class TestInferenceStoreExtended:
    """Extended tests for InferenceEndpointStore."""

    @patch("gco.services.inference_store.boto3")
    def test_delete_endpoint_success(self, mock_boto):
        from gco.services.inference_store import InferenceEndpointStore

        store = InferenceEndpointStore(table_name="test-table", region="us-east-1")
        result = store.delete_endpoint("ep1")
        assert result is True

    @patch("gco.services.inference_store.boto3")
    def test_delete_endpoint_not_found(self, mock_boto):
        from botocore.exceptions import ClientError

        from gco.services.inference_store import InferenceEndpointStore

        store = InferenceEndpointStore(table_name="test-table", region="us-east-1")
        store._table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem"
        )
        result = store.delete_endpoint("nonexistent")
        assert result is False

    @patch("gco.services.inference_store.boto3")
    def test_update_region_status(self, mock_boto):
        from gco.services.inference_store import InferenceEndpointStore

        store = InferenceEndpointStore(table_name="test-table", region="us-east-1")
        # Should not raise
        store.update_region_status(
            "ep1", "us-east-1", "running", replicas_ready=2, replicas_desired=2
        )
        store._table.update_item.assert_called_once()

    @patch("gco.services.inference_store.boto3")
    def test_update_region_status_with_error(self, mock_boto):
        from gco.services.inference_store import InferenceEndpointStore

        store = InferenceEndpointStore(table_name="test-table", region="us-east-1")
        store.update_region_status("ep1", "us-east-1", "error", error="OOM killed")
        call_args = store._table.update_item.call_args[1]
        assert "error" in str(call_args)

    @patch("gco.services.inference_store.boto3")
    def test_scale_endpoint_not_found(self, mock_boto):
        from botocore.exceptions import ClientError

        from gco.services.inference_store import InferenceEndpointStore

        store = InferenceEndpointStore(table_name="test-table", region="us-east-1")
        store._table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        result = store.scale_endpoint("nonexistent", 3)
        assert result is None
