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
- Teardown removes classic and Mooncake role ScaledObjects left behind by GPU
  autoscaling paths.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from gco.services.inference_monitor import ResourceCleanupResult


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


def _absent_autoscalers(custom: MagicMock, autoscaling: MagicMock | None = None) -> None:
    """The monitor reads before it creates: no ScaledObject or HPA exists yet."""
    custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    if autoscaling is not None:
        autoscaling.read_namespaced_horizontal_pod_autoscaler.side_effect = ApiException(status=404)


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
    _absent_autoscalers(custom, autoscaling)
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
    _absent_autoscalers(custom)
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
    _absent_autoscalers(custom)
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
    _absent_autoscalers(custom, autoscaling)
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
    _absent_autoscalers(custom)
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


@pytest.mark.parametrize(
    ("spec", "expected_hpa"),
    [
        (
            {
                "autoscaling": {
                    "enabled": True,
                    "min_replicas": 2,
                    "metrics": [{"type": "cpu", "target": 70}],
                }
            },
            "ep",
        ),
        (_gpu_spec(), "keda-hpa-ep"),
    ],
    ids=("native-hpa", "keda"),
)
def test_classic_stop_to_start_seeds_before_reapplying_and_verifying_owner(spec, expected_hpa):
    monitor = _make_monitor()
    deployment = MagicMock()
    deployment.spec.replicas = 0
    events: list[str] = []
    monitor._get_deployment = MagicMock(return_value=deployment)  # type: ignore[method-assign]
    monitor._scale_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_args: events.append("seed")
    )
    monitor._create_or_update_hpa = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_args: events.append("apply")
    )
    monitor._verify_hpa_owner = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_args: (
            events.append("verify") or ResourceCleanupResult(resources_found=True)
        )
    )
    with (
        patch.object(
            monitor,
            "_delete_scaled_objects",
            side_effect=lambda *_args: (
                events.append("remove-scaledobject") or ResourceCleanupResult()
            ),
        ),
        patch.object(
            monitor,
            "_delete_hpas",
            side_effect=lambda *_args: events.append("remove-hpa") or ResourceCleanupResult(),
        ),
    ):
        result = monitor._reconcile_classic_autoscaler("ep", "gco-inference", spec)

    assert result.complete is True
    assert events[-3:] == ["seed", "apply", "verify"]
    minimum = spec["autoscaling"]["min_replicas"]
    monitor._scale_deployment.assert_called_once_with("ep", "gco-inference", minimum)
    monitor._verify_hpa_owner.assert_called_once_with(expected_hpa, "gco-inference", "ep")


def test_classic_delayed_keda_hpa_remains_blocking_after_seed_pass():
    monitor = _make_monitor()
    deployment = MagicMock()
    deployment.spec.replicas = 0
    monitor._get_deployment = MagicMock(return_value=deployment)  # type: ignore[method-assign]
    monitor._scale_deployment = MagicMock()  # type: ignore[method-assign]
    monitor._create_or_update_hpa = MagicMock()  # type: ignore[method-assign]
    monitor._delete_hpas = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._verify_hpa_owner = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            ResourceCleanupResult(pending=("hpa/keda-hpa-ep",)),
            ResourceCleanupResult(resources_found=True),
        ]
    )

    first = monitor._reconcile_classic_autoscaler("ep", "gco-inference", _gpu_spec())
    deployment.spec.replicas = 2
    second = monitor._reconcile_classic_autoscaler("ep", "gco-inference", _gpu_spec())

    assert first.complete is False
    assert second.complete is True
    monitor._scale_deployment.assert_called_once_with("ep", "gco-inference", 2)
    assert monitor._create_or_update_hpa.call_count == 2
    assert monitor._verify_hpa_owner.call_count == 2


@pytest.mark.parametrize(
    ("api_version", "kind", "name", "expected_complete"),
    [
        ("apps/v1", "Deployment", "ep", True),
        ("apps/v1", "StatefulSet", "ep", False),
        ("custom.example/v1", "Deployment", "ep", False),
        ("apps/v1", "Deployment", "other", False),
    ],
)
def test_hpa_owner_requires_exact_deployment_target(api_version, kind, name, expected_complete):
    monitor = _make_monitor()
    autoscaling = MagicMock()
    autoscaling.read_namespaced_horizontal_pod_autoscaler.return_value = SimpleNamespace(
        spec=SimpleNamespace(
            scale_target_ref=SimpleNamespace(
                api_version=api_version,
                kind=kind,
                name=name,
            )
        )
    )

    with patch(
        "gco.services.inference_monitor.client.AutoscalingV2Api",
        return_value=autoscaling,
    ):
        result = monitor._verify_hpa_owner("ep", "gco-inference", "ep")

    assert result.complete is expected_complete
    assert result.resources_found is True
    if not expected_complete:
        assert result.pending == ("hpa/ep",)


def test_mooncake_autoscaled_and_static_roles_restore_zero_targets():
    monitor = _make_monitor()
    deployment = MagicMock()
    deployment.spec.replicas = 0
    deployment.status.ready_replicas = 0
    monitor._get_deployment = MagicMock(return_value=deployment)  # type: ignore[method-assign]
    monitor._scale_deployment = MagicMock()  # type: ignore[method-assign]
    autoscaled = {
        "mooncake": {
            "topology": {"prefill": 7},
            "autoscaling": {"enabled": True, "prefill": {"min_replicas": 3}},
        }
    }

    assert monitor._ensure_role_deployment("ep", "gco-inference", autoscaled, "prefill") == (
        0,
        3,
        True,
    )
    monitor._scale_deployment.assert_called_once_with("ep-prefill", "gco-inference", 3)

    monitor._scale_deployment.reset_mock()
    static = {"mooncake": {"topology": {"decode": 4}}}
    assert monitor._ensure_role_deployment("ep", "gco-inference", static, "decode") == (
        0,
        4,
        False,
    )
    monitor._scale_deployment.assert_called_once_with("ep-decode", "gco-inference", 4)


def test_native_autoscaler_handoff_removes_keda_owner_before_apply():
    """GPU -> CPU waits for the ScaledObject and generated HPA to disappear."""
    monitor = _make_monitor()
    scaled_cleanup = ResourceCleanupResult()
    generated_hpa_cleanup = ResourceCleanupResult()
    spec = {
        "autoscaling": {
            "enabled": True,
            "metrics": [{"type": "cpu", "target": 70}],
        }
    }
    with (
        patch.object(
            monitor,
            "_delete_scaled_objects",
            return_value=scaled_cleanup,
        ) as delete_scaled_objects,
        patch.object(
            monitor,
            "_delete_hpas",
            return_value=generated_hpa_cleanup,
        ) as delete_hpas,
        patch.object(monitor, "_create_or_update_hpa") as apply_autoscaler,
        patch.object(
            monitor,
            "_verify_hpa_owner",
            return_value=ResourceCleanupResult(resources_found=True),
        ) as verify_owner,
    ):
        result = monitor._reconcile_classic_autoscaler("ep", "gco-inference", spec)

    assert result.complete is True
    delete_scaled_objects.assert_called_once_with(("ep",), "gco-inference")
    delete_hpas.assert_called_once_with(("keda-hpa-ep",), "gco-inference")
    apply_autoscaler.assert_called_once_with("ep", "gco-inference", spec)
    verify_owner.assert_called_once_with("ep", "gco-inference", "ep")


def test_keda_autoscaler_handoff_removes_native_hpa_before_apply():
    """CPU -> GPU waits for the native HPA before creating a ScaledObject."""
    monitor = _make_monitor()
    spec = _gpu_spec()
    with (
        patch.object(
            monitor,
            "_delete_hpas",
            return_value=ResourceCleanupResult(),
        ) as delete_hpas,
        patch.object(monitor, "_create_or_update_hpa") as apply_autoscaler,
        patch.object(
            monitor,
            "_verify_hpa_owner",
            return_value=ResourceCleanupResult(resources_found=True),
        ) as verify_owner,
    ):
        result = monitor._reconcile_classic_autoscaler("ep", "gco-inference", spec)

    assert result.complete is True
    delete_hpas.assert_called_once_with(("ep",), "gco-inference")
    apply_autoscaler.assert_called_once_with("ep", "gco-inference", spec)
    verify_owner.assert_called_once_with("keda-hpa-ep", "gco-inference", "ep")


def test_autoscaler_handoff_does_not_apply_while_old_owner_is_present():
    monitor = _make_monitor()
    pending = ResourceCleanupResult(pending=("scaledobject/ep",), resources_found=True)
    spec = {
        "autoscaling": {
            "enabled": True,
            "metrics": [{"type": "cpu", "target": 70}],
        }
    }
    with (
        patch.object(monitor, "_delete_scaled_objects", return_value=pending),
        patch.object(monitor, "_delete_hpas", return_value=ResourceCleanupResult()),
        patch.object(monitor, "_create_or_update_hpa") as apply_autoscaler,
    ):
        result = monitor._reconcile_classic_autoscaler("ep", "gco-inference", spec)

    assert result == pending
    apply_autoscaler.assert_not_called()


def test_delete_resources_removes_scaled_object():
    """Teardown deletes classic and role-scoped KEDA ScaledObjects."""
    monitor = _make_monitor()
    custom = MagicMock()
    hpa = MagicMock()
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=hpa),
    ):
        monitor._delete_resources("ep-gpu", "gco-inference")

    calls = [
        entry
        for entry in custom.delete_namespaced_custom_object.call_args_list
        if entry.kwargs["group"] == "keda.sh"
    ]
    assert [entry.kwargs["name"] for entry in calls] == [
        "ep-gpu",
        "ep-gpu-prefill",
        "ep-gpu-decode",
    ]
    assert all(entry.kwargs["plural"] == "scaledobjects" for entry in calls)
    deleted_hpas = {
        entry.args[0] for entry in hpa.delete_namespaced_horizontal_pod_autoscaler.call_args_list
    }
    assert {"ep-gpu", "keda-hpa-ep-gpu"} <= deleted_hpas


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


def test_role_native_handoff_removes_keda_owner_before_apply():
    monitor = _make_monitor()
    spec = {
        "mooncake": {
            "autoscaling": {
                "enabled": True,
                "prefill": {"metrics": [{"type": "cpu", "target": 70}]},
            }
        }
    }
    with (
        patch.object(
            monitor,
            "_delete_scaled_objects",
            return_value=ResourceCleanupResult(),
        ) as delete_scaled_objects,
        patch.object(
            monitor,
            "_delete_hpas",
            return_value=ResourceCleanupResult(),
        ) as delete_hpas,
        patch.object(monitor, "_create_role_hpa") as apply_autoscaler,
    ):
        result = monitor._reconcile_role_autoscaler("ep", "gco-inference", spec, "prefill")

    assert result.complete is True
    delete_scaled_objects.assert_called_once_with(("ep-prefill",), "gco-inference")
    delete_hpas.assert_called_once_with(("keda-hpa-ep-prefill",), "gco-inference")
    apply_autoscaler.assert_called_once_with("ep", "gco-inference", spec, "prefill")


def test_role_keda_handoff_and_recreate_barrier_remove_native_owner():
    monitor = _make_monitor()
    spec = {
        "mooncake": {
            "autoscaling": {
                "enabled": True,
                "decode": {"metrics": [{"type": "gpu", "target": 60}]},
            }
        }
    }
    pending = ResourceCleanupResult(pending=("hpa/ep-decode",), resources_found=True)
    with (
        patch.object(monitor, "_delete_hpas", return_value=pending) as delete_hpas,
        patch.object(monitor, "_create_role_hpa") as apply_autoscaler,
    ):
        result = monitor._reconcile_role_autoscaler(
            "ep",
            "gco-inference",
            spec,
            "decode",
            apply_desired=False,
        )

    assert result == pending
    delete_hpas.assert_called_once_with(("ep-decode",), "gco-inference")
    apply_autoscaler.assert_not_called()


def test_role_disable_or_removed_block_deletes_every_possible_owner():
    monitor = _make_monitor()
    for spec in (
        {"mooncake": {"autoscaling": {"enabled": False}}},
        {"mooncake": {}},
    ):
        with patch.object(
            monitor,
            "_delete_autoscalers",
            return_value=ResourceCleanupResult(),
        ) as delete_autoscalers:
            result = monitor._reconcile_role_autoscaler("ep", "gco-inference", spec, "decode")

        assert result.complete is True
        delete_autoscalers.assert_called_once_with(
            ("ep-decode",),
            ("ep-decode", "keda-hpa-ep-decode"),
            "gco-inference",
        )
