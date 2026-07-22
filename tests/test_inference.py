"""
Tests for the inference endpoint feature.

Covers the EndpointState / RegionSyncState / RegionStatus /
InferenceEndpoint / InferenceEndpointSpec dataclasses and their
to_dict/from_dict round-trips; the InferenceEndpointStore's DynamoDB
serialization helpers and CRUD methods (including Decimal coercion);
the reconciliation monitor that drives regional sync state; and the
CLI manager that calls into the store. Uses moto-backed DynamoDB
fixtures so store tests run against a realistic schema without AWS.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from gco.models.inference_models import (
    EndpointState,
    InferenceEndpoint,
    InferenceEndpointSpec,
    RegionStatus,
    RegionSyncState,
)
from gco.services.inference_store import (
    InferenceEndpointStore,
    _deserialize_from_dynamo,
    _serialize_for_dynamo,
)

# =============================================================================
# Model Tests — EndpointState & RegionSyncState enums
# =============================================================================


class TestEndpointState:
    """Tests for EndpointState enum."""

    def test_values(self):
        assert EndpointState.DEPLOYING.value == "deploying"
        assert EndpointState.RUNNING.value == "running"
        assert EndpointState.STOPPED.value == "stopped"
        assert EndpointState.DELETED.value == "deleted"

    def test_is_string_enum(self):
        assert isinstance(EndpointState.RUNNING, str)
        assert EndpointState.RUNNING == "running"


class TestRegionSyncState:
    """Tests for RegionSyncState enum."""

    def test_values(self):
        assert RegionSyncState.PENDING.value == "pending"
        assert RegionSyncState.CREATING.value == "creating"
        assert RegionSyncState.RUNNING.value == "running"
        assert RegionSyncState.UPDATING.value == "updating"
        assert RegionSyncState.STOPPING.value == "stopping"
        assert RegionSyncState.STOPPED.value == "stopped"
        assert RegionSyncState.DELETING.value == "deleting"
        assert RegionSyncState.DELETED.value == "deleted"
        assert RegionSyncState.ERROR.value == "error"

    def test_is_string_enum(self):
        assert isinstance(RegionSyncState.RUNNING, str)
        assert RegionSyncState.RUNNING == "running"


# =============================================================================
# Model Tests — InferenceEndpointSpec
# =============================================================================


class TestInferenceEndpointSpec:
    """Tests for InferenceEndpointSpec dataclass."""

    def test_defaults(self):
        spec = InferenceEndpointSpec(image="vllm/vllm:latest")
        assert spec.image == "vllm/vllm:latest"
        assert spec.port == 8000
        assert spec.replicas == 1
        assert spec.gpu_count == 1
        assert spec.gpu_type is None
        assert spec.model_path is None
        assert spec.health_check_path == "/health"
        assert spec.env == {}
        assert spec.resources == {}
        assert spec.command is None
        assert spec.args is None
        assert spec.tolerations is None
        assert spec.node_selector is None

    def test_to_dict_minimal(self):
        spec = InferenceEndpointSpec(image="img:v1")
        d = spec.to_dict()
        assert d == {
            "image": "img:v1",
            "port": 8000,
            "replicas": 1,
            "gpu_count": 1,
            "health_check_path": "/health",
        }
        # Optional fields should be absent
        for key in (
            "gpu_type",
            "model_path",
            "env",
            "resources",
            "command",
            "args",
            "tolerations",
            "node_selector",
        ):
            assert key not in d

    def test_to_dict_all_optional_fields(self):
        spec = InferenceEndpointSpec(
            image="img:v2",
            port=9090,
            replicas=4,
            gpu_count=2,
            gpu_type="g5.xlarge",
            model_path="/models/llama",
            health_check_path="/ready",
            env={"MODEL": "llama"},
            resources={"requests": {"cpu": "2"}},
            command=["python"],
            args=["-m", "serve"],
            tolerations=[{"key": "gpu", "effect": "NoSchedule"}],
            node_selector={"gpu": "true"},
        )
        d = spec.to_dict()
        assert d["gpu_type"] == "g5.xlarge"
        assert d["model_path"] == "/models/llama"
        assert d["env"] == {"MODEL": "llama"}
        assert d["resources"] == {"requests": {"cpu": "2"}}
        assert d["command"] == ["python"]
        assert d["args"] == ["-m", "serve"]
        assert d["tolerations"] == [{"key": "gpu", "effect": "NoSchedule"}]
        assert d["node_selector"] == {"gpu": "true"}

    def test_from_dict_minimal(self):
        spec = InferenceEndpointSpec.from_dict({"image": "img:v1"})
        assert spec.image == "img:v1"
        assert spec.port == 8000
        assert spec.replicas == 1

    def test_from_dict_full(self):
        data = {
            "image": "img:v3",
            "port": 3000,
            "replicas": 8,
            "gpu_count": 4,
            "gpu_type": "p4d.24xlarge",
            "model_path": "/weights",
            "health_check_path": "/healthz",
            "env": {"KEY": "VAL"},
            "resources": {"limits": {"memory": "64Gi"}},
            "command": ["bash"],
            "args": ["-c", "serve"],
            "tolerations": [{"key": "k"}],
            "node_selector": {"zone": "a"},
        }
        spec = InferenceEndpointSpec.from_dict(data)
        assert spec.image == "img:v3"
        assert spec.port == 3000
        assert spec.replicas == 8
        assert spec.gpu_count == 4
        assert spec.gpu_type == "p4d.24xlarge"
        assert spec.model_path == "/weights"
        assert spec.command == ["bash"]
        assert spec.node_selector == {"zone": "a"}

    def test_roundtrip(self):
        original = InferenceEndpointSpec(
            image="test:1",
            port=5000,
            replicas=3,
            gpu_count=2,
            gpu_type="g5.2xlarge",
            model_path="/m",
            env={"A": "B"},
        )
        rebuilt = InferenceEndpointSpec.from_dict(original.to_dict())
        assert rebuilt.image == original.image
        assert rebuilt.port == original.port
        assert rebuilt.replicas == original.replicas
        assert rebuilt.gpu_type == original.gpu_type
        assert rebuilt.env == original.env


# =============================================================================
# Model Tests — RegionStatus
# =============================================================================


class TestRegionStatus:
    """Tests for RegionStatus dataclass."""

    def test_to_dict_required_only(self):
        rs = RegionStatus(region="us-east-1")
        d = rs.to_dict()
        assert d == {
            "region": "us-east-1",
            "state": "pending",
            "replicas_ready": 0,
            "replicas_desired": 0,
        }
        assert "last_sync" not in d
        assert "error" not in d
        assert "endpoint_url" not in d

    def test_to_dict_with_optional_fields(self):
        rs = RegionStatus(
            region="eu-west-1",
            state="running",
            replicas_ready=3,
            replicas_desired=3,
            last_sync="2024-01-01T00:00:00Z",
            error="timeout",
            endpoint_url="https://example.com/inference/my-ep",
        )
        d = rs.to_dict()
        assert d["last_sync"] == "2024-01-01T00:00:00Z"
        assert d["error"] == "timeout"
        assert d["endpoint_url"] == "https://example.com/inference/my-ep"


# =============================================================================
# Model Tests — InferenceEndpoint
# =============================================================================


class TestInferenceEndpoint:
    """Tests for InferenceEndpoint dataclass."""

    def test_auto_generated_ingress_path(self):
        ep = InferenceEndpoint(endpoint_name="my-model")
        assert ep.ingress_path == "/inference/my-model"

    def test_explicit_ingress_path_preserved(self):
        ep = InferenceEndpoint(endpoint_name="my-model", ingress_path="/custom/path")
        assert ep.ingress_path == "/custom/path"

    def test_spec_dict_converted_to_object(self):
        ep = InferenceEndpoint(
            endpoint_name="ep1",
            spec={"image": "img:v1", "port": 9090},
        )
        assert isinstance(ep.spec, InferenceEndpointSpec)
        assert ep.spec.image == "img:v1"
        assert ep.spec.port == 9090

    def test_spec_object_kept(self):
        spec = InferenceEndpointSpec(image="img:v2")
        ep = InferenceEndpoint(endpoint_name="ep2", spec=spec)
        assert ep.spec is spec

    def test_to_dict(self):
        ep = InferenceEndpoint(
            endpoint_name="ep3",
            desired_state="running",
            target_regions=["us-east-1", "us-west-2"],
            spec={"image": "img:v1"},
            labels={"team": "ml"},
        )
        d = ep.to_dict()
        assert d["endpoint_name"] == "ep3"
        assert d["desired_state"] == "running"
        assert d["target_regions"] == ["us-east-1", "us-west-2"]
        assert d["ingress_path"] == "/inference/ep3"
        assert d["labels"] == {"team": "ml"}
        assert isinstance(d["spec"], dict)
        assert d["spec"]["image"] == "img:v1"

    def test_from_dict(self):
        data = {
            "endpoint_name": "ep4",
            "desired_state": "stopped",
            "target_regions": ["eu-west-1"],
            "namespace": "custom-ns",
            "spec": {"image": "img:v3", "replicas": 5},
            "ingress_path": "/custom",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "created_by": "user",
            "region_status": {"eu-west-1": {"state": "running"}},
            "labels": {"env": "prod"},
        }
        ep = InferenceEndpoint.from_dict(data)
        assert ep.endpoint_name == "ep4"
        assert ep.desired_state == "stopped"
        assert ep.namespace == "custom-ns"
        assert isinstance(ep.spec, InferenceEndpointSpec)
        assert ep.spec.replicas == 5
        assert ep.ingress_path == "/custom"
        assert ep.created_by == "user"
        assert ep.labels == {"env": "prod"}

    def test_from_dict_defaults(self):
        ep = InferenceEndpoint.from_dict(
            {"endpoint_name": "minimal", "spec": {"image": "placeholder:latest"}}
        )
        assert ep.desired_state == "deploying"
        assert ep.target_regions == []
        assert ep.namespace == "gco-inference"
        assert ep.ingress_path == "/inference/minimal"


# =============================================================================
# Serialization helpers
# =============================================================================


class TestSerializationHelpers:
    """Tests for _serialize_for_dynamo and _deserialize_from_dynamo."""

    def test_serialize_float_to_string(self):
        assert _serialize_for_dynamo(3.14) == "3.14"

    def test_serialize_int_unchanged(self):
        assert _serialize_for_dynamo(42) == 42

    def test_serialize_nested(self):
        result = _serialize_for_dynamo({"a": [1, 2.5], "b": {"c": 0.1}})
        assert result == {"a": [1, "2.5"], "b": {"c": "0.1"}}

    def test_serialize_string_unchanged(self):
        assert _serialize_for_dynamo("hello") == "hello"

    def test_deserialize_decimal_int(self):
        item = {"replicas": Decimal("3"), "name": "ep"}
        result = _deserialize_from_dynamo(item)
        assert result["replicas"] == 3
        assert isinstance(result["replicas"], int)

    def test_deserialize_decimal_float(self):
        item = {"ratio": Decimal("3.14")}
        result = _deserialize_from_dynamo(item)
        assert result["ratio"] == 3.14
        assert isinstance(result["ratio"], float)

    def test_deserialize_nested(self):
        item = {
            "spec": {"replicas": Decimal("2"), "items": [Decimal("1"), Decimal("2.5")]},
        }
        result = _deserialize_from_dynamo(item)
        assert result["spec"]["replicas"] == 2
        assert result["spec"]["items"] == [1, 2.5]


# =============================================================================
# InferenceEndpointStore Tests (moto)
# =============================================================================


class TestInferenceEndpointStore:
    """Tests for InferenceEndpointStore using moto-mocked DynamoDB."""

    @pytest.fixture
    def dynamodb_table(self):
        import boto3
        from moto import mock_aws

        with mock_aws():
            dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
            table = dynamodb.create_table(
                TableName="gco-inference-endpoints",
                KeySchema=[{"AttributeName": "endpoint_name", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "endpoint_name", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            table.wait_until_exists()
            yield table

    @pytest.fixture
    def store(self, dynamodb_table):
        store = InferenceEndpointStore(table_name="gco-inference-endpoints", region="us-east-1")
        store._table = dynamodb_table
        return store

    # -- create_endpoint --

    def test_create_endpoint_success(self, store):
        result = store.create_endpoint(
            endpoint_name="my-ep",
            spec={"image": "img:v1", "replicas": 2},
            target_regions=["us-east-1", "us-west-2"],
            namespace="gco-inference",
            labels={"team": "ml"},
            created_by="tester",
        )
        assert result["endpoint_name"] == "my-ep"
        assert result["desired_state"] == "deploying"
        assert result["target_regions"] == ["us-east-1", "us-west-2"]
        assert result["labels"] == {"team": "ml"}
        assert result["created_by"] == "tester"
        assert result["ingress_path"] == "/inference/my-ep"
        assert "created_at" in result
        assert "updated_at" in result

    def test_create_endpoint_duplicate_raises(self, store):
        store.create_endpoint(
            endpoint_name="dup-ep",
            spec={"image": "img:v1"},
            target_regions=["us-east-1"],
        )
        with pytest.raises(ValueError, match="already exists"):
            store.create_endpoint(
                endpoint_name="dup-ep",
                spec={"image": "img:v2"},
                target_regions=["us-west-2"],
            )

    # -- get_endpoint --

    def test_get_endpoint_found(self, store):
        store.create_endpoint(
            endpoint_name="get-ep",
            spec={"image": "img:v1"},
            target_regions=["us-east-1"],
        )
        result = store.get_endpoint("get-ep")
        assert result is not None
        assert result["endpoint_name"] == "get-ep"

    def test_get_endpoint_not_found(self, store):
        assert store.get_endpoint("nonexistent") is None

    # -- list_endpoints --

    def test_list_endpoints_all(self, store):
        store.create_endpoint("ep-a", {"image": "a"}, ["us-east-1"])
        store.create_endpoint("ep-b", {"image": "b"}, ["us-west-2"])
        result = store.list_endpoints()
        assert len(result) == 2

    def test_list_endpoints_filtered_by_state(self, store):
        store.create_endpoint("ep-c", {"image": "c"}, ["us-east-1"])
        store.update_desired_state("ep-c", "running")
        store.create_endpoint("ep-d", {"image": "d"}, ["us-east-1"])
        result = store.list_endpoints(desired_state="running")
        assert len(result) == 1
        assert result[0]["endpoint_name"] == "ep-c"

    def test_list_endpoints_filtered_by_region(self, store):
        store.create_endpoint("ep-e", {"image": "e"}, ["us-east-1"])
        store.create_endpoint("ep-f", {"image": "f"}, ["eu-west-1"])
        result = store.list_endpoints(target_region="eu-west-1")
        assert len(result) == 1
        assert result[0]["endpoint_name"] == "ep-f"

    # -- update_desired_state --

    def test_update_desired_state_success(self, store):
        store.create_endpoint("state-ep", {"image": "img"}, ["us-east-1"])
        result = store.update_desired_state("state-ep", "running")
        assert result is not None
        assert result["desired_state"] == "running"

    def test_update_desired_state_not_found(self, store):
        result = store.update_desired_state("ghost", "running")
        assert result is None

    # -- update_spec --

    def test_update_spec_success(self, store):
        store.create_endpoint("spec-ep", {"image": "old"}, ["us-east-1"])
        result = store.update_spec("spec-ep", {"image": "new", "replicas": 5})
        assert result is not None
        assert result["desired_state"] == "deploying"  # reset on spec change

    # -- update_region_status --

    def test_update_region_status_success(self, store):
        store.create_endpoint("rs-ep", {"image": "img"}, ["us-east-1"])
        store.update_region_status(
            "rs-ep", "us-east-1", "running", replicas_ready=2, replicas_desired=2
        )
        ep = store.get_endpoint("rs-ep")
        assert "us-east-1" in ep["region_status"]
        status = ep["region_status"]["us-east-1"]
        assert status["state"] == "running"
        assert status["replicas_ready"] == 2

    def test_update_region_status_with_error(self, store):
        store.create_endpoint("rs-err", {"image": "img"}, ["us-east-1"])
        store.update_region_status("rs-err", "us-east-1", "error", error="OOMKilled")
        ep = store.get_endpoint("rs-err")
        assert ep["region_status"]["us-east-1"]["error"] == "OOMKilled"

    # -- delete_endpoint --

    def test_delete_endpoint_success(self, store):
        store.create_endpoint("del-ep", {"image": "img"}, ["us-east-1"])
        assert store.delete_endpoint("del-ep") is True
        assert store.get_endpoint("del-ep") is None

    def test_delete_endpoint_not_found(self, store):
        assert store.delete_endpoint("nope") is False

    # -- scale_endpoint --

    def test_scale_endpoint_success(self, store):
        store.create_endpoint("scale-ep", {"image": "img", "replicas": 1}, ["us-east-1"])
        result = store.scale_endpoint("scale-ep", 5)
        assert result is not None

    def test_scale_endpoint_not_found(self, store):
        result = store.scale_endpoint("missing", 3)
        assert result is None


# =============================================================================
# InferenceMonitor Tests
# =============================================================================


class TestInferenceMonitor:
    """Tests for InferenceMonitor reconciliation controller."""

    @pytest.fixture
    def mock_store(self):
        return MagicMock()

    @pytest.fixture
    def monitor(self, mock_store):
        with (
            patch("gco.services.inference_monitor.config.load_incluster_config"),
            patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
            patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
            patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_net,
            patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
        ):
            from gco.services.inference_monitor import InferenceMonitor

            m = InferenceMonitor(
                cluster_id="test-cluster",
                region="us-east-1",
                store=mock_store,
                namespace="gco-inference",
                reconcile_interval=5,
            )
            # Replace k8s clients with the mocks
            m.apps_v1 = mock_apps.return_value
            m.core_v1 = mock_core.return_value
            m.networking_v1 = mock_net.return_value
            yield m

    # -- _deployment_exists --

    def test_deployment_exists_true(self, monitor):
        monitor.apps_v1.read_namespaced_deployment.return_value = MagicMock()
        assert monitor._deployment_exists("ep", "ns") is True

    def test_deployment_exists_false(self, monitor):
        from kubernetes.client.rest import ApiException

        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        assert monitor._deployment_exists("ep", "ns") is False

    # -- _delete_resources --

    def test_delete_resources_handles_404(self, monitor):
        from kubernetes.client.rest import ApiException

        monitor.apps_v1.delete_namespaced_deployment.side_effect = ApiException(status=404)
        monitor.core_v1.delete_namespaced_service.side_effect = ApiException(status=404)
        monitor.core_v1.delete_namespaced_config_map.side_effect = ApiException(status=404)
        monitor.networking_v1.delete_namespaced_ingress.side_effect = ApiException(status=404)
        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api") as mock_hpa_api,
            patch("gco.services.inference_monitor.client.CustomObjectsApi") as mock_custom_api,
        ):
            mock_hpa_api.return_value.delete_namespaced_horizontal_pod_autoscaler.side_effect = (
                ApiException(status=404)
            )
            mock_custom_api.return_value.delete_namespaced_custom_object.side_effect = ApiException(
                status=404
            )
            # Should not raise
            monitor._delete_resources("ep", "ns")

    def test_delete_resources_calls_all(self, monitor):
        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
            patch("gco.services.inference_monitor.client.CustomObjectsApi"),
        ):
            monitor._delete_resources("ep", "ns")
        # Primary deployment delete (canary cleanup also calls delete for ep-canary)
        monitor.apps_v1.delete_namespaced_deployment.assert_any_call(
            "ep", "ns", _request_timeout=30
        )
        monitor.core_v1.delete_namespaced_service.assert_any_call("ep", "ns", _request_timeout=30)
        # The historical single-instance and proxy Ingress names are both
        # attempted so upgrades cannot leave either direct route behind.
        monitor.networking_v1.delete_namespaced_ingress.assert_any_call(
            "inference-ep", "ns", _request_timeout=30
        )

    def test_delete_resources_tears_down_mooncake_topology(self, monitor):
        """A disaggregated endpoint's role/proxy resources are all torn down.

        Regression: _reconcile_deleted/_delete_resources originally only deleted
        part of the split topology. Deletion must remove the prefill/decode/proxy
        Deployments and Services, the proxy program and transport ConfigMaps,
        historical direct Ingresses, native or KEDA per-role autoscalers, and an
        auto-managed admin Secret. The shared regional mooncake-master and a
        user-named admin Secret must NOT be deleted.
        """
        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api") as mock_hpa_api,
            patch("gco.services.inference_monitor.client.CustomObjectsApi") as mock_custom_api,
        ):
            hpa = mock_hpa_api.return_value
            custom = mock_custom_api.return_value
            monitor._delete_resources(
                "ep",
                "ns",
                {"mooncake": {"mode": "disaggregated", "proxy": {}}},
            )

        deleted_deploys = {
            c.args[0] for c in monitor.apps_v1.delete_namespaced_deployment.call_args_list
        }
        assert {"ep", "ep-prefill", "ep-decode", "ep-proxy"}.issubset(deleted_deploys)
        # The shared master is region-wide and must survive endpoint deletion.
        assert "mooncake-master" not in deleted_deploys

        deleted_svcs = {c.args[0] for c in monitor.core_v1.delete_namespaced_service.call_args_list}
        assert {"ep", "ep-prefill", "ep-decode", "ep-proxy"}.issubset(deleted_svcs)

        deleted_ingresses = {
            c.args[0] for c in monitor.networking_v1.delete_namespaced_ingress.call_args_list
        }
        assert {"inference-ep", "inference-ep-proxy"}.issubset(deleted_ingresses)

        deleted_cms = {
            c.args[0] for c in monitor.core_v1.delete_namespaced_config_map.call_args_list
        }
        assert {"ep-mooncake", "ep-pd-proxy"}.issubset(deleted_cms)

        deleted_hpas = {
            c.args[0] for c in hpa.delete_namespaced_horizontal_pod_autoscaler.call_args_list
        }
        assert {"ep", "ep-prefill", "ep-decode"}.issubset(deleted_hpas)

        deleted_scaled_objects = {
            c.kwargs["name"] for c in custom.delete_namespaced_custom_object.call_args_list
        }
        assert {"ep", "ep-prefill", "ep-decode"}.issubset(deleted_scaled_objects)

        monitor.core_v1.delete_namespaced_secret.assert_called_once_with(
            "ep-admin", "ns", _request_timeout=30
        )

    def test_reconcile_deleted_detects_disaggregated_role_deployments(self, monitor, mock_store):
        """A disaggregated endpoint is detected and torn down, not skipped.

        Regression: the deletion gate only checked for a Deployment named after
        the endpoint. A disaggregated endpoint has no such Deployment (only
        ``name-prefill``/``-decode``/``-proxy``), so it was marked deleted
        without its resources ever being removed — orphaning the role
        Deployments and the GPU nodes they held.
        """
        from kubernetes.client.rest import ApiException

        def _read(dep_name, _namespace, **_kw):
            # No single Deployment named after the endpoint; only a role worker.
            if dep_name == "disagg-ep-prefill":
                return MagicMock()
            raise ApiException(status=404)

        monitor.apps_v1.read_namespaced_deployment.side_effect = _read
        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
            patch("gco.services.inference_monitor.client.CustomObjectsApi"),
        ):
            result = monitor._reconcile_deleted("disagg-ep", "gco-inference")

        assert result is not None
        assert result["action"] == "delete"
        deleted = {c.args[0] for c in monitor.apps_v1.delete_namespaced_deployment.call_args_list}
        assert "disagg-ep-prefill" in deleted
        mock_store.update_region_status.assert_any_call("disagg-ep", monitor.region, "deleted")

    # -- _reconcile_endpoint: create --

    @pytest.mark.asyncio
    async def test_reconcile_creates_when_no_deployment(self, monitor, mock_store):
        from kubernetes.client.rest import ApiException

        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        endpoint = {
            "endpoint_name": "new-ep",
            "desired_state": "deploying",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1", "replicas": 2},
            "namespace": "gco-inference",
        }
        with patch("gco.services.inference_monitor.client.CustomObjectsApi") as custom_api:
            result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "create"
        monitor.apps_v1.create_namespaced_deployment.assert_called_once()

        # Endpoint reconciliation creates only an internal ClusterIP Service.
        monitor.core_v1.create_namespaced_service.assert_called_once()
        service = monitor.core_v1.create_namespaced_service.call_args.args[1]
        assert service.spec.type == "ClusterIP"

        # The shared gco-system/gco-gateway HTTPRoute owns /inference; the
        # endpoint controller creates no Ingress, Gateway, or HTTPRoute.
        monitor.networking_v1.create_namespaced_ingress.assert_not_called()
        monitor.networking_v1.patch_namespaced_ingress.assert_not_called()
        custom_api.return_value.create_namespaced_custom_object.assert_not_called()
        mock_store.update_region_status.assert_called()

    @pytest.mark.asyncio
    async def test_reconcile_rejects_persisted_mooncake_canary_spec(self, monitor, mock_store):
        endpoint = {
            "endpoint_name": "invalid-ep",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "spec": {
                "image": "img:v1",
                "mooncake": {"mode": "store"},
                "canary": {"image": "img:v2", "weight": 10},
            },
            "namespace": "gco-inference",
        }

        result = await monitor._reconcile_endpoint(endpoint)

        assert result == {
            "action": "reject",
            "endpoint": "invalid-ep",
            "reason": "invalid_spec",
        }
        mock_store.update_region_status.assert_called_once_with(
            "invalid-ep",
            "us-east-1",
            "failed",
            error="endpoint spec cannot combine 'mooncake' and 'canary' blocks",
        )
        monitor.apps_v1.create_namespaced_deployment.assert_not_called()

    # -- _reconcile_endpoint: scale --

    @pytest.mark.asyncio
    async def test_reconcile_scales_when_replicas_differ(self, monitor, mock_store):
        deployment = MagicMock()
        deployment.spec.replicas = 1
        deployment.status.ready_replicas = 1
        deployment.spec.template.spec.containers = [MagicMock(image="img:v1")]
        monitor.apps_v1.read_namespaced_deployment.return_value = deployment

        endpoint = {
            "endpoint_name": "scale-ep",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1", "replicas": 4},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "scale"
        assert result["replicas"] == 4
        monitor.apps_v1.patch_namespaced_deployment.assert_called_once()

    # -- _reconcile_endpoint: update image --

    @pytest.mark.asyncio
    async def test_reconcile_updates_image_when_changed(self, monitor, mock_store):
        deployment = MagicMock()
        deployment.spec.replicas = 2
        deployment.status.ready_replicas = 2
        deployment.spec.template.spec.containers = [MagicMock(image="img:old")]
        monitor.apps_v1.read_namespaced_deployment.return_value = deployment

        endpoint = {
            "endpoint_name": "img-ep",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:new", "replicas": 2},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "update_image"
        assert result["image"] == "img:new"

    # -- _reconcile_endpoint: cleanup when region removed --

    @pytest.mark.asyncio
    async def test_reconcile_cleans_up_split_endpoint_when_region_removed(
        self, monitor, mock_store
    ):
        from kubernetes.client.rest import ApiException

        def _read(deployment_name, _namespace, **_kwargs):
            if deployment_name == "removed-ep-prefill":
                return MagicMock()
            raise ApiException(status=404)

        monitor.apps_v1.read_namespaced_deployment.side_effect = _read
        endpoint = {
            "endpoint_name": "removed-ep",
            "desired_state": "running",
            "target_regions": ["eu-west-1"],  # us-east-1 not in list
            "spec": {
                "image": "img:v1",
                "mooncake": {"mode": "disaggregated", "proxy": {}},
            },
            "namespace": "gco-inference",
        }
        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
            patch("gco.services.inference_monitor.client.CustomObjectsApi"),
        ):
            result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "cleanup"
        assert result["reason"] == "region_removed"

    # -- _reconcile_endpoint: stopped --

    @pytest.mark.asyncio
    async def test_reconcile_stops_endpoint(self, monitor, mock_store):
        deployment = MagicMock()
        deployment.spec.replicas = 2
        monitor.apps_v1.read_namespaced_deployment.return_value = deployment

        endpoint = {
            "endpoint_name": "stop-ep",
            "desired_state": "stopped",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "stop"

    # -- _reconcile_endpoint: deleted --

    @pytest.mark.asyncio
    async def test_reconcile_deletes_endpoint(self, monitor, mock_store):
        monitor.apps_v1.read_namespaced_deployment.return_value = MagicMock()
        endpoint = {
            "endpoint_name": "del-ep",
            "desired_state": "deleted",
            "target_regions": ["us-east-1"],
            "spec": {"image": "img:v1"},
            "namespace": "gco-inference",
        }
        with (
            patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
            patch("gco.services.inference_monitor.client.CustomObjectsApi"),
        ):
            result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "delete"

    # -- reconcile: processes multiple endpoints --

    @pytest.mark.asyncio
    async def test_reconcile_processes_multiple_endpoints(self, monitor, mock_store):
        from kubernetes.client.rest import ApiException

        mock_store.list_endpoints.return_value = [
            {
                "endpoint_name": "ep-1",
                "desired_state": "deploying",
                "target_regions": ["us-east-1"],
                "spec": {"image": "img:v1"},
                "namespace": "gco-inference",
            },
            {
                "endpoint_name": "ep-2",
                "desired_state": "deploying",
                "target_regions": ["us-east-1"],
                "spec": {"image": "img:v2"},
                "namespace": "gco-inference",
            },
        ]
        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)

        actions = await monitor.reconcile()
        assert len(actions) == 2
        assert all(a["action"] == "create" for a in actions)

    # -- reconcile: handles per-endpoint errors --

    @pytest.mark.asyncio
    async def test_reconcile_handles_per_endpoint_error(self, monitor, mock_store):
        mock_store.list_endpoints.return_value = [
            {
                "endpoint_name": "bad-ep",
                "desired_state": "deploying",
                "target_regions": ["us-east-1"],
                "spec": {},  # missing image will cause error
                "namespace": "gco-inference",
            },
        ]
        from kubernetes.client.rest import ApiException

        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        monitor.apps_v1.create_namespaced_deployment.side_effect = Exception("boom")

        actions = await monitor.reconcile()
        # Error is caught, not propagated
        assert actions == []
        mock_store.update_region_status.assert_called()

    # -- get_metrics --

    def test_get_metrics(self, monitor):
        metrics = monitor.get_metrics()
        assert metrics["cluster_id"] == "test-cluster"
        assert metrics["region"] == "us-east-1"
        assert metrics["running"] is False
        assert metrics["reconcile_count"] == 0
        assert metrics["errors_count"] == 0

    # -- create_inference_monitor_from_env --

    def test_create_inference_monitor_from_env(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "CLUSTER_NAME": "env-cluster",
                    "REGION": "ap-southeast-1",
                    "INFERENCE_NAMESPACE": "custom-ns",
                    "RECONCILE_INTERVAL_SECONDS": "30",
                },
            ),
            patch("gco.services.inference_monitor.config.load_incluster_config"),
            patch("gco.services.inference_monitor.client.AppsV1Api"),
            patch("gco.services.inference_monitor.client.CoreV1Api"),
            patch("gco.services.inference_monitor.client.NetworkingV1Api"),
            patch("gco.services.inference_monitor.InferenceEndpointStore"),
        ):
            from gco.services.inference_monitor import create_inference_monitor_from_env

            m = create_inference_monitor_from_env()
            assert m.cluster_id == "env-cluster"
            assert m.region == "ap-southeast-1"
            assert m.namespace == "custom-ns"
            assert m.reconcile_interval == 30

    # -- accelerator support --

    @pytest.mark.asyncio
    async def test_reconcile_neuron_accelerator_creates_neuron_resources(self, monitor, mock_store):
        """Test that accelerator=neuron uses aws.amazon.com/neuron resources."""
        from kubernetes.client.rest import ApiException

        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        endpoint = {
            "endpoint_name": "neuron-ep",
            "desired_state": "deploying",
            "target_regions": ["us-east-1"],
            "spec": {
                "image": "public.ecr.aws/neuron/my-model:latest",
                "replicas": 1,
                "gpu_count": 1,
                "accelerator": "neuron",
            },
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "create"

        # Verify the deployment was created with Neuron resources
        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args.args[1] if call_args.args else call_args.kwargs.get("body")
        pod_spec = deployment.spec.template.spec

        # Check tolerations include Neuron taint
        taint_keys = [t.key for t in pod_spec.tolerations]
        assert "aws.amazon.com/neuron" in taint_keys
        assert "nvidia.com/gpu" not in taint_keys

        # Check container resources use Neuron device
        container = pod_spec.containers[0]
        assert "aws.amazon.com/neuron" in container.resources.limits
        assert "nvidia.com/gpu" not in container.resources.limits

    @pytest.mark.asyncio
    async def test_reconcile_default_accelerator_uses_nvidia(self, monitor, mock_store):
        """Test that default (no accelerator field) uses nvidia.com/gpu resources."""
        from kubernetes.client.rest import ApiException

        monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
        endpoint = {
            "endpoint_name": "gpu-ep",
            "desired_state": "deploying",
            "target_regions": ["us-east-1"],
            "spec": {
                "image": "vllm/vllm-openai:v0.22.0",
                "replicas": 1,
                "gpu_count": 1,
            },
            "namespace": "gco-inference",
        }
        result = await monitor._reconcile_endpoint(endpoint)
        assert result is not None
        assert result["action"] == "create"

        call_args = monitor.apps_v1.create_namespaced_deployment.call_args
        deployment = call_args.args[1] if call_args.args else call_args.kwargs.get("body")
        pod_spec = deployment.spec.template.spec

        # Check tolerations include NVIDIA taint
        taint_keys = [t.key for t in pod_spec.tolerations]
        assert "nvidia.com/gpu" in taint_keys
        assert "aws.amazon.com/neuron" not in taint_keys

        # Check container resources use NVIDIA GPU
        container = pod_spec.containers[0]
        assert "nvidia.com/gpu" in container.resources.limits
        assert "aws.amazon.com/neuron" not in container.resources.limits


# =============================================================================
# InferenceManager Tests (CLI)
# =============================================================================


class TestInferenceManager:
    """Tests for InferenceManager CLI layer."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.global_region = "us-east-2"
        return config

    @pytest.fixture
    def mock_aws_client(self):
        return MagicMock()

    @pytest.fixture
    def mock_store_instance(self):
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_config, mock_aws_client, mock_store_instance):
        with (
            patch("cli.inference.get_aws_client", return_value=mock_aws_client),
            patch("cli.inference.get_config", return_value=mock_config),
        ):
            from cli.inference import InferenceManager

            mgr = InferenceManager(config=mock_config)
            mgr._aws_client = mock_aws_client
        # Patch _get_store to return our mock
        mgr._get_store = MagicMock(return_value=mock_store_instance)
        return mgr

    # -- deploy --

    def test_deploy_with_explicit_regions(self, manager, mock_store_instance):
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        result = manager.deploy(
            endpoint_name="ep1",
            image="img:v1",
            target_regions=["us-east-1", "us-west-2"],
        )
        assert result["endpoint_name"] == "ep1"
        mock_store_instance.create_endpoint.assert_called_once()
        call_kwargs = mock_store_instance.create_endpoint.call_args
        assert call_kwargs.kwargs["target_regions"] == ["us-east-1", "us-west-2"]

    def test_deploy_auto_discovers_regions(self, manager, mock_aws_client, mock_store_instance):
        mock_aws_client.discover_regional_stacks.return_value = {
            "us-east-1": {},
            "eu-west-1": {},
        }
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep2"}
        result = manager.deploy(endpoint_name="ep2", image="img:v1")
        assert result["endpoint_name"] == "ep2"
        call_kwargs = mock_store_instance.create_endpoint.call_args
        regions = call_kwargs.kwargs["target_regions"]
        assert set(regions) == {"us-east-1", "eu-west-1"}

    def test_deploy_no_regions_raises(self, manager, mock_aws_client):
        mock_aws_client.discover_regional_stacks.return_value = {}
        with pytest.raises(ValueError, match="No deployed regions"):
            manager.deploy(endpoint_name="ep3", image="img:v1")

    # -- list_endpoints --

    def test_list_endpoints(self, manager, mock_store_instance):
        mock_store_instance.list_endpoints.return_value = [{"endpoint_name": "a"}]
        result = manager.list_endpoints(desired_state="running", region="us-east-1")
        assert len(result) == 1
        mock_store_instance.list_endpoints.assert_called_once_with(
            desired_state="running", target_region="us-east-1"
        )

    # -- get_endpoint --

    def test_get_endpoint_found(self, manager, mock_store_instance):
        mock_store_instance.get_endpoint.return_value = {"endpoint_name": "ep"}
        assert manager.get_endpoint("ep") is not None

    def test_get_endpoint_not_found(self, manager, mock_store_instance):
        mock_store_instance.get_endpoint.return_value = None
        assert manager.get_endpoint("nope") is None

    # -- scale --

    def test_scale_success(self, manager, mock_store_instance):
        mock_store_instance.scale_endpoint.return_value = {"endpoint_name": "ep"}
        result = manager.scale("ep", 5)
        assert result is not None
        mock_store_instance.scale_endpoint.assert_called_once_with("ep", 5)

    def test_scale_not_found(self, manager, mock_store_instance):
        mock_store_instance.scale_endpoint.return_value = None
        assert manager.scale("ghost", 3) is None

    # -- stop --

    def test_stop_success(self, manager, mock_store_instance):
        mock_store_instance.update_desired_state.return_value = {"desired_state": "stopped"}
        result = manager.stop("ep")
        assert result is not None
        mock_store_instance.update_desired_state.assert_called_once_with("ep", "stopped")

    def test_stop_not_found(self, manager, mock_store_instance):
        mock_store_instance.update_desired_state.return_value = None
        assert manager.stop("ghost") is None

    # -- start --

    def test_start_success(self, manager, mock_store_instance):
        mock_store_instance.update_desired_state.return_value = {"desired_state": "running"}
        result = manager.start("ep")
        assert result is not None
        mock_store_instance.update_desired_state.assert_called_once_with("ep", "running")

    def test_start_not_found(self, manager, mock_store_instance):
        mock_store_instance.update_desired_state.return_value = None
        assert manager.start("ghost") is None

    # -- delete --

    def test_delete_success(self, manager, mock_store_instance):
        mock_store_instance.update_desired_state.return_value = {"desired_state": "deleted"}
        result = manager.delete("ep")
        assert result is not None
        mock_store_instance.update_desired_state.assert_called_once_with("ep", "deleted")

    def test_delete_not_found(self, manager, mock_store_instance):
        mock_store_instance.update_desired_state.return_value = None
        assert manager.delete("ghost") is None

    # -- update_image --

    def test_update_image_success(self, manager, mock_store_instance):
        original_spec = {
            "image": "old:v1",
            "replicas": 2,
            "region_image_uris": {"us-east-1": "regional-old:v1"},
            "env": {"MODEL_REVISION": "stable"},
        }
        mock_store_instance.get_endpoint.return_value = {"spec": original_spec}
        mock_store_instance.update_spec.return_value = {"endpoint_name": "ep"}

        result = manager.update_image("ep", " new:v2 ")

        assert result is not None
        submitted_spec = mock_store_instance.update_spec.call_args.args[1]
        assert submitted_spec["image"] == "new:v2"
        assert "region_image_uris" not in submitted_spec
        assert submitted_spec is not original_spec
        assert submitted_spec["env"] is not original_spec["env"]
        assert original_spec["image"] == "old:v1"
        assert "region_image_uris" in original_spec

    @pytest.mark.parametrize("image", [None, "", "   ", 123])
    def test_update_image_rejects_invalid_image(self, manager, mock_store_instance, image):
        with pytest.raises(ValueError, match="Image must be a non-empty string"):
            manager.update_image("ep", image)

        mock_store_instance.get_endpoint.assert_not_called()
        mock_store_instance.update_spec.assert_not_called()

    def test_update_image_not_found(self, manager, mock_store_instance):
        mock_store_instance.get_endpoint.return_value = None
        assert manager.update_image("ghost", "img:v1") is None

    def test_deploy_with_extra_args(self, manager, mock_store_instance):
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        result = manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.22.0",
            target_regions=["us-east-1"],
            extra_args=["--kv-transfer-config", '{"kv_connector":"P2pNcclConnector"}'],
        )
        assert result["endpoint_name"] == "ep1"
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert call_kwargs["spec"]["args"] == [
            "--kv-transfer-config",
            '{"kv_connector":"P2pNcclConnector"}',
        ]

    def test_deploy_without_extra_args_has_no_args_key(self, manager, mock_store_instance):
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.22.0",
            target_regions=["us-east-1"],
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert "args" not in call_kwargs["spec"]

    def test_deploy_with_neuron_accelerator(self, manager, mock_store_instance):
        """Test that --accelerator neuron sets the accelerator field in the spec."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        result = manager.deploy(
            endpoint_name="ep1",
            image="public.ecr.aws/neuron/my-model:latest",
            target_regions=["us-east-1"],
            accelerator="neuron",
        )
        assert result["endpoint_name"] == "ep1"
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert call_kwargs["spec"]["accelerator"] == "neuron"

    def test_deploy_with_nvidia_accelerator_omits_field(self, manager, mock_store_instance):
        """Test that --accelerator nvidia (default) does not set the accelerator field."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.22.0",
            target_regions=["us-east-1"],
            accelerator="nvidia",
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert "accelerator" not in call_kwargs["spec"]

    def test_deploy_default_accelerator_is_nvidia(self, manager, mock_store_instance):
        """Test that the default accelerator is nvidia (no accelerator field in spec)."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.22.0",
            target_regions=["us-east-1"],
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert "accelerator" not in call_kwargs["spec"]

    def test_deploy_with_node_selector(self, manager, mock_store_instance):
        """Test that --node-selector passes through to the spec."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        result = manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.22.0",
            target_regions=["us-east-1"],
            node_selector={"eks.amazonaws.com/instance-family": "inf2"},
        )
        assert result["endpoint_name"] == "ep1"
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert call_kwargs["spec"]["node_selector"] == {"eks.amazonaws.com/instance-family": "inf2"}

    def test_deploy_without_node_selector_omits_field(self, manager, mock_store_instance):
        """Test that no --node-selector omits the field from spec."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.22.0",
            target_regions=["us-east-1"],
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        assert "node_selector" not in call_kwargs["spec"]

    # -- per-region image URI rewrite --

    def test_inference_deploy_rewrites_ecr_uri_to_local_region(self, manager, mock_store_instance):
        """ECR URIs are rewritten per-target-region into a region_image_uris map."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        src = "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/foo:v1"
        manager.deploy(
            endpoint_name="ep1",
            image=src,
            target_regions=["us-west-2", "eu-west-1"],
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        spec = call_kwargs["spec"]
        # The flat ``image`` field is preserved as-is (the source URI).
        assert spec["image"] == src
        # And the per-region map gets each region's local replica URI.
        region_map = spec["region_image_uris"]
        assert region_map["us-west-2"] == "123456789012.dkr.ecr.us-west-2.amazonaws.com/gco/foo:v1"
        assert region_map["eu-west-1"] == "123456789012.dkr.ecr.eu-west-1.amazonaws.com/gco/foo:v1"

    def test_inference_deploy_no_rewrite_flag_keeps_original_uri(
        self, manager, mock_store_instance
    ):
        """``rewrite_image=False`` writes the URI verbatim to every region."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        src = "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/foo:v1"
        manager.deploy(
            endpoint_name="ep1",
            image=src,
            target_regions=["us-west-2"],
            rewrite_image=False,
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        spec = call_kwargs["spec"]
        assert spec["image"] == src
        # No per-region map when rewrite is disabled.
        assert "region_image_uris" not in spec

    def test_inference_deploy_no_rewrite_for_non_ecr_uri(self, manager, mock_store_instance):
        """Non-ECR refs (Docker Hub, GHCR, etc.) never produce a region map."""
        mock_store_instance.create_endpoint.return_value = {"endpoint_name": "ep1"}
        manager.deploy(
            endpoint_name="ep1",
            image="vllm/vllm-openai:v0.8.0",
            target_regions=["us-west-2", "eu-west-1"],
        )
        call_kwargs = mock_store_instance.create_endpoint.call_args.kwargs
        spec = call_kwargs["spec"]
        assert spec["image"] == "vllm/vllm-openai:v0.8.0"
        # The rewrite helper is identity for non-ECR refs, so the map
        # would be all-equal-to-image and the deploy() helper drops it.
        assert "region_image_uris" not in spec
