"""GPU-aware autoscaling routes through a KEDA ScaledObject.

GPU utilization is not a Kubernetes Resource metric, so a native
HorizontalPodAutoscaler cannot scale on it. When an autoscaler's metric set
includes a GPU signal, the monitor materializes a KEDA ScaledObject with an
``aws-cloudwatch`` trigger that reads the ContainerInsights GPU metric for the
target Deployment. CPU/memory-only autoscalers keep using the native HPA.

These tests pin:

- A GPU metric forces the KEDA ScaledObject path (no native HPA created), with
  a correctly-shaped aws-cloudwatch trigger (namespace, metric name, dimension
  triple, target, region).
- CPU/memory targets ride along as native KEDA triggers on the same object.
- A CPU/memory-only autoscaler stays on the native HPA path (no ScaledObject).
- Teardown removes any ScaledObject left behind by the GPU path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException


def _make_monitor(region: str = "us-west-2"):
    """Build an :class:`InferenceMonitor` with every K8s client mocked out."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_net,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        from gco.services.inference_monitor import InferenceMonitor

        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region=region,
            store=MagicMock(),
            namespace="gco-inference",
            reconcile_interval=5,
        )
        monitor.apps_v1 = mock_apps.return_value
        monitor.core_v1 = mock_core.return_value
        monitor.networking_v1 = mock_net.return_value
        return monitor


def _gpu_spec(target: int = 60) -> dict:
    return {
        "autoscaling": {
            "enabled": True,
            "min_replicas": 2,
            "max_replicas": 9,
            "metrics": [{"type": "gpu", "target": target}],
        }
    }


def test_gpu_metric_creates_scaled_object_not_hpa():
    """A GPU metric is served by a KEDA ScaledObject, never a native HPA."""
    monitor = _make_monitor()
    custom = MagicMock()
    autoscaling = MagicMock()
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=autoscaling),
    ):
        monitor._create_or_update_hpa("ep-gpu", "gco-inference", _gpu_spec())

    custom.create_namespaced_custom_object.assert_called_once()
    autoscaling.create_namespaced_horizontal_pod_autoscaler.assert_not_called()

    kwargs = custom.create_namespaced_custom_object.call_args.kwargs
    assert kwargs["group"] == "keda.sh"
    assert kwargs["plural"] == "scaledobjects"
    body = kwargs["body"]
    assert body["kind"] == "ScaledObject"
    assert body["spec"]["scaleTargetRef"]["name"] == "ep-gpu"
    assert body["spec"]["minReplicaCount"] == 2
    assert body["spec"]["maxReplicaCount"] == 9


def test_gpu_trigger_targets_cloudwatch_container_insights():
    """The GPU trigger reads the ContainerInsights metric for the Deployment."""
    monitor = _make_monitor(region="eu-west-1")
    custom = MagicMock()
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor._create_or_update_hpa("llm", "gco-inference", _gpu_spec(target=75))

    triggers = custom.create_namespaced_custom_object.call_args.kwargs["body"]["spec"]["triggers"]
    gpu = next(t for t in triggers if t["type"] == "aws-cloudwatch")
    md = gpu["metadata"]
    assert md["namespace"] == "ContainerInsights"
    assert md["metricName"] == "pod_gpu_utilization"
    assert md["dimensionName"] == "ClusterName;Namespace;PodName"
    assert md["dimensionValue"] == "test-cluster;gco-inference;llm"
    assert md["targetMetricValue"] == "75"
    assert md["awsRegion"] == "eu-west-1"


def test_cpu_rides_along_with_gpu_in_same_scaled_object():
    """CPU/memory targets become native KEDA triggers next to the GPU trigger."""
    monitor = _make_monitor()
    custom = MagicMock()
    spec = {
        "autoscaling": {
            "enabled": True,
            "min_replicas": 1,
            "max_replicas": 4,
            "metrics": [
                {"type": "cpu", "target": 70},
                {"type": "gpu", "target": 60},
            ],
        }
    }
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor._create_or_update_hpa("mixed", "gco-inference", spec)

    triggers = custom.create_namespaced_custom_object.call_args.kwargs["body"]["spec"]["triggers"]
    types = {t["type"] for t in triggers}
    assert types == {"cpu", "aws-cloudwatch"}
    cpu = next(t for t in triggers if t["type"] == "cpu")
    assert cpu["metricType"] == "Utilization"
    assert cpu["metadata"]["value"] == "70"


def test_cpu_only_stays_on_native_hpa():
    """A CPU/memory-only autoscaler does not create a ScaledObject."""
    monitor = _make_monitor()
    custom = MagicMock()
    autoscaling = MagicMock()
    spec = {
        "autoscaling": {
            "enabled": True,
            "min_replicas": 1,
            "max_replicas": 5,
            "metrics": [{"type": "cpu", "target": 80}],
        }
    }
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=autoscaling),
    ):
        monitor._create_or_update_hpa("cpu-ep", "gco-inference", spec)

    autoscaling.create_namespaced_horizontal_pod_autoscaler.assert_called_once()
    custom.create_namespaced_custom_object.assert_not_called()


def test_role_gpu_scaling_targets_role_deployment():
    """A Mooncake role's GPU scaler targets the {name}-{role} Deployment."""
    monitor = _make_monitor()
    custom = MagicMock()
    spec = {
        "mooncake": {
            "autoscaling": {
                "enabled": True,
                "decode": {
                    "min_replicas": 2,
                    "max_replicas": 16,
                    "metrics": [{"type": "gpu", "target": 50}],
                },
            }
        }
    }
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor._create_role_hpa("svc", "gco-inference", spec, "decode")

    body = custom.create_namespaced_custom_object.call_args.kwargs["body"]
    assert body["metadata"]["name"] == "svc-decode"
    assert body["spec"]["scaleTargetRef"]["name"] == "svc-decode"
    gpu = next(t for t in body["spec"]["triggers"] if t["type"] == "aws-cloudwatch")
    assert gpu["metadata"]["dimensionValue"] == "test-cluster;gco-inference;svc-decode"


def test_existing_scaled_object_is_patched():
    """A 409 on create falls back to a merge patch of the ScaledObject."""
    monitor = _make_monitor()
    custom = MagicMock()
    custom.create_namespaced_custom_object.side_effect = ApiException(status=409)
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor._create_or_update_hpa("ep-gpu", "gco-inference", _gpu_spec())

    custom.patch_namespaced_custom_object.assert_called_once()


def test_delete_resources_removes_scaled_object():
    """Teardown deletes any KEDA ScaledObject left by the GPU path."""
    monitor = _make_monitor()
    custom = MagicMock()
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor._delete_resources("ep-gpu", "gco-inference")

    custom.delete_namespaced_custom_object.assert_called_once()
    kwargs = custom.delete_namespaced_custom_object.call_args.kwargs
    assert kwargs["plural"] == "scaledobjects"
    assert kwargs["name"] == "ep-gpu"


def test_delete_resources_tolerates_absent_scaled_object():
    """A 404 deleting the ScaledObject (the common case) is swallowed."""
    monitor = _make_monitor()
    custom = MagicMock()
    custom.delete_namespaced_custom_object.side_effect = ApiException(status=404)
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        # Should not raise.
        monitor._delete_resources("ep-cpu", "gco-inference")
