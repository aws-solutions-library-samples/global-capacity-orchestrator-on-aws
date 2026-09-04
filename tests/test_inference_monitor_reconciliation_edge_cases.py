"""Behavioral coverage for the inference monitor's defensive branches.

All Kubernetes, DynamoDB, AWS, and timing boundaries are local doubles.  The
cases focus on observable fencing, cleanup, topology, and error semantics left
uncovered by the saved baseline report.
"""

from __future__ import annotations

import contextlib
import runpy
import threading
import warnings
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from gco.services.inference_monitor import (
    ADMIN_API_KEY_SECRET_DATA_KEY,
    EFA_RESOURCE_NAME,
    MAX_BOOTSTRAP_PORT,
    MOONCAKE_MASTER_READY_TIMEOUT_SECONDS,
    AdminApiKeySecretError,
    InferenceMonitor,
    MasterReadinessGate,
    ReconcileAuthority,
    ReconcileFencedError,
    RegionalScopeResolution,
    RegionServicesResolution,
    ResourceCleanupResult,
    _resolved_mooncake_transfer,
    apply_efa_scheduling,
    bootstrap_port_for_worker,
)

REGION = "us-east-1"
NAMESPACE = "gco-inference"
LIFECYCLE = "life-1"
REGION_GENERATION = "region-1"
DELETE_GENERATION = "delete-1"


def _make_monitor(store: MagicMock | None = None) -> InferenceMonitor:
    endpoint_store = store or MagicMock()
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as networking,
        patch("gco.services.inference_monitor.client.DiscoveryV1Api") as discovery,
    ):
        monitor = InferenceMonitor(
            cluster_id="cluster-a",
            region=REGION,
            store=endpoint_store,
            namespace=NAMESPACE,
            reconcile_interval=1,
        )
    monitor.apps_v1 = apps.return_value
    monitor.core_v1 = core.return_value
    monitor.networking_v1 = networking.return_value
    monitor.discovery_v1 = discovery.return_value
    return monitor


def _authority(*, deleting: bool = False) -> ReconcileAuthority:
    return ReconcileAuthority(
        endpoint_name="ep",
        lifecycle_id=LIFECYCLE,
        region_generation=REGION_GENERATION,
        leader_epoch="epoch-1",
        deletion_generation=DELETE_GENERATION if deleting else None,
        deleting=deleting,
    )


def _resource(
    *,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    uid: str | None = "uid-1",
    resource_version: str | None = "7",
    name: str = "ep",
    owners: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            annotations=annotations or {},
            labels=labels or {},
            uid=uid,
            resource_version=resource_version,
            owner_references=owners,
        )
    )


def _owned_resource(**overrides: Any) -> SimpleNamespace:
    values = {
        "annotations": _authority().annotations,
        "labels": {"project": "gco", "gco.io/type": "inference", "app": "ep"},
    }
    values.update(overrides)
    return _resource(**values)


def _endpoint(**updates: Any) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "endpoint_name": "ep",
        "lifecycle_id": LIFECYCLE,
        "desired_state": "running",
        "target_regions": [REGION],
        "cleanup_regions": [REGION],
        "region_generations": {REGION: REGION_GENERATION},
        "updated_at": "2026-01-01T00:00:00+00:00",
        "namespace": NAMESPACE,
        "spec": {"image": "vllm/vllm-openai:v1", "replicas": 1, "gpu_count": 0},
        "region_status": {},
    }
    endpoint.update(updates)
    return endpoint


# ---------------------------------------------------------------------------
# Pure Mooncake validation and EFA scheduling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mooncake", "message"),
    [
        ({"transfer": []}, "must be a mapping"),
        ({"transfer": {"protocol": "ib"}}, "protocol must be one of"),
        ({"transfer": {"protocol": "rdma", "device_name": 3}}, "must be a string"),
    ],
)
def test_resolved_mooncake_transfer_rejects_invalid_shapes(
    mooncake: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _resolved_mooncake_transfer(mooncake)


@pytest.mark.parametrize("base_port", [0, MAX_BOOTSTRAP_PORT + 1])
def test_bootstrap_port_rejects_values_outside_user_port_range(base_port: int) -> None:
    with pytest.raises(ValueError, match="outside the valid range"):
        bootstrap_port_for_worker(base_port, 0, 1, 0)


def test_efa_scheduling_targets_accelerator_container_and_is_idempotent() -> None:
    existing_toleration = client.V1Toleration(
        key=EFA_RESOURCE_NAME, operator="Equal", value="true", effect="NoSchedule"
    )
    cpu_only = client.V1Container(name="sidecar", resources=None)
    gpu = client.V1Container(
        name="model",
        resources=client.V1ResourceRequirements(
            requests=None,
            limits={"nvidia.com/gpu": "1"},
        ),
    )
    pod = client.V1PodSpec(
        containers=[cpu_only, gpu],
        tolerations=[existing_toleration],
        node_selector={"existing": "selector"},
    )

    apply_efa_scheduling({"transfer": {"protocol": "rdma"}}, pod)
    apply_efa_scheduling({"transfer": {"protocol": "rdma"}}, pod)

    assert [item.key for item in pod.tolerations].count(EFA_RESOURCE_NAME) == 1
    assert cpu_only.resources is None
    assert gpu.resources.requests[EFA_RESOURCE_NAME] == "1"
    assert gpu.resources.limits[EFA_RESOURCE_NAME] == "1"
    assert pod.node_selector["existing"] == "selector"


def test_efa_scheduling_falls_back_to_all_containers_and_initializes_resources() -> None:
    missing = client.V1Container(name="missing", resources=None)
    empty = client.V1Container(
        name="empty", resources=client.V1ResourceRequirements(requests=None, limits=None)
    )
    pod = client.V1PodSpec(containers=[missing, empty], tolerations=None, node_selector=None)

    apply_efa_scheduling({}, pod)

    for container in (missing, empty):
        assert container.resources.requests[EFA_RESOURCE_NAME] == "1"
        assert container.resources.limits[EFA_RESOURCE_NAME] == "1"


# ---------------------------------------------------------------------------
# Construction, loop, and Lease edge behavior
# ---------------------------------------------------------------------------


def test_monitor_constructor_propagates_double_kubernetes_config_failure() -> None:
    with (
        patch(
            "gco.services.inference_monitor.config.load_incluster_config",
            side_effect=config.ConfigException("not in cluster"),
        ),
        patch(
            "gco.services.inference_monitor.config.load_kube_config",
            side_effect=config.ConfigException("no kubeconfig"),
        ),
        pytest.raises(config.ConfigException, match="no kubeconfig"),
    ):
        InferenceMonitor("cluster", REGION, MagicMock())


@pytest.mark.asyncio
async def test_monitor_start_breaks_when_reconcile_sleep_is_interrupted() -> None:
    monitor = _make_monitor()

    with (
        patch.object(monitor, "_try_acquire_lease", return_value=False),
        patch(
            "gco.services.inference_monitor.asyncio.sleep",
            AsyncMock(side_effect=RuntimeError("event loop closing")),
        ),
    ):
        await monitor.start()

    assert monitor._running is True


def test_same_holder_with_matching_persisted_epoch_renews_lease() -> None:
    monitor = _make_monitor()
    monitor._leader_epoch = "epoch-1"
    lease = SimpleNamespace(
        metadata=SimpleNamespace(annotations={"gco.io/leader-epoch": "epoch-1"}),
        spec=SimpleNamespace(
            holder_identity="pod-a",
            renew_time=datetime.now(UTC),
            lease_duration_seconds=30,
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.return_value = lease

    with patch("gco.services.inference_monitor.client.CoordinationV1Api", return_value=api):
        assert monitor._try_acquire_lease("lease", "pod-a") is True

    api.replace_namespaced_lease.assert_called_once()


def test_lease_renewal_loop_can_complete_successful_iteration_before_stop() -> None:
    monitor = _make_monitor()
    stop = MagicMock(spec=threading.Event)
    stop.wait.side_effect = [False, True]

    with patch.object(monitor, "_renew_current_lease", return_value=True) as renew:
        monitor._lease_renewal_loop(stop)

    renew.assert_called_once_with()
    assert stop.wait.call_count == 2


# ---------------------------------------------------------------------------
# Lifecycle predicates and resource fencing
# ---------------------------------------------------------------------------


def test_status_write_conditions_include_deletion_or_region_generation() -> None:
    monitor = _make_monitor()
    deleting = _endpoint(deletion_generation=DELETE_GENERATION)

    assert monitor._status_write_conditions(deleting, deleting=True) == {
        "expected_lifecycle_id": LIFECYCLE,
        "expected_deletion_generation": DELETE_GENERATION,
    }
    assert monitor._status_write_conditions(_endpoint()) == {
        "expected_lifecycle_id": LIFECYCLE,
        "expected_region_generation": REGION_GENERATION,
    }
    assert monitor._status_write_conditions({"lifecycle_id": ""}) == {}


@pytest.mark.parametrize("failure", [AttributeError("legacy"), TypeError("old signature")])
def test_strong_authority_compatibility_without_active_lease(failure: Exception) -> None:
    monitor = _make_monitor()
    monitor.store.get_endpoint.side_effect = failure
    assert monitor._strong_authority_matches(_authority()) is True


def test_strong_authority_non_mapping_snapshot_fails_closed_with_lease() -> None:
    monitor = _make_monitor()
    monitor._lease_name = "lease"
    monitor.store.get_endpoint.return_value = ["invalid"]
    assert monitor._strong_authority_matches(_authority()) is False


def test_handoff_stale_resource_reraises_unexpected_delete_error() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    resource = _owned_resource()
    delete = MagicMock(side_effect=ApiException(status=500, reason="down"))

    with (
        patch.object(monitor, "_strong_authority_matches", return_value=True),
        pytest.raises(ApiException) as caught,
    ):
        monitor._handoff_stale_resource(
            resource,
            kind="deployment",
            resource_name="ep",
            delete_resource=delete,
            reason="is stale",
        )

    assert caught.value.status == 500


def test_handoff_stale_resource_treats_absence_and_conflict_as_completed_request() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    resource = _owned_resource()

    for status in (404, 409):
        delete = MagicMock(side_effect=ApiException(status=status))
        with (
            patch.object(monitor, "_strong_authority_matches", return_value=True),
            pytest.raises(ReconcileFencedError, match="handoff deletion requested"),
        ):
            monitor._handoff_stale_resource(
                resource,
                kind="deployment",
                resource_name="ep",
                delete_resource=delete,
                reason="is stale",
            )


def test_assert_mutation_authority_detects_changed_dynamodb_snapshot() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    with (
        patch.object(monitor, "_strong_authority_matches", return_value=False),
        pytest.raises(ReconcileFencedError, match="authority changed"),
    ):
        monitor._assert_mutation_authority()


def test_authorize_resource_rejects_ambiguous_legacy_object() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    ambiguous = _resource(annotations={}, labels={})

    with pytest.raises(ReconcileFencedError, match="ambiguous legacy ownership"):
        monitor._authorize_resource(ambiguous, kind="service", resource_name="ep")


def test_authorize_resource_requires_claim_callbacks_and_resource_version() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    prior = dict(_authority().annotations)
    prior.pop("gco.io/leader-epoch")
    resource = _resource(
        annotations=prior,
        labels={"project": "gco", "gco.io/type": "inference"},
    )

    with pytest.raises(ReconcileFencedError, match="lacks current immutable provenance"):
        monitor._authorize_resource(resource, kind="service", resource_name="ep")

    monitor._lease_name = "lease"
    without_version = _resource(
        annotations=prior,
        labels={"project": "gco", "gco.io/type": "inference"},
        resource_version=None,
    )
    with pytest.raises(ReconcileFencedError, match="no resourceVersion"):
        monitor._authorize_resource(
            without_version,
            kind="service",
            resource_name="ep",
            patch_metadata=MagicMock(),
            read_resource=MagicMock(),
        )


def test_authorize_resource_fences_failed_claim_and_unverified_readback() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    prior = dict(_authority().annotations)
    prior.pop("gco.io/leader-epoch")
    resource = _resource(
        annotations=prior,
        labels={"project": "gco", "gco.io/type": "inference"},
    )

    with (
        patch.object(monitor, "_strong_authority_matches", return_value=True),
        pytest.raises(ReconcileFencedError, match="changed during authority claim"),
    ):
        monitor._authorize_resource(
            resource,
            kind="service",
            resource_name="ep",
            patch_metadata=MagicMock(side_effect=ApiException(status=409)),
            read_resource=MagicMock(),
        )

    with (
        patch.object(monitor, "_strong_authority_matches", return_value=True),
        pytest.raises(ReconcileFencedError, match="could not be verified"),
    ):
        monitor._authorize_resource(
            resource,
            kind="service",
            resource_name="ep",
            patch_metadata=MagicMock(),
            read_resource=MagicMock(return_value=resource),
        )


def test_confirm_created_resource_handles_direct_fixture_absence_and_bad_provenance() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()

    assert (
        monitor._confirm_created_resource(
            kind="service",
            resource_name="ep",
            read_resource=MagicMock(side_effect=ApiException(status=404)),
            delete_resource=MagicMock(),
        )
        is None
    )

    monitor._lease_name = "lease"
    with pytest.raises(ReconcileFencedError, match="post-create provenance changed"):
        monitor._confirm_created_resource(
            kind="service",
            resource_name="ep",
            read_resource=MagicMock(return_value=_resource(annotations={})),
            delete_resource=MagicMock(),
        )


def test_confirm_created_resource_compensates_and_preserves_authority_error() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    created = _owned_resource()
    delete = MagicMock(side_effect=ApiException(status=500))

    with (
        patch.object(
            monitor,
            "_assert_mutation_authority",
            side_effect=ReconcileFencedError("lost authority"),
        ),
        pytest.raises(ReconcileFencedError, match="lost authority"),
    ):
        monitor._confirm_created_resource(
            kind="service",
            resource_name="ep",
            read_resource=MagicMock(return_value=created),
            delete_resource=delete,
        )

    delete.assert_called_once()


def test_delete_options_require_uid_and_resource_version_under_active_lease() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    monitor._lease_name = "lease"

    with pytest.raises(ReconcileFencedError, match="lacks UID/resourceVersion"):
        monitor._delete_options_for(
            _resource(uid=None, resource_version=None), kind="service", resource_name="ep"
        )

    monitor._active_authority = None
    options = monitor._delete_options_for(
        _resource(uid="uid", resource_version=None), kind="service", resource_name="ep"
    )
    assert options.preconditions.uid == "uid"


# ---------------------------------------------------------------------------
# Reconcile, cleanup observations, and endpoint state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_logs_failed_conditional_purge_without_action() -> None:
    endpoint = _endpoint(
        desired_state="deleted",
        deletion_generation=DELETE_GENERATION,
        deletion_regions=[REGION],
        region_status={
            REGION: {
                "state": "deleted",
                "lifecycle_id": LIFECYCLE,
                "deletion_generation": DELETE_GENERATION,
                "absence_observations": 2,
            }
        },
    )
    store = MagicMock()
    store.list_endpoints.return_value = [endpoint]
    store.get_endpoint.return_value = endpoint
    store.delete_endpoint.side_effect = RuntimeError("DynamoDB unavailable")
    monitor = _make_monitor(store)

    with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
        assert await monitor.reconcile() == []


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (_endpoint(lifecycle_id=""), "lifecycle id"),
        (_endpoint(desired_state="deleted", deletion_generation=""), "deletion generation"),
        (_endpoint(region_generations={}), "Region generation"),
    ],
)
def test_cleanup_observation_requires_complete_generation_identity(
    endpoint: dict[str, Any], message: str
) -> None:
    monitor = _make_monitor()
    with pytest.raises(RuntimeError, match=message):
        monitor._record_cleanup_observation(endpoint, ResourceCleanupResult())


@pytest.mark.asyncio
async def test_deleted_endpoint_initializes_legacy_deletion_snapshot() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    endpoint = _endpoint(desired_state="deleted")

    result = await monitor._reconcile_endpoint(endpoint)

    assert result == {"action": "initialize_deletion", "endpoint": "ep"}
    store.update_desired_state.assert_called_once_with(
        "ep", "deleted", expected_lifecycle_id=LIFECYCLE
    )


@pytest.mark.asyncio
async def test_deleted_or_removed_region_terminal_paths_are_quiescent() -> None:
    monitor = _make_monitor()
    deleted_elsewhere = _endpoint(
        desired_state="deleted",
        deletion_generation=DELETE_GENERATION,
        deletion_regions=["eu-west-1"],
    )
    assert await monitor._reconcile_endpoint(deleted_elsewhere) is None

    terminal_removed = _endpoint(
        target_regions=["eu-west-1"],
        cleanup_regions=[REGION, "eu-west-1"],
        region_status={
            REGION: {
                "state": "deleted",
                "lifecycle_id": LIFECYCLE,
                "region_generation": REGION_GENERATION,
                "absence_observations": 2,
            }
        },
    )
    assert await monitor._reconcile_endpoint(terminal_removed) is None


@pytest.mark.asyncio
async def test_nonmember_without_cleanup_authority_is_ignored() -> None:
    monitor = _make_monitor()
    endpoint = _endpoint(target_regions=["eu-west-1"], cleanup_regions=["eu-west-1"])
    assert await monitor._reconcile_endpoint(endpoint) is None


def test_reconcile_deleted_requires_lifecycle_identity() -> None:
    monitor = _make_monitor()
    with pytest.raises(RuntimeError, match="lifecycle id"):
        monitor._reconcile_deleted(_endpoint(lifecycle_id=""), NAMESPACE)


# ---------------------------------------------------------------------------
# Classic and Mooncake running reconciliation
# ---------------------------------------------------------------------------


def _deployment(
    *, replicas: int = 1, ready: int = 1, image: str = "image:v1", rv: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(annotations={}, labels={}, uid="uid", resource_version=rv),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[SimpleNamespace(name="inference", image=image)])
            ),
        ),
        status=SimpleNamespace(ready_replicas=ready),
    )


def _prepare_existing_classic(monitor: InferenceMonitor, deployment: Any) -> None:
    monitor._get_deployment = MagicMock(return_value=deployment)  # type: ignore[method-assign]
    monitor._ensure_service = MagicMock()  # type: ignore[method-assign]
    monitor._check_health_watchdog = MagicMock(return_value=False)  # type: ignore[method-assign]
    monitor._cleanup_canary = MagicMock()  # type: ignore[method-assign]
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )


@pytest.mark.asyncio
async def test_classic_running_reports_autoscaler_cleanup_error() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _prepare_existing_classic(monitor, _deployment(replicas=2, ready=1))
    monitor._delete_autoscalers.return_value = ResourceCleanupResult(errors=("HPA read failed",))

    result = await monitor._reconcile_running(
        "ep", NAMESPACE, {"image": "image:v1", "replicas": 2}, _endpoint()
    )

    assert result == {
        "action": "reconcile_autoscaler",
        "endpoint": "ep",
        "cleanup_complete": False,
    }
    assert "HPA read failed" in store.update_region_status.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_classic_running_scales_static_deployment() -> None:
    monitor = _make_monitor()
    _prepare_existing_classic(monitor, _deployment(replicas=1, ready=1))
    monitor._scale_deployment = MagicMock()  # type: ignore[method-assign]

    result = await monitor._reconcile_running(
        "ep", NAMESPACE, {"image": "image:v1", "replicas": 3}, _endpoint()
    )

    assert result == {"action": "scale", "endpoint": "ep", "replicas": 3}
    monitor._scale_deployment.assert_called_once_with("ep", NAMESPACE, 3)


@pytest.mark.asyncio
async def test_classic_running_updates_drifted_image() -> None:
    monitor = _make_monitor()
    _prepare_existing_classic(monitor, _deployment(image="old:v1"))
    monitor._update_deployment_image = MagicMock()  # type: ignore[method-assign]

    result = await monitor._reconcile_running(
        "ep", NAMESPACE, {"image": "new:v2", "replicas": 1}, _endpoint()
    )

    assert result == {"action": "update_image", "endpoint": "ep", "image": "new:v2"}
    monitor._update_deployment_image.assert_called_once_with("ep", NAMESPACE, "new:v2")


@pytest.mark.asyncio
async def test_classic_running_cleans_absent_canary_and_promotes_multiregion_readiness() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _prepare_existing_classic(monitor, _deployment())
    endpoint = _endpoint(
        desired_state="deploying",
        target_regions=[REGION, "eu-west-1"],
        region_status={"eu-west-1": {"state": "running"}},
    )

    assert (
        await monitor._reconcile_running(
            "ep", NAMESPACE, {"image": "image:v1", "replicas": 1}, endpoint
        )
        is None
    )

    monitor._cleanup_canary.assert_called_once_with("ep", NAMESPACE)
    store.update_desired_state.assert_called_once_with(
        "ep",
        "running",
        expected_lifecycle_id=LIFECYCLE,
        expected_desired_state="deploying",
    )


@pytest.mark.asyncio
async def test_classic_running_does_not_promote_malformed_remote_status() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _prepare_existing_classic(monitor, _deployment())
    endpoint = _endpoint(
        desired_state="deploying",
        target_regions=[REGION, "eu-west-1"],
        region_status={"eu-west-1": "invalid"},
    )

    await monitor._reconcile_running(
        "ep", NAMESPACE, {"image": "image:v1", "replicas": 1}, endpoint
    )

    store.update_desired_state.assert_not_called()


def test_mooncake_configmap_propagates_nonconflict_create_error() -> None:
    monitor = _make_monitor()
    monitor.core_v1.create_namespaced_config_map.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        monitor._ensure_mooncake_configmap("ep", NAMESPACE, {"protocol": "rdma"})


def test_role_deployment_restarts_zero_autoscaled_target_and_scales_static_drift() -> None:
    monitor = _make_monitor()
    monitor._scale_deployment = MagicMock()  # type: ignore[method-assign]
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=[_deployment(replicas=0, ready=0), _deployment(replicas=1, ready=1)]
    )
    autoscaled = {
        "mooncake": {
            "topology": {"prefill": 2, "decode": 1},
            "autoscaling": {
                "enabled": True,
                "prefill": {"min_replicas": 2, "max_replicas": 5},
            },
        }
    }
    static = {"mooncake": {"topology": {"prefill": 3, "decode": 1}}}

    assert monitor._ensure_role_deployment("ep", NAMESPACE, autoscaled, "prefill") == (
        0,
        2,
        True,
    )
    assert monitor._ensure_role_deployment("ep", NAMESPACE, static, "prefill") == (
        1,
        3,
        False,
    )
    assert monitor._scale_deployment.call_args_list == [
        call("ep-prefill", NAMESPACE, 2),
        call("ep-prefill", NAMESPACE, 3),
    ]


def test_report_role_status_marks_underready_split_topology_creating() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=[_deployment(replicas=2, ready=1), None]
    )
    mooncake = {
        "mode": "disaggregated",
        "topology": {"prefill": 2, "decode": 1},
    }

    assert monitor._report_role_status("ep", NAMESPACE, mooncake, {}, _endpoint()) == "creating"
    kwargs = store.update_region_status.call_args.kwargs
    assert kwargs["replicas_ready"] == 1
    assert kwargs["replicas_desired"] == 3


def _admit_mooncake(monitor: InferenceMonitor) -> None:
    monitor._resolve_region_services = MagicMock(  # type: ignore[method-assign]
        return_value=RegionServicesResolution(
            region_services={
                "metadata_server": "http://mooncake-master:8080/metadata",
                "master_server_address": "mooncake-master:50051",
            }
        )
    )
    monitor._resolve_regional_scope = MagicMock(  # type: ignore[method-assign]
        return_value=RegionalScopeResolution(in_region=True)
    )
    monitor._gate_on_mooncake_master = MagicMock(  # type: ignore[method-assign]
        return_value=MasterReadinessGate(proceed=True)
    )
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_mooncake_reconcile_reports_incomplete_autoscaler_ownership_counts() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult(errors=("old owner still present",))
    )
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=[_deployment(replicas=2, ready=1), _deployment(replicas=1, ready=0)]
    )
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 2, "decode": 1},
            "transfer": {"protocol": "tcp"},
        }
    }

    result = await monitor._reconcile_mooncake("ep", NAMESPACE, spec, _endpoint())

    assert result == {
        "action": "reconcile_mooncake_autoscaler",
        "endpoint": "ep",
        "cleanup_complete": False,
    }
    kwargs = store.update_region_status.call_args.kwargs
    assert kwargs["replicas_ready"] == 1
    assert kwargs["replicas_desired"] == 3
    assert "old owner still present" in kwargs["error"]


@pytest.mark.asyncio
async def test_mooncake_reconcile_waits_for_verified_hpa_owner() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._delete_autoscalers = MagicMock(return_value=ResourceCleanupResult())  # type: ignore[method-assign]
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._ensure_role_deployment = MagicMock(return_value=(1, 1, False))  # type: ignore[method-assign]
    monitor._create_role_hpa = MagicMock()  # type: ignore[method-assign]
    monitor._verify_hpa_owner = MagicMock(  # type: ignore[method-assign]
        side_effect=[ResourceCleanupResult(pending=("hpa/ep-prefill",)), ResourceCleanupResult()]
    )
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 1, "decode": 1},
            "transfer": {"protocol": "tcp"},
            "autoscaling": {
                "enabled": True,
                "prefill": {"min_replicas": 1, "max_replicas": 2},
                "decode": {"min_replicas": 1, "max_replicas": 2},
            },
        }
    }

    result = await monitor._reconcile_mooncake("ep", NAMESPACE, spec, _endpoint())

    assert result and result["cleanup_complete"] is False
    assert store.update_region_status.call_args.args[2] == "updating"


@pytest.mark.asyncio
async def test_mooncake_reconcile_rejects_unavailable_admin_secret() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._delete_autoscalers = MagicMock(return_value=ResourceCleanupResult())  # type: ignore[method-assign]
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._ensure_role_deployment = MagicMock(return_value=(1, 1, False))  # type: ignore[method-assign]
    monitor._create_role_service = MagicMock()  # type: ignore[method-assign]
    monitor._create_pd_proxy = MagicMock(  # type: ignore[method-assign]
        side_effect=AdminApiKeySecretError("admin", "Secret not found")
    )
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 1, "decode": 1},
            "transfer": {"protocol": "tcp"},
        }
    }

    result = await monitor._reconcile_mooncake("ep", NAMESPACE, spec, _endpoint())

    assert result and result["failed"] == "admin_api_key"
    assert store.update_region_status.call_args.args[2] == "failed"


# ---------------------------------------------------------------------------
# Regional address/master/network policy behavior
# ---------------------------------------------------------------------------


def test_metadata_server_url_honors_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = _make_monitor()
    monkeypatch.setenv("MOONCAKE_METADATA_SERVER", "https://metadata.internal/custom")
    assert monitor._metadata_server_url("mooncake-master:50051") == (
        "https://metadata.internal/custom"
    )


def test_regional_bucket_resolution_swallows_ssm_failure() -> None:
    monitor = _make_monitor()
    with patch(
        "gco.services.aws_ssm.get_ssm_parameter_optional",
        side_effect=RuntimeError("SSM unavailable"),
    ):
        assert monitor._resolve_regional_shared_bucket() is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("", "unknown"),
        ("http://[invalid", "unknown"),
        ("https://service.us-west-2.amazonaws.com", "us-west-2"),
        ("model.gco-inference.svc.cluster.local", REGION),
        ("public.example.com", "unknown"),
    ],
)
def test_region_address_classification(address: str, expected: str) -> None:
    assert _make_monitor()._region_of_address(address) == expected


def test_regional_scope_collects_explicit_peers_without_region_services() -> None:
    monitor = _make_monitor()
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "store": {"master_server_address": "mooncake-master:50051"},
            "transfer": {
                "peer_addresses": [
                    "ep-prefill.gco-inference.svc.cluster.local",
                    "",
                    "ep-prefill.gco-inference.svc.cluster.local",
                ]
            },
        }
    }

    result = monitor._resolve_regional_scope("ep", NAMESPACE, spec, None)

    assert result.in_region is True
    assert result.peer_addresses.count("ep-prefill.gco-inference.svc.cluster.local") == 1
    assert "mooncake-master:50051" in result.peer_addresses


@pytest.mark.parametrize("failing_api", ["service", "statefulset"])
def test_shared_master_propagates_nonconflict_create_failure(failing_api: str) -> None:
    monitor = _make_monitor()
    if failing_api == "service":
        monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=500)
    else:
        monitor.apps_v1.create_namespaced_stateful_set.side_effect = ApiException(status=500)

    with pytest.raises(ApiException):
        monitor._ensure_mooncake_store(NAMESPACE, {"mooncake": {"store": {"master_image": "m:v1"}}})


@pytest.mark.parametrize("status", [404, 500])
def test_master_ready_status_distinguishes_absence_from_failure(status: int) -> None:
    monitor = _make_monitor()
    monitor.apps_v1.read_namespaced_stateful_set_status.side_effect = ApiException(status=status)
    if status == 404:
        assert monitor._mooncake_master_ready_replicas(NAMESPACE) == 0
    else:
        with pytest.raises(ApiException):
            monitor._mooncake_master_ready_replicas(NAMESPACE)


def test_master_gate_clears_ready_clock_and_reports_timeout() -> None:
    monitor = _make_monitor()
    monitor._ensure_mooncake_store = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_intra_namespace_network_policies = MagicMock()  # type: ignore[method-assign]
    monitor._master_deferral_since["ready"] = datetime.now(UTC) - timedelta(seconds=5)
    monitor._mooncake_master_ready_replicas = MagicMock(side_effect=[1, 0])  # type: ignore[method-assign]

    ready = monitor._gate_on_mooncake_master("ready", NAMESPACE, {})
    assert ready.proceed is True
    assert "ready" not in monitor._master_deferral_since

    monitor._master_deferral_since["slow"] = datetime.now(UTC) - timedelta(
        seconds=MOONCAKE_MASTER_READY_TIMEOUT_SECONDS + 1
    )
    timed_out = monitor._gate_on_mooncake_master("slow", NAMESPACE, {})
    assert timed_out.proceed is False
    assert timed_out.error == "shared master did not become Ready"


def test_network_policy_invalid_bootstrap_port_falls_back_to_default() -> None:
    monitor = _make_monitor()
    monitor.networking_v1.create_namespaced_network_policy.return_value = None

    monitor._ensure_intra_namespace_network_policies(
        NAMESPACE,
        {"mooncake": {"transfer": {"bootstrap_base_port": "not-a-number"}}},
    )

    assert monitor.networking_v1.create_namespaced_network_policy.call_count == 4


# ---------------------------------------------------------------------------
# Deployment rendering and Secret/proxy/service failures
# ---------------------------------------------------------------------------


def test_legacy_vllm_detection_injects_root_path_and_initializes_accelerator_maps() -> None:
    monitor = _make_monitor()
    deployment = monitor._build_inference_deployment_object(
        name="ep",
        deploy_name="ep",
        app_label="ep",
        namespace=NAMESPACE,
        spec={
            "image": "vllm/vllm-openai:v1",
            "gpu_count": 1,
            "resources": {"requests": None, "limits": None},
        },
        replicas=1,
    )
    container = deployment.spec.template.spec.containers[0]

    assert container.args == ["--root-path", "/inference/ep"]
    assert container.resources.requests["nvidia.com/gpu"] == "1"
    assert container.resources.limits["nvidia.com/gpu"] == "1"


def test_legacy_unknown_runtime_does_not_inject_vllm_root_path() -> None:
    deployment = _make_monitor()._build_inference_deployment_object(
        name="ep",
        deploy_name="ep",
        app_label="ep",
        namespace=NAMESPACE,
        spec={"image": "private/runtime:v1", "gpu_count": 0},
        replicas=1,
    )
    assert deployment.spec.template.spec.containers[0].args is None


def test_admin_secret_read_propagates_nonabsence_failure() -> None:
    monitor = _make_monitor()
    monitor.core_v1.read_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        monitor._verify_admin_api_key_secret({"admin_api_key_secret": "admin"}, NAMESPACE)


def test_admin_secret_rejects_malformed_base64() -> None:
    secret = client.V1Secret(
        data={ADMIN_API_KEY_SECRET_DATA_KEY: "%%%not-base64%%%"},
        string_data=None,
    )
    assert InferenceMonitor._secret_has_admin_api_key(secret) is False


def test_generated_secret_read_and_create_propagate_nonconflict_failures() -> None:
    monitor = _make_monitor()
    monitor.core_v1.read_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        monitor._provision_admin_api_key_secret("ep-admin", NAMESPACE, LIFECYCLE)

    monitor.core_v1.reset_mock()
    monitor.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)
    monitor.core_v1.create_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        monitor._provision_admin_api_key_secret("ep-admin", NAMESPACE, LIFECYCLE)


def test_concurrent_conventional_secret_rejection_is_specific() -> None:
    monitor = _make_monitor()
    existing = client.V1Secret(
        metadata=client.V1ObjectMeta(name="ep-admin", labels={}),
        string_data={ADMIN_API_KEY_SECRET_DATA_KEY: "key"},
    )
    monitor.core_v1.read_namespaced_secret.side_effect = [
        ApiException(status=404),
        existing,
    ]
    monitor.core_v1.create_namespaced_secret.side_effect = ApiException(status=409)
    monitor._authorize_resource = MagicMock(return_value=existing)  # type: ignore[method-assign]

    with pytest.raises(AdminApiKeySecretError, match="concurrent conventional Secret"):
        monitor._provision_admin_api_key_secret("ep-admin", NAMESPACE, LIFECYCLE)


def test_proxy_requires_endpoint_lifecycle_identity() -> None:
    monitor = _make_monitor()
    with pytest.raises(AdminApiKeySecretError, match="no immutable lifecycle"):
        monitor._create_pd_proxy(
            "ep",
            NAMESPACE,
            {"mooncake": {"proxy": {"image": "proxy:v1"}}},
            {"endpoint_name": "ep"},
        )


def test_proxy_invalid_replica_count_defaults_to_one_and_conflict_disappearance_fences() -> None:
    monitor = _make_monitor()
    monitor._ensure_admin_api_key_secret = MagicMock(return_value="ep-admin")  # type: ignore[method-assign]
    monitor._ensure_pd_proxy_configmap = MagicMock()  # type: ignore[method-assign]
    monitor.apps_v1.create_namespaced_deployment.side_effect = ApiException(status=409)
    monitor._get_deployment = MagicMock(return_value=None)  # type: ignore[method-assign]
    spec = {"mooncake": {"proxy": {"image": "proxy:v1", "replicas": True}}}

    with pytest.raises(ReconcileFencedError, match="disappeared"):
        monitor._create_pd_proxy("ep", NAMESPACE, spec, _endpoint())

    deployment = monitor.apps_v1.create_namespaced_deployment.call_args.args[1]
    assert deployment.spec.replicas == 1


@pytest.mark.parametrize(
    "method_name",
    [
        "_create_role_service",
        "_ensure_pd_proxy_configmap",
        "_create_proxy_service",
        "_create_service",
    ],
)
def test_service_and_proxy_materializers_propagate_nonconflict_create_errors(
    method_name: str,
) -> None:
    monitor = _make_monitor()
    if method_name == "_ensure_pd_proxy_configmap":
        monitor.core_v1.create_namespaced_config_map.side_effect = ApiException(status=500)
        args = ("ep", NAMESPACE)
    elif method_name == "_create_role_service":
        monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=500)
        args = ("ep", NAMESPACE, "prefill")
    elif method_name == "_create_proxy_service":
        monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=500)
        args = ("ep-proxy", NAMESPACE)
    else:
        monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=500)
        args = ("ep", NAMESPACE, {"port": 8000})

    with pytest.raises(ApiException):
        getattr(monitor, method_name)(*args)


def test_ensure_service_propagates_nonabsence_read_error() -> None:
    monitor = _make_monitor()
    monitor._authorize_existing_service = MagicMock(side_effect=ApiException(status=500))  # type: ignore[method-assign]
    with pytest.raises(ApiException):
        monitor._ensure_service("ep", NAMESPACE, {})


# ---------------------------------------------------------------------------
# Deployment mutation, canary cleanup, admin adoption, and child inventory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name,args", [("_scale_deployment", (2,)), ("_update_deployment_image", ("new:v2",))]
)
def test_deployment_mutation_fences_disappearance(method_name: str, args: tuple[Any, ...]) -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(ReconcileFencedError, match="disappeared"):
        getattr(monitor, method_name)("ep", NAMESPACE, *args)


@pytest.mark.parametrize(
    "method_name,args", [("_scale_deployment", (2,)), ("_update_deployment_image", ("new:v2",))]
)
def test_deployment_mutation_includes_resource_version(
    method_name: str, args: tuple[Any, ...]
) -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(return_value=_deployment(rv="12"))  # type: ignore[method-assign]
    getattr(monitor, method_name)("ep", NAMESPACE, *args)
    body = monitor.apps_v1.patch_namespaced_deployment.call_args.kwargs["body"]
    assert body["metadata"] == {"resourceVersion": "12"}


def test_canary_unchanged_image_scales_replica_drift() -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=1, ready=3, image="canary:v2")
    )
    monitor._ensure_service = MagicMock()  # type: ignore[method-assign]
    monitor._scale_deployment = MagicMock()  # type: ignore[method-assign]

    result = monitor._reconcile_canary(
        "ep",
        NAMESPACE,
        {"image": "primary:v1"},
        {"image": "canary:v2", "replicas": 2, "weight": 25},
        _endpoint(),
    )

    assert result["state"] == "updating"
    assert result["replicas_ready"] == 2
    monitor._scale_deployment.assert_called_once_with("ep-canary", NAMESPACE, 2)


def test_cleanup_canary_handles_absence_and_non404_failures() -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(return_value=_owned_resource())  # type: ignore[method-assign]
    monitor.apps_v1.delete_namespaced_deployment.side_effect = ApiException(status=500)
    monitor._authorize_existing_service = MagicMock(side_effect=ApiException(status=500))  # type: ignore[method-assign]
    monitor._cleanup_canary("ep", NAMESPACE)

    monitor._get_deployment = MagicMock(return_value=None)  # type: ignore[method-assign]
    monitor._authorize_existing_service = MagicMock(side_effect=ApiException(status=404))  # type: ignore[method-assign]
    monitor._cleanup_canary("ep", NAMESPACE)


def test_cleanup_canary_logs_non404_service_delete_failure() -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(return_value=None)  # type: ignore[method-assign]
    monitor._authorize_existing_service = MagicMock(return_value=_owned_resource())  # type: ignore[method-assign]
    monitor.core_v1.delete_namespaced_service.side_effect = ApiException(status=500)
    monitor._cleanup_canary("ep", NAMESPACE)


def _legacy_admin_secret(
    *, resource_version: str | None = "1", key: str = "key"
) -> client.V1Secret:
    return client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="ep-admin",
            resource_version=resource_version,
            labels={
                "app": "ep-admin",
                "project": "gco",
                "gco.io/type": "inference",
            },
        ),
        string_data={ADMIN_API_KEY_SECRET_DATA_KEY: key},
        type="Opaque",
    )


def test_legacy_admin_secret_adoption_rejects_ambiguous_and_versionless_objects() -> None:
    monitor = _make_monitor()
    ambiguous = client.V1Secret(metadata=client.V1ObjectMeta(name="ep-admin", labels={}))
    with pytest.raises(AdminApiKeySecretError, match="ambiguous ownership"):
        monitor._adopt_legacy_admin_secret(ambiguous, "ep-admin", NAMESPACE, LIFECYCLE)
    with pytest.raises(AdminApiKeySecretError, match="no resource version"):
        monitor._adopt_legacy_admin_secret(
            _legacy_admin_secret(resource_version=None), "ep-admin", NAMESPACE, LIFECYCLE
        )


def test_legacy_admin_secret_adoption_handles_patch_and_verification_failures() -> None:
    monitor = _make_monitor()
    secret = _legacy_admin_secret()
    monitor.core_v1.patch_namespaced_secret.side_effect = ApiException(status=409)
    with pytest.raises(AdminApiKeySecretError, match="changed during lifecycle migration"):
        monitor._adopt_legacy_admin_secret(secret, "ep-admin", NAMESPACE, LIFECYCLE)

    monitor.core_v1.patch_namespaced_secret.reset_mock(side_effect=True)
    monitor.core_v1.read_namespaced_secret.return_value = secret
    with pytest.raises(AdminApiKeySecretError, match="could not be verified"):
        monitor._adopt_legacy_admin_secret(secret, "ep-admin", NAMESPACE, LIFECYCLE)


def test_generated_child_matching_covers_owner_and_label_combinations() -> None:
    deployment_names = ("ep", "ep-canary")
    service_names = ("ep",)
    unlabeled = _resource(name="pod", labels={})
    assert not InferenceMonitor._generated_child_matches(
        unlabeled, "pod", deployment_names, service_names
    )

    orphan = _resource(
        name="pod",
        labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
        owners=[],
    )
    assert InferenceMonitor._generated_child_matches(orphan, "pod", deployment_names, service_names)

    rs_owner = SimpleNamespace(kind="ReplicaSet", name="ep-rs")
    pod = _resource(
        name="pod",
        labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
        owners=[rs_owner],
    )
    assert InferenceMonitor._generated_child_matches(
        pod, "pod", deployment_names, service_names, ("ep-rs",)
    )
    assert not InferenceMonitor._generated_child_matches(
        pod, "replicaset", deployment_names, service_names
    )
    assert InferenceMonitor._generated_child_matches(
        _resource(name="ep"), "endpoints", deployment_names, service_names
    )
    assert InferenceMonitor._generated_child_matches(
        _resource(name="slice", labels={"kubernetes.io/service-name": "ep"}),
        "endpointslice",
        deployment_names,
        service_names,
    )
    assert not InferenceMonitor._generated_child_matches(
        orphan, "unknown", deployment_names, service_names
    )


def test_generated_child_observation_handles_bad_responses_and_dependency_order() -> None:
    monitor = _make_monitor()
    inventory = monitor._endpoint_resource_inventory("ep")
    replica_set = _resource(
        name="ep-rs",
        labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
        owners=[],
    )
    pod = _resource(
        name="ep-pod",
        labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
        owners=[SimpleNamespace(kind="ReplicaSet", name="ep-rs")],
    )
    monitor.apps_v1.list_namespaced_replica_set.return_value = SimpleNamespace(items=[replica_set])
    monitor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
    monitor.core_v1.list_namespaced_endpoints.return_value = SimpleNamespace(items="malformed")
    monitor.discovery_v1.list_namespaced_endpoint_slice.side_effect = RuntimeError("API down")
    pending: list[str] = []
    errors: list[str] = []

    assert monitor._observe_generated_children("ep", NAMESPACE, inventory, pending, errors) is True
    assert pending == ["replicaset/ep-rs", "pod/ep-pod"]
    assert errors and "list endpointslice ep failed" in errors[0]


def test_delete_resources_reports_generated_secret_with_unknown_lifecycle() -> None:
    monitor = _make_monitor()
    monitor._delete_autoscalers = MagicMock(return_value=ResourceCleanupResult())  # type: ignore[method-assign]
    monitor._delete_and_confirm = MagicMock(return_value=False)  # type: ignore[method-assign]
    monitor._observe_generated_children = MagicMock(return_value=False)  # type: ignore[method-assign]
    monitor.core_v1.read_namespaced_secret.return_value = _legacy_admin_secret()

    result = monitor._delete_resources("ep", NAMESPACE, expected_lifecycle_id=None)

    assert result.resources_found is True
    assert "lifecycle unknown" in result.errors[0]


# ---------------------------------------------------------------------------
# HPA/KEDA branches
# ---------------------------------------------------------------------------


def test_hpa_metric_and_keda_trigger_builders_handle_multiple_and_unknown_metrics() -> None:
    monitor = _make_monitor()
    hpa = monitor._build_hpa_metrics(
        [{"type": "cpu", "target": 60}, {"type": "memory", "target": 75}]
    )
    assert [metric.resource.name for metric in hpa] == ["cpu", "memory"]
    assert monitor._build_hpa_metrics([{"type": "unknown"}])[0].resource.name == "cpu"

    triggers = monitor._build_keda_triggers(
        [
            {"type": "cpu", "target": 55},
            {"type": "gpu", "target": 70},
            {"type": "unknown"},
        ],
        "ep",
        NAMESPACE,
    )
    assert [trigger["type"] for trigger in triggers] == ["cpu", "aws-cloudwatch"]
    assert monitor._build_keda_triggers([{"type": "unknown"}], "ep", NAMESPACE) == [
        {"type": "cpu", "metricType": "Utilization", "metadata": {"value": "70"}}
    ]


def test_scaled_object_and_hpa_create_propagate_nonconflict_errors() -> None:
    monitor = _make_monitor()
    custom = MagicMock()
    custom.create_namespaced_custom_object.side_effect = ApiException(status=500)
    with (
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
        pytest.raises(ApiException),
    ):
        monitor._apply_scaled_object("ep", NAMESPACE, "ep", 1, 3, [{"type": "gpu"}])

    hpa_api = MagicMock()
    hpa_api.create_namespaced_horizontal_pod_autoscaler.side_effect = ApiException(status=500)
    with (
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=hpa_api),
        pytest.raises(ApiException),
    ):
        monitor._apply_hpa("ep", NAMESPACE, "ep", 1, 3, [{"type": "cpu"}])


@pytest.mark.parametrize(
    "failure", [ApiException(status=404), ApiException(status=500), RuntimeError("down")]
)
def test_verify_hpa_owner_distinguishes_pending_and_read_errors(failure: Exception) -> None:
    monitor = _make_monitor()
    api = MagicMock()
    api.read_namespaced_horizontal_pod_autoscaler.side_effect = failure
    with patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=api):
        result = monitor._verify_hpa_owner("ep", NAMESPACE, "ep")
    if isinstance(failure, ApiException) and failure.status == 404:
        assert result.pending == ("hpa/ep",)
    else:
        assert result.errors


def test_verify_hpa_owner_reports_wrong_scale_target_pending() -> None:
    monitor = _make_monitor()
    api = MagicMock()
    api.read_namespaced_horizontal_pod_autoscaler.return_value = SimpleNamespace(
        spec=SimpleNamespace(
            scale_target_ref=SimpleNamespace(
                api_version="apps/v1", kind="Deployment", name="someone-else"
            )
        )
    )
    with patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=api):
        result = monitor._verify_hpa_owner("ep", NAMESPACE, "ep")
    assert result.pending == ("hpa/ep",)
    assert result.resources_found is True


def test_role_autoscaling_config_rejects_nonmapping_role() -> None:
    spec = {"mooncake": {"autoscaling": {"enabled": True, "prefill": ["not", "a", "mapping"]}}}
    assert InferenceMonitor._role_autoscaling_config(spec, "prefill") is None


# ---------------------------------------------------------------------------
# Entrypoint shutdown/restart behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_main_handles_unsupported_signal_hook_and_keyboard_interrupt() -> None:
    from gco.services.inference_monitor import main

    monitor = MagicMock()
    monitor.get_metrics.return_value = {}
    monitor.start = AsyncMock(side_effect=KeyboardInterrupt)
    loop = MagicMock()
    loop.add_signal_handler.side_effect = NotImplementedError

    with (
        patch(
            "gco.services.inference_monitor.create_inference_monitor_from_env", return_value=monitor
        ),
        patch("gco.services.service_metrics.start_metrics_server"),
        patch("gco.services.inference_monitor.asyncio.get_running_loop", return_value=loop),
    ):
        await main()

    monitor.stop.assert_called_once_with()
    loop.remove_signal_handler.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_main_restarts_after_crash_then_exits_on_shutdown_event() -> None:
    from gco.services.inference_monitor import main

    monitor = MagicMock()
    monitor.get_metrics.return_value = {}
    monitor.start = AsyncMock(side_effect=[RuntimeError("boom"), KeyboardInterrupt])
    loop = MagicMock()

    with (
        patch(
            "gco.services.inference_monitor.create_inference_monitor_from_env", return_value=monitor
        ),
        patch("gco.services.service_metrics.start_metrics_server"),
        patch("gco.services.inference_monitor.asyncio.get_running_loop", return_value=loop),
        patch("gco.services.inference_monitor.asyncio.sleep", AsyncMock()) as sleep,
    ):
        await main()

    sleep.assert_awaited_once_with(10)
    assert monitor.stop.call_count == 2
    assert monitor._running is False


def test_monitor_module_entry_runs_async_main() -> None:
    with patch("asyncio.run") as async_run, warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*gco.services.inference_monitor.*found in sys.modules.*",
            category=RuntimeWarning,
        )
        runpy.run_module("gco.services.inference_monitor", run_name="__main__")
        assert async_run.call_count == 1
        async_run.call_args.args[0].close()


# ---------------------------------------------------------------------------
# Final baseline-union outcomes: authority, ownership, and adapter boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_loop_treats_authority_loss_as_leadership_handoff() -> None:
    monitor = _make_monitor()

    with (
        patch.object(monitor, "_try_acquire_lease", return_value=True),
        patch.object(
            monitor,
            "_renewing_leadership",
            return_value=contextlib.nullcontext(),
        ),
        patch.object(
            monitor,
            "reconcile",
            AsyncMock(side_effect=ReconcileFencedError("lease changed")),
        ) as reconcile,
        patch(
            "gco.services.inference_monitor.asyncio.sleep",
            AsyncMock(side_effect=RuntimeError("stop loop")),
        ),
    ):
        await monitor.start()

    reconcile.assert_awaited_once_with()
    assert monitor._errors_count == 0


def test_deleting_status_without_generation_keeps_lifecycle_condition() -> None:
    monitor = _make_monitor()

    assert monitor._status_write_conditions({"lifecycle_id": LIFECYCLE}, deleting=True) == {
        "expected_lifecycle_id": LIFECYCLE
    }


def test_post_create_confirmation_distinguishes_api_and_fixture_boundaries() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()

    with pytest.raises(ApiException) as caught:
        monitor._confirm_created_resource(
            kind="service",
            resource_name="ep",
            read_resource=MagicMock(side_effect=ApiException(status=500)),
            delete_resource=MagicMock(),
        )
    assert caught.value.status == 500

    unversioned = _resource(resource_version=None)
    assert (
        monitor._confirm_created_resource(
            kind="service",
            resource_name="ep",
            read_resource=MagicMock(return_value=unversioned),
            delete_resource=MagicMock(),
        )
        is unversioned
    )

    verified = _owned_resource()
    with patch.object(monitor, "_assert_mutation_authority") as assert_authority:
        assert (
            monitor._confirm_created_resource(
                kind="service",
                resource_name="ep",
                read_resource=MagicMock(return_value=verified),
                delete_resource=MagicMock(),
            )
            is verified
        )
    assert_authority.assert_called_once_with()


@pytest.mark.asyncio
async def test_reconcile_never_refreshes_deleted_record_without_string_name() -> None:
    store = MagicMock()
    store.list_endpoints.return_value = [
        _endpoint(
            endpoint_name=7,
            desired_state="deleted",
            deletion_generation=DELETE_GENERATION,
            deletion_regions=[REGION],
        )
    ]
    monitor = _make_monitor(store)

    with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
        assert await monitor.reconcile() == []

    store.get_endpoint.assert_not_called()


@pytest.mark.asyncio
async def test_endpoint_reconcile_quiesces_terminal_delete_and_rejects_bad_spec() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    terminal = _endpoint(
        desired_state="deleted",
        deletion_generation=DELETE_GENERATION,
        deletion_regions=[REGION],
        region_status={
            REGION: {
                "state": "deleted",
                "lifecycle_id": LIFECYCLE,
                "deletion_generation": DELETE_GENERATION,
                "absence_observations": 2,
            }
        },
    )

    assert await monitor._reconcile_endpoint(terminal) is None

    rejected = await monitor._reconcile_endpoint(_endpoint(spec=["invalid"]))
    assert rejected == {
        "action": "reject",
        "endpoint": "ep",
        "reason": "invalid_spec",
    }
    assert store.update_region_status.call_args.args[2] == "failed"
    assert "must be a mapping" in store.update_region_status.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_missing_classic_deployment_reports_owner_cleanup_error() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    monitor._get_deployment = MagicMock(return_value=None)  # type: ignore[method-assign]
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult(errors=("old HPA read failed",))
    )

    result = await monitor._reconcile_running("ep", NAMESPACE, {"image": "image:v1"}, _endpoint())

    assert result == {
        "action": "reconcile_autoscaler",
        "endpoint": "ep",
        "cleanup_complete": False,
    }
    assert "old HPA read failed" in store.update_region_status.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_classic_canary_status_does_not_promote_without_lifecycle() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _prepare_existing_classic(monitor, _deployment())
    canary_status = {
        "state": "running",
        "image": "canary:v2",
        "weight": 20,
        "replicas_ready": 1,
        "replicas_desired": 1,
    }
    monitor._reconcile_canary = MagicMock(return_value=canary_status)  # type: ignore[method-assign]
    endpoint = _endpoint(desired_state="deploying", lifecycle_id="")
    spec = {
        "image": "image:v1",
        "replicas": 1,
        "canary": {"image": "canary:v2", "weight": 20},
    }

    assert await monitor._reconcile_running("ep", NAMESPACE, spec, endpoint) is None

    monitor._reconcile_canary.assert_called_once_with(
        "ep", NAMESPACE, spec, spec["canary"], endpoint
    )
    assert store.update_region_status.call_args.kwargs["extra"] == {"canary": canary_status}
    store.update_desired_state.assert_not_called()


def test_role_deployment_leaves_live_autoscaled_count_under_controller_ownership() -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=4, ready=2)
    )
    monitor._scale_deployment = MagicMock()  # type: ignore[method-assign]
    spec = {
        "mooncake": {
            "topology": {"prefill": 2, "decode": 1},
            "autoscaling": {
                "enabled": True,
                "prefill": {"min_replicas": 2, "max_replicas": 6},
            },
        }
    }

    assert monitor._ensure_role_deployment("ep", NAMESPACE, spec, "prefill") == (
        2,
        2,
        False,
    )
    monitor._scale_deployment.assert_not_called()


def test_role_status_handles_omitted_split_role_and_underready_store() -> None:
    split_store = MagicMock()
    split = _make_monitor(split_store)
    split._desired_roles = MagicMock(return_value=["prefill"])  # type: ignore[method-assign]
    split._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=1, ready=1)
    )

    assert (
        split._report_role_status(
            "ep",
            NAMESPACE,
            {"mode": "disaggregated", "topology": {"prefill": 1}},
            {},
        )
        == "running"
    )
    split._get_deployment.assert_called_once_with("ep-prefill", NAMESPACE)

    store_backend = MagicMock()
    store_monitor = _make_monitor(store_backend)
    store_monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=1, ready=0)
    )
    assert store_monitor._report_role_status("ep", NAMESPACE, {"mode": "store"}, {}) == "creating"
    assert store_backend.update_region_status.call_args.kwargs["replicas_ready"] == 0


def _autoscaled_mooncake_spec() -> dict[str, Any]:
    return {
        "mooncake": {
            "mode": "disaggregated",
            "topology": {"prefill": 1, "decode": 1},
            "transfer": {"protocol": "tcp"},
            "autoscaling": {
                "enabled": True,
                "prefill": {"min_replicas": 1, "max_replicas": 2},
                "decode": {"min_replicas": 1, "max_replicas": 2},
            },
        }
    }


@pytest.mark.asyncio
async def test_mooncake_pending_owner_without_error_stays_updating() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult(pending=("hpa/old",))
    )
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=1, ready=0)
    )

    result = await monitor._reconcile_mooncake(
        "ep", NAMESPACE, _autoscaled_mooncake_spec(), _endpoint()
    )

    assert result and result["cleanup_complete"] is False
    assert "error" not in store.update_region_status.call_args.kwargs


@pytest.mark.asyncio
async def test_mooncake_failed_owner_verification_reports_error() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._ensure_role_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=(1, 1, False)
    )
    monitor._create_role_hpa = MagicMock()  # type: ignore[method-assign]
    monitor._verify_hpa_owner = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            ResourceCleanupResult(errors=("HPA owner unreadable",)),
            ResourceCleanupResult(),
        ]
    )

    result = await monitor._reconcile_mooncake(
        "ep", NAMESPACE, _autoscaled_mooncake_spec(), _endpoint()
    )

    assert result and result["cleanup_complete"] is False
    assert "HPA owner unreadable" in store.update_region_status.call_args.kwargs["error"]


def test_region_address_with_no_hostname_is_unknown() -> None:
    assert _make_monitor()._region_of_address("file:///tmp/mooncake.sock") == "unknown"


def test_master_gate_reports_network_policy_rule_failure() -> None:
    from gco.services.inference_monitor import NetworkPolicyApplyError

    monitor = _make_monitor()
    monitor._ensure_mooncake_store = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_intra_namespace_network_policies = MagicMock(  # type: ignore[method-assign]
        side_effect=NetworkPolicyApplyError("allow-rdma-bootstrap", "forbidden")
    )

    gate = monitor._gate_on_mooncake_master("ep", NAMESPACE, {})

    assert gate.proceed is False
    assert gate.state == "creating"
    assert gate.error == "network policy allow-rdma-bootstrap could not be applied"


def test_existing_vllm_args_and_neuron_resources_are_extended_safely() -> None:
    deployment = _make_monitor()._build_inference_deployment_object(
        name="ep",
        deploy_name="ep",
        app_label="ep",
        namespace=NAMESPACE,
        spec={
            "image": "vllm/vllm-openai:v1",
            "args": ["--model", "org/model"],
            "accelerator": "neuron",
            "gpu_count": 2,
            "resources": {"requests": None, "limits": None},
        },
        replicas=1,
    )
    container = deployment.spec.template.spec.containers[0]

    assert container.args == [
        "--model",
        "org/model",
        "--root-path",
        "/inference/ep",
    ]
    assert container.resources.requests["aws.amazon.com/neuron"] == "2"
    assert container.resources.limits["aws.amazon.com/neuron"] == "2"


def test_proxy_deployment_propagates_nonconflict_create_failure() -> None:
    monitor = _make_monitor()
    monitor._ensure_admin_api_key_secret = MagicMock(return_value="ep-admin")  # type: ignore[method-assign]
    monitor._ensure_pd_proxy_configmap = MagicMock()  # type: ignore[method-assign]
    monitor.apps_v1.create_namespaced_deployment.side_effect = ApiException(status=500)

    with pytest.raises(ApiException) as caught:
        monitor._create_pd_proxy(
            "ep",
            NAMESPACE,
            {"mooncake": {"proxy": {"image": "proxy:v1"}}},
            _endpoint(),
        )

    assert caught.value.status == 500


def test_generated_child_rejects_matching_app_with_wrong_provenance() -> None:
    wrong_project = _resource(
        name="ep-pod",
        labels={"app": "ep", "project": "other", "gco.io/type": "inference"},
    )
    wrong_type = _resource(
        name="ep-rs",
        labels={"app": "ep", "project": "gco", "gco.io/type": "other"},
    )

    assert not InferenceMonitor._generated_child_matches(wrong_project, "pod", ("ep",), ("ep",))
    assert not InferenceMonitor._generated_child_matches(wrong_type, "replicaset", ("ep",), ("ep",))


def test_generated_child_observation_records_kubernetes_api_failure() -> None:
    monitor = _make_monitor()
    inventory = monitor._endpoint_resource_inventory("ep")
    monitor.apps_v1.list_namespaced_replica_set.side_effect = ApiException(
        status=500, reason="unavailable"
    )
    monitor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    monitor.core_v1.list_namespaced_endpoints.return_value = SimpleNamespace(items=[])
    monitor.discovery_v1.list_namespaced_endpoint_slice.return_value = SimpleNamespace(items=[])
    pending: list[str] = []
    errors: list[str] = []

    assert monitor._observe_generated_children("ep", NAMESPACE, inventory, pending, errors) is False
    assert pending == []
    assert errors and "list replicaset ep failed" in errors[0]


@pytest.mark.parametrize(
    "failure",
    [ApiException(status=500, reason="unavailable"), RuntimeError("transport closed")],
)
def test_delete_resources_records_generated_secret_read_failure(
    failure: Exception,
) -> None:
    monitor = _make_monitor()
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._delete_and_confirm = MagicMock(return_value=False)  # type: ignore[method-assign]
    monitor._observe_generated_children = MagicMock(return_value=False)  # type: ignore[method-assign]
    monitor.core_v1.read_namespaced_secret.side_effect = failure

    result = monitor._delete_resources("ep", NAMESPACE, expected_lifecycle_id=LIFECYCLE)

    assert result.resources_found is False
    assert result.errors and "read secret ep-admin failed" in result.errors[0]


def test_verify_hpa_owner_propagates_authority_fence() -> None:
    monitor = _make_monitor()
    api = MagicMock()
    api.read_namespaced_horizontal_pod_autoscaler.return_value = _owned_resource()
    monitor._authorize_resource = MagicMock(  # type: ignore[method-assign]
        side_effect=ReconcileFencedError("lease changed")
    )

    with (
        patch(
            "gco.services.inference_monitor.client.AutoscalingV2Api",
            return_value=api,
        ),
        pytest.raises(ReconcileFencedError, match="lease changed"),
    ):
        monitor._verify_hpa_owner("ep", NAMESPACE, "ep")


def test_role_autoscaling_rejects_nonmapping_mooncake_block() -> None:
    assert InferenceMonitor._role_autoscaling_config({"mooncake": ["invalid"]}, "prefill") is None


def test_canary_with_stable_image_and_replicas_reports_running() -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=2, ready=2, image="canary:v2")
    )
    monitor._ensure_service = MagicMock()  # type: ignore[method-assign]

    result = monitor._reconcile_canary(
        "ep",
        NAMESPACE,
        {"image": "primary:v1"},
        {"image": "canary:v2", "replicas": 2, "weight": 25},
        _endpoint(),
    )

    assert result == {
        "state": "running",
        "image": "canary:v2",
        "weight": 25,
        "replicas_ready": 2,
        "replicas_desired": 2,
    }


def test_post_create_compensation_quietly_accepts_already_absent_resource() -> None:
    monitor = _make_monitor()
    monitor._active_authority = _authority()
    created = _owned_resource()
    delete = MagicMock(side_effect=ApiException(status=404))

    with (
        patch.object(
            monitor,
            "_assert_mutation_authority",
            side_effect=ReconcileFencedError("lease changed"),
        ),
        pytest.raises(ReconcileFencedError, match="lease changed"),
    ):
        monitor._confirm_created_resource(
            kind="service",
            resource_name="ep",
            read_resource=MagicMock(return_value=created),
            delete_resource=delete,
        )

    delete.assert_called_once()


def test_stable_canary_below_desired_readiness_remains_creating() -> None:
    monitor = _make_monitor()
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment(replicas=2, ready=1, image="canary:v2")
    )
    monitor._ensure_service = MagicMock()  # type: ignore[method-assign]

    result = monitor._reconcile_canary(
        "ep",
        NAMESPACE,
        {"image": "primary:v1"},
        {"image": "canary:v2", "replicas": 2, "weight": 25},
        _endpoint(),
    )

    assert result["state"] == "creating"
    assert result["replicas_ready"] == 1
