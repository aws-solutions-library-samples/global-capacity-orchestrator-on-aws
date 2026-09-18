"""Focused behavior coverage for the inference monitor's orchestration branches."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from kubernetes.client.rest import ApiException

from gco.services.inference_monitor import (
    AWS_CLI_IMAGE,
    MOONCAKE_BOOTSTRAP_BASE_PORT,
    VLLM_MOONCAKE_BOOTSTRAP_PORT_ENV,
    AdminApiKeySecretError,
    InferenceMonitor,
    MasterReadinessGate,
    ReconcileAuthority,
    ReconcileFencedError,
    RegionalScopeResolution,
    RegionServicesResolution,
    ResourceCleanupResult,
)

NAMESPACE = "gco-inference"
REGION_SERVICES = {
    "metadata_server": "http://mooncake-master:8080/metadata",
    "master_server_address": "mooncake-master:50051",
}
LIFECYCLE_ID = "life-1"
REGION_GENERATION = "region-1"
DELETION_GENERATION = "delete-1"


def _terminal_status(*, state: str = "deleted", observations: int = 2) -> dict[str, object]:
    return {
        "state": state,
        "lifecycle_id": LIFECYCLE_ID,
        "deletion_generation": DELETION_GENERATION,
        "absence_observations": observations,
    }


def _deleted_endpoint(
    name: str,
    regions: list[str],
    statuses: dict[str, object],
    *,
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "endpoint_name": name,
        "lifecycle_id": LIFECYCLE_ID,
        "desired_state": "deleted",
        "target_regions": list(regions),
        "cleanup_regions": list(regions),
        "region_generations": dict.fromkeys(regions, REGION_GENERATION),
        "deletion_regions": list(regions),
        "deletion_generation": DELETION_GENERATION,
        "updated_at": updated_at,
        "region_status": statuses,
        "spec": {},
        "namespace": NAMESPACE,
    }


def _make_monitor(store: MagicMock | None = None) -> InferenceMonitor:
    """Construct a monitor with the Kubernetes clients patched, as in the extended suite."""
    endpoint_store = store if store is not None else MagicMock()
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as mock_apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as mock_core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as mock_networking,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region="us-east-1",
            store=endpoint_store,
            namespace=NAMESPACE,
            reconcile_interval=5,
        )
        monitor.apps_v1 = mock_apps.return_value
        monitor.core_v1 = mock_core.return_value
        monitor.networking_v1 = mock_networking.return_value
        return monitor


def _deployment_with_ready_replicas(ready: int) -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(ready_replicas=ready))


def _admit_mooncake(monitor: InferenceMonitor) -> None:
    monitor._resolve_region_services = MagicMock(  # type: ignore[method-assign]
        return_value=RegionServicesResolution(region_services=dict(REGION_SERVICES))
    )
    monitor._resolve_regional_scope = MagicMock(  # type: ignore[method-assign]
        return_value=RegionalScopeResolution(in_region=True)
    )
    monitor._gate_on_mooncake_master = MagicMock(  # type: ignore[method-assign]
        return_value=MasterReadinessGate(proceed=True)
    )
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )


def _build_deployment(
    monitor: InferenceMonitor,
    spec: dict,
    *,
    extra_args: list[str] | None = None,
    extra_labels: dict[str, str] | None = None,
):
    return monitor._build_inference_deployment_object(
        name="chat",
        deploy_name="chat-worker",
        app_label="chat-worker",
        namespace=NAMESPACE,
        spec=spec,
        replicas=1,
        extra_args=extra_args,
        extra_labels=extra_labels,
    )


@pytest.mark.asyncio
async def test_reconcile_returns_cleanly_when_endpoint_store_listing_fails() -> None:
    store = MagicMock()
    store.list_endpoints.side_effect = RuntimeError("dynamodb unavailable")
    monitor = _make_monitor(store)
    reconcile_endpoint = AsyncMock()

    with patch.object(monitor, "_reconcile_endpoint", reconcile_endpoint):
        actions = await monitor.reconcile()

    assert actions == []
    assert monitor.get_metrics()["reconcile_count"] == 1
    assert monitor.get_metrics()["errors_count"] == 0
    reconcile_endpoint.assert_not_awaited()
    store.update_region_status.assert_not_called()
    store.delete_endpoint.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_isolates_one_endpoint_failure_and_continues() -> None:
    store = MagicMock()
    failed = {"endpoint_name": "broken", "desired_state": "running"}
    healthy = {"endpoint_name": "healthy", "desired_state": "running"}
    store.list_endpoints.return_value = [failed, healthy]
    reconcile_endpoint = AsyncMock(
        side_effect=[
            RuntimeError("invalid persisted state"),
            {"action": "observe", "endpoint": "healthy"},
        ]
    )
    monitor = _make_monitor(store)

    with patch.object(monitor, "_reconcile_endpoint", reconcile_endpoint):
        actions = await monitor.reconcile()

    assert actions == [{"action": "observe", "endpoint": "healthy"}]
    assert reconcile_endpoint.await_args_list == [call(failed), call(healthy)]
    assert monitor.get_metrics()["errors_count"] == 1
    store.update_region_status.assert_called_once_with(
        "broken",
        "us-east-1",
        "error",
        error="invalid persisted state",
    )


@pytest.mark.asyncio
async def test_reconcile_does_not_turn_authority_loss_into_endpoint_error() -> None:
    store = MagicMock()
    endpoint = {
        "endpoint_name": "terminal",
        "desired_state": "deleted",
        "lifecycle_id": LIFECYCLE_ID,
        "deletion_generation": DELETION_GENERATION,
    }
    store.list_endpoints.return_value = [endpoint]
    store.get_endpoint.return_value = endpoint
    monitor = _make_monitor(store)

    with patch.object(
        monitor,
        "_reconcile_endpoint",
        AsyncMock(side_effect=ReconcileFencedError("leader Lease was lost")),
    ):
        assert await monitor.reconcile() == []

    store.update_region_status.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_purges_only_fresh_current_generation_quorum() -> None:
    store = MagicMock()
    eligible = _deleted_endpoint(
        "eligible",
        ["us-east-1", "eu-west-1"],
        {
            "us-east-1": _terminal_status(),
            "eu-west-1": _terminal_status(),
        },
    )
    stale_generation = _deleted_endpoint(
        "stale",
        ["us-east-1"],
        {"us-east-1": {**_terminal_status(), "deletion_generation": "old"}},
    )
    historical_missing = _deleted_endpoint(
        "historical-missing",
        ["us-east-1", "eu-west-1"],
        {"us-east-1": _terminal_status()},
    )
    store.list_endpoints.return_value = [eligible, stale_generation, historical_missing]
    store.get_endpoint.side_effect = [eligible, stale_generation, historical_missing]
    monitor = _make_monitor(store)

    with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
        actions = await monitor.reconcile()

    store.delete_endpoint.assert_called_once_with(
        "eligible",
        expected_updated_at="2026-01-01T00:00:00+00:00",
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_deletion_generation=DELETION_GENERATION,
    )
    assert actions == [{"action": "purge", "endpoint": "eligible"}]


@pytest.mark.asyncio
async def test_reconcile_keeps_fully_deleted_record_when_purge_fails() -> None:
    store = MagicMock()
    endpoint = _deleted_endpoint(
        "retry-later",
        ["us-east-1"],
        {"us-east-1": _terminal_status()},
        updated_at="2026-01-02T00:00:00+00:00",
    )
    store.list_endpoints.return_value = [endpoint]
    store.get_endpoint.return_value = endpoint
    store.delete_endpoint.side_effect = RuntimeError("conditional delete failed")
    monitor = _make_monitor(store)

    cleanup_action = {
        "action": "delete",
        "endpoint": "retry-later",
        "cleanup_complete": True,
    }
    with patch.object(
        monitor,
        "_reconcile_endpoint",
        AsyncMock(return_value=cleanup_action),
    ):
        actions = await monitor.reconcile()

    assert actions == [cleanup_action]
    store.delete_endpoint.assert_called_once_with(
        "retry-later",
        expected_updated_at="2026-01-02T00:00:00+00:00",
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_deletion_generation=DELETION_GENERATION,
    )


@pytest.mark.asyncio
async def test_async_resource_presence_keeps_deleting_and_blocks_stale_purge() -> None:
    store = MagicMock()
    endpoint = _deleted_endpoint(
        "pending-ep",
        ["us-east-1", "us-west-2"],
        {
            "us-east-1": {
                **_terminal_status(state="deleting", observations=1),
            },
            "us-west-2": _terminal_status(),
        },
    )
    store.list_endpoints.return_value = [endpoint]
    monitor = _make_monitor(store)
    pending = ResourceCleanupResult(
        pending=("service/pending-ep",),
        resources_found=True,
    )

    with patch.object(monitor, "_delete_resources", return_value=pending):
        actions = await monitor.reconcile()

    assert actions == [
        {
            "action": "delete",
            "endpoint": "pending-ep",
            "cleanup_complete": False,
        }
    ]
    store.update_region_status.assert_called_once_with(
        "pending-ep",
        "us-east-1",
        "deleting",
        extra={"absence_observations": 0},
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_deletion_generation=DELETION_GENERATION,
    )
    store.delete_endpoint.assert_not_called()


@pytest.mark.asyncio
async def test_non_404_cleanup_failure_is_surfaced_and_remains_retryable() -> None:
    store = MagicMock()
    endpoint = _deleted_endpoint(
        "failed-ep",
        ["us-east-1"],
        {
            "us-east-1": {
                **_terminal_status(state="deleting", observations=1),
            }
        },
    )
    store.list_endpoints.return_value = [endpoint]
    monitor = _make_monitor(store)
    failed = ResourceCleanupResult(
        errors=("delete service failed-ep failed (status 500)",),
        resources_found=True,
    )

    with patch.object(monitor, "_delete_resources", return_value=failed):
        actions = await monitor.reconcile()

    assert actions == [{"action": "delete", "endpoint": "failed-ep", "cleanup_complete": False}]
    store.update_region_status.assert_called_once_with(
        "failed-ep",
        "us-east-1",
        "deleting",
        extra={"absence_observations": 0},
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_deletion_generation=DELETION_GENERATION,
        error=(
            "Endpoint cleanup could not be completed: delete service failed-ep failed (status 500)"
        ),
    )
    store.delete_endpoint.assert_not_called()


@pytest.mark.asyncio
async def test_verified_complete_absence_marks_deleted_and_permits_purge() -> None:
    store = MagicMock()
    endpoint = _deleted_endpoint(
        "gone-ep",
        ["us-east-1"],
        {
            "us-east-1": {
                **_terminal_status(state="deleting", observations=1),
            }
        },
        updated_at="2026-01-03T00:00:00+00:00",
    )
    refreshed = _deleted_endpoint(
        "gone-ep",
        ["us-east-1"],
        {"us-east-1": _terminal_status()},
        updated_at="after-terminal-write",
    )
    store.list_endpoints.return_value = [endpoint]
    store.get_endpoint.return_value = refreshed
    monitor = _make_monitor(store)

    with patch.object(
        monitor,
        "_delete_resources",
        return_value=ResourceCleanupResult(),
    ):
        actions = await monitor.reconcile()

    store.update_region_status.assert_called_once_with(
        "gone-ep",
        "us-east-1",
        "deleted",
        extra={"absence_observations": 2},
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_deletion_generation=DELETION_GENERATION,
    )
    store.delete_endpoint.assert_called_once_with(
        "gone-ep",
        expected_updated_at="after-terminal-write",
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_deletion_generation=DELETION_GENERATION,
    )
    assert actions == [
        {"action": "delete", "endpoint": "gone-ep", "cleanup_complete": True},
        {"action": "purge", "endpoint": "gone-ep"},
    ]


@pytest.mark.parametrize(
    ("ready_by_role", "expected_state"),
    [
        ({"prefill": 2, "decode": 1}, "running"),
        ({"prefill": 2, "decode": 0}, "creating"),
    ],
)
def test_report_role_status_uses_split_role_readiness(
    ready_by_role: dict[str, int], expected_state: str
) -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    deployments = {
        f"chat-{role}": _deployment_with_ready_replicas(ready)
        for role, ready in ready_by_role.items()
    }
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda name, _namespace: deployments[name]
    )
    mooncake = {
        "mode": "disaggregated",
        "topology": {"prefill": 2, "decode": 1},
    }

    state = monitor._report_role_status("chat", NAMESPACE, mooncake, REGION_SERVICES)

    assert state == expected_state
    store.update_region_status.assert_called_once_with(
        "chat",
        "us-east-1",
        expected_state,
        replicas_ready=sum(ready_by_role.values()),
        replicas_desired=3,
        extra={
            "roles": {
                "prefill": {"ready": ready_by_role["prefill"], "desired": 2},
                "decode": {"ready": ready_by_role["decode"], "desired": 1},
            }
        },
    )


@pytest.mark.parametrize(("master_ready", "expected_state"), [(1, "running"), (0, "creating")])
def test_report_role_status_requires_store_master_readiness(
    master_ready: int, expected_state: str
) -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    monitor._get_deployment = MagicMock(  # type: ignore[method-assign]
        return_value=_deployment_with_ready_replicas(1)
    )
    monitor._mooncake_master_ready_replicas = MagicMock(  # type: ignore[method-assign]
        return_value=master_ready
    )
    mooncake = {"mode": "store", "store": {"enabled": True}}

    state = monitor._report_role_status("chat", NAMESPACE, mooncake, REGION_SERVICES)

    assert state == expected_state
    store.update_region_status.assert_called_once_with(
        "chat",
        "us-east-1",
        expected_state,
        replicas_ready=1,
        replicas_desired=1,
        extra={
            "store": {
                "ready": bool(master_ready),
                "master": "mooncake-master:50051",
            }
        },
    )


@pytest.mark.asyncio
async def test_reconcile_mooncake_treats_an_empty_block_as_classic_endpoint() -> None:
    monitor = _make_monitor()
    resolve = MagicMock()
    monitor._resolve_region_services = resolve  # type: ignore[method-assign]

    action = await monitor._reconcile_mooncake(
        "chat",
        NAMESPACE,
        {"image": "example/runtime:1", "mooncake": {}},
        {},
    )

    assert action is None
    resolve.assert_not_called()
    monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    monitor.core_v1.create_namespaced_service.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_mooncake_defers_an_unresolved_store_without_materializing() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    monitor._resolve_region_services = MagicMock(  # type: ignore[method-assign]
        return_value=RegionServicesResolution(
            render_skipped=True,
            store_master_unresolved=True,
            error="own-region store master is unresolved",
        )
    )
    monitor._resolve_regional_scope = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    spec = {"mooncake": {"mode": "store", "store": {"enabled": True}}}

    action = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})

    assert action == {
        "action": "reconcile_mooncake",
        "endpoint": "chat",
        "deferred": "store_master_unresolved",
    }
    store.update_region_status.assert_called_once_with(
        "chat",
        "us-east-1",
        "creating",
        error="own-region store master is unresolved",
    )
    monitor._resolve_regional_scope.assert_not_called()
    monitor._ensure_mooncake_configmap.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_mooncake_rejects_cross_region_scope_before_materializing() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    monitor._resolve_region_services = MagicMock(  # type: ignore[method-assign]
        return_value=RegionServicesResolution(region_services=dict(REGION_SERVICES))
    )
    monitor._resolve_regional_scope = MagicMock(  # type: ignore[method-assign]
        return_value=RegionalScopeResolution(
            in_region=False,
            state="failed",
            error="cross-region boundary violation: peer.eu-west-1.internal",
        )
    )
    monitor._gate_on_mooncake_master = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    spec = {"mooncake": {"mode": "disaggregated", "transfer": {"protocol": "rdma"}}}

    action = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})

    assert action == {
        "action": "reconcile_mooncake",
        "endpoint": "chat",
        "failed": "cross_region_boundary",
    }
    store.update_region_status.assert_called_once_with(
        "chat",
        "us-east-1",
        "failed",
        error="cross-region boundary violation: peer.eu-west-1.internal",
    )
    monitor._gate_on_mooncake_master.assert_not_called()
    monitor._ensure_mooncake_configmap.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_mooncake_honors_closed_master_gate() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._gate_on_mooncake_master = MagicMock(  # type: ignore[method-assign]
        return_value=MasterReadinessGate(
            proceed=False,
            state="creating",
            error="shared master did not become Ready",
        )
    )
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_role_deployment = MagicMock(return_value=(0, 1, False))  # type: ignore[method-assign]
    spec = {"mooncake": {"mode": "store", "store": {"enabled": True}}}

    action = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})

    assert action == {
        "action": "reconcile_mooncake",
        "endpoint": "chat",
        "deferred": "master_not_ready",
    }
    store.update_region_status.assert_called_once_with(
        "chat",
        "us-east-1",
        "creating",
        error="shared master did not become Ready",
    )
    monitor._ensure_mooncake_configmap.assert_not_called()
    monitor._ensure_role_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_mooncake_orders_roles_autoscalers_and_front_end() -> None:
    monitor = _make_monitor()
    events: list[str] = []

    def resolve(*_args):
        events.append("resolve")
        return RegionServicesResolution(region_services=dict(REGION_SERVICES))

    def scope(*_args):
        events.append("scope")
        return RegionalScopeResolution(in_region=True)

    def gate(*_args):
        events.append("gate")
        return MasterReadinessGate(proceed=True)

    def report(*_args):
        events.append("status")
        return "running"

    def ensure_role(_name, _ns, _spec, role):
        events.append(f"role:{role}")
        return 0, 1, False

    monitor._resolve_region_services = MagicMock(side_effect=resolve)  # type: ignore[method-assign]
    monitor._resolve_regional_scope = MagicMock(side_effect=scope)  # type: ignore[method-assign]
    monitor._gate_on_mooncake_master = MagicMock(side_effect=gate)  # type: ignore[method-assign]
    monitor._ensure_mooncake_configmap = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_args: events.append("config")
    )
    monitor._delete_autoscalers = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._reconcile_role_autoscaler = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult()
    )
    monitor._ensure_role_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=ensure_role
    )
    monitor._create_role_hpa = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _name, _ns, _spec, role: events.append(f"autoscaler:{role}")
    )
    monitor._verify_hpa_owner = MagicMock(  # type: ignore[method-assign]
        return_value=ResourceCleanupResult(resources_found=True)
    )
    monitor._create_role_service = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _name, _ns, role, _port: events.append(f"service:{role}")
    )
    monitor._create_pd_proxy = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_args: events.append("proxy")
    )
    monitor._report_role_status = MagicMock(side_effect=report)  # type: ignore[method-assign]
    spec = {
        "image": "vllm/vllm-openai:pinned",
        "port": 9000,
        "mooncake": {
            "mode": "both",
            "store": {"enabled": True},
            "topology": {"prefill": 2, "decode": 3},
            "autoscaling": {
                "enabled": True,
                "prefill": {"metrics": [{"type": "cpu", "target": 70}]},
                "decode": {"metrics": [{"type": "cpu", "target": 70}]},
            },
        },
    }

    action = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})

    assert action == {"action": "reconcile_mooncake", "endpoint": "chat", "state": "running"}
    assert events == [
        "resolve",
        "scope",
        "gate",
        "config",
        "role:prefill",
        "role:decode",
        "autoscaler:prefill",
        "autoscaler:decode",
        "service:prefill",
        "service:decode",
        "proxy",
        "status",
    ]


@pytest.mark.asyncio
async def test_mooncake_delayed_keda_hpa_blocks_proxy_across_reconcile_passes() -> None:
    monitor = _make_monitor()
    _admit_mooncake(monitor)
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    pass_number = {"value": 0}

    def ensure_role(_name, _namespace, _spec, role):
        restarted = role == "prefill" and pass_number["value"] == 0
        return 0, 2 if role == "prefill" else 1, restarted

    monitor._ensure_role_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=ensure_role
    )
    monitor._create_role_hpa = MagicMock()  # type: ignore[method-assign]
    monitor._verify_hpa_owner = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            ResourceCleanupResult(pending=("hpa/keda-hpa-chat-prefill",)),
            ResourceCleanupResult(resources_found=True),
        ]
    )
    monitor._create_role_service = MagicMock()  # type: ignore[method-assign]
    monitor._create_pd_proxy = MagicMock()  # type: ignore[method-assign]
    monitor._report_role_status = MagicMock(return_value="running")  # type: ignore[method-assign]
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "transfer": {"protocol": "tcp"},
            "topology": {"prefill": 2, "decode": 1},
            "autoscaling": {
                "enabled": True,
                "prefill": {
                    "min_replicas": 2,
                    "metrics": [{"type": "gpu", "target": 60}],
                },
            },
        }
    }

    first = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})
    assert first == {
        "action": "reconcile_mooncake_autoscaler",
        "endpoint": "chat",
        "cleanup_complete": False,
    }
    monitor._create_pd_proxy.assert_not_called()

    pass_number["value"] = 1
    second = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})

    assert second == {"action": "reconcile_mooncake", "endpoint": "chat", "state": "running"}
    assert monitor._verify_hpa_owner.call_count == 2
    monitor._create_pd_proxy.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_mooncake_reports_admin_key_failure_without_status_overwrite() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_role_deployment = MagicMock(return_value=(0, 1, False))  # type: ignore[method-assign]
    monitor._create_role_service = MagicMock()  # type: ignore[method-assign]
    error = AdminApiKeySecretError("missing-admin", "Secret not found")
    monitor._create_pd_proxy = MagicMock(side_effect=error)  # type: ignore[method-assign]
    monitor._report_role_status = MagicMock()  # type: ignore[method-assign]
    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "transfer": {"protocol": "tcp"},
            "topology": {"prefill": 1, "decode": 1},
        }
    }

    action = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, {})

    assert action == {
        "action": "reconcile_mooncake",
        "endpoint": "chat",
        "failed": "admin_api_key",
    }
    store.update_region_status.assert_called_once_with(
        "chat", "us-east-1", "failed", error=str(error)
    )
    monitor._report_role_status.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_mooncake_store_mode_uses_direct_service_front_end() -> None:
    monitor = _make_monitor()
    _admit_mooncake(monitor)
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_role_deployment = MagicMock(return_value=(0, 1, False))  # type: ignore[method-assign]
    monitor._create_role_service = MagicMock()  # type: ignore[method-assign]
    monitor._create_pd_proxy = MagicMock()  # type: ignore[method-assign]
    monitor._create_service = MagicMock()  # type: ignore[method-assign]
    monitor._report_role_status = MagicMock(return_value="running")  # type: ignore[method-assign]
    spec = {
        "image": "vllm/vllm-openai:pinned",
        "mooncake": {"mode": "store", "store": {"enabled": True}},
    }
    endpoint = {"endpoint_name": "chat"}

    action = await monitor._reconcile_mooncake("chat", NAMESPACE, spec, endpoint)

    assert action == {"action": "reconcile_mooncake", "endpoint": "chat", "state": "running"}
    monitor._ensure_role_deployment.assert_called_once_with("chat", NAMESPACE, spec, "single")
    monitor._create_service.assert_called_once_with("chat", NAMESPACE, spec)
    monitor._create_role_service.assert_not_called()
    monitor._create_pd_proxy.assert_not_called()


@pytest.mark.parametrize(
    ("mooncake", "role", "expected"),
    [
        (
            {
                "topology": {"prefill": 9},
                "autoscaling": {"enabled": True, "prefill": {"min_replicas": 3}},
            },
            "prefill",
            3,
        ),
        (
            {
                "topology": {"prefill": 7},
                "autoscaling": {"enabled": True, "prefill": {"min_replicas": True}},
            },
            "prefill",
            7,
        ),
        ({"topology": {"decode": "4"}}, "decode", 4),
        ({}, "prefill", 1),
        ({}, "decode", 1),
        (
            {
                "topology": {"single": 8},
                "autoscaling": {"enabled": True, "single": {"min_replicas": 5}},
            },
            "single",
            1,
        ),
    ],
)
def test_replica_count_for_role_honors_precedence_and_defaults(
    mooncake: dict, role: str, expected: int
) -> None:
    monitor = _make_monitor()

    assert monitor._replica_count_for_role(mooncake, role) == expected


def test_ensure_service_recreates_only_after_not_found() -> None:
    monitor = _make_monitor()
    monitor.core_v1.read_namespaced_service.side_effect = ApiException(status=404)

    with patch.object(monitor, "_create_service") as create_service:
        monitor._ensure_service("chat", NAMESPACE, {"port": 9000})

    create_service.assert_called_once_with("chat", NAMESPACE, {"port": 9000})


def test_ensure_service_leaves_existing_service_untouched() -> None:
    monitor = _make_monitor()
    monitor.core_v1.read_namespaced_service.return_value = SimpleNamespace()

    with patch.object(monitor, "_create_service") as create_service:
        monitor._ensure_service("chat", NAMESPACE, {"port": 9000})

    create_service.assert_not_called()


def test_ensure_service_propagates_non_not_found_api_error() -> None:
    monitor = _make_monitor()
    failure = ApiException(status=503, reason="API unavailable")
    monitor.core_v1.read_namespaced_service.side_effect = failure

    with (
        patch.object(monitor, "_create_service") as create_service,
        pytest.raises(ApiException) as exc_info,
    ):
        monitor._ensure_service("chat", NAMESPACE, {"port": 9000})

    assert exc_info.value is failure
    create_service.assert_not_called()


def test_deployment_builder_deduplicates_existing_root_path() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "vllm/vllm-openai:pinned",
            "gpu_count": 0,
            "health_check_path": "/ready",
            "args": [
                "--root-path",
                "/inference/chat",
                "--tensor-parallel-size",
                "2",
            ],
        },
    )
    container = deployment.spec.template.spec.containers[0]

    assert container.args.count("--root-path") == 1
    assert container.args == [
        "--root-path",
        "/inference/chat",
        "--tensor-parallel-size",
        "2",
    ]
    assert container.readiness_probe.http_get.path == "/inference/chat/ready"


def test_deployment_builder_suppresses_root_path_for_explicit_command() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "vllm/vllm-openai:pinned",
            "gpu_count": 0,
            "health_check_path": "/ready",
            "command": ["python3", "custom_server.py"],
        },
    )
    container = deployment.spec.template.spec.containers[0]

    assert container.command == ["python3", "custom_server.py"]
    assert container.args is None
    assert container.readiness_probe.http_get.path == "/ready"


def test_deployment_builder_uses_literal_model_sync_argv_without_api_token() -> None:
    monitor = _make_monitor()
    model_source = "s3://model-bucket/prefix; echo not-shell-syntax"
    deployment = _build_deployment(
        monitor,
        {
            "image": "example/runtime:pinned",
            "gpu_count": 0,
            "model_source": model_source,
        },
    )
    pod = deployment.spec.template.spec
    (model_sync,) = pod.init_containers

    assert pod.service_account_name == "gco-service-account"
    assert pod.automount_service_account_token is False
    assert model_sync.image == AWS_CLI_IMAGE
    assert model_sync.command == ["aws"]
    assert model_sync.args == [
        "s3",
        "sync",
        model_source,
        "/models/chat",
        "--quiet",
    ]


def test_deployment_builder_appends_extra_args_and_merges_role_labels() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "example/runtime:pinned",
            "gpu_count": 0,
            "args": ["--served-model-name", "chat"],
        },
        extra_args=["--kv-transfer-config", "{}"],
        extra_labels={"gco.io/role": "prefill", "team": "inference"},
    )
    container = deployment.spec.template.spec.containers[0]

    assert container.args == [
        "--served-model-name",
        "chat",
        "--kv-transfer-config",
        "{}",
    ]
    for labels in (deployment.metadata.labels, deployment.spec.template.metadata.labels):
        assert labels["gco.io/role"] == "prefill"
        assert labels["team"] == "inference"
        assert labels["app"] == "chat-worker"


def test_deployment_builder_falls_back_from_malformed_mooncake_bootstrap_port() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "example/runtime:pinned",
            "gpu_count": 0,
            "mooncake": {
                "mode": "disaggregated",
                "transfer": {"bootstrap_base_port": ["not", "an", "integer"]},
            },
        },
    )
    container = deployment.spec.template.spec.containers[0]
    env = {entry.name: entry.value for entry in container.env or []}

    assert env[VLLM_MOONCAKE_BOOTSTRAP_PORT_ENV] == str(MOONCAKE_BOOTSTRAP_BASE_PORT)


def test_deployment_builder_accepts_empty_resource_maps_before_accelerator_injection() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "example/runtime:pinned",
            "gpu_count": 2,
            "resources": {"requests": {}, "limits": {}},
        },
    )
    resources = deployment.spec.template.spec.containers[0].resources

    assert resources.requests == {"nvidia.com/gpu": "2"}
    assert resources.limits == {"nvidia.com/gpu": "2"}


def test_deployment_builder_preserves_custom_selector_with_capacity_type() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "example/runtime:pinned",
            "gpu_count": 1,
            "node_selector": {"workload": "low-latency"},
            "capacity_type": "spot",
        },
    )

    assert deployment.spec.template.spec.node_selector == {
        "workload": "low-latency",
        "karpenter.sh/capacity-type": "spot",
    }


@pytest.mark.parametrize(
    ("holder", "status"),
    [("monitor-a", 409), ("", 409), ("monitor-a", 503)],
)
def test_lease_replace_errors_lose_leadership_without_escaping(holder: str, status: int) -> None:
    monitor = _make_monitor()
    coordination = MagicMock()
    coordination.read_namespaced_lease.return_value = SimpleNamespace(
        spec=SimpleNamespace(
            holder_identity=holder,
            renew_time=datetime.now(UTC),
        )
    )
    coordination.replace_namespaced_lease.side_effect = ApiException(status=status)

    with patch(
        "gco.services.inference_monitor.client.CoordinationV1Api",
        return_value=coordination,
    ):
        acquired = monitor._try_acquire_lease("inference-monitor-leader", "monitor-a")

    assert acquired is False
    coordination.replace_namespaced_lease.assert_called_once()


def test_master_status_helper_propagates_non_not_found_api_error() -> None:
    monitor = _make_monitor()
    failure = ApiException(status=403, reason="Forbidden")
    monitor.apps_v1.read_namespaced_stateful_set_status.side_effect = failure

    with pytest.raises(ApiException) as exc_info:
        monitor._mooncake_master_ready_replicas(NAMESPACE)

    assert exc_info.value is failure


@pytest.mark.asyncio
async def test_completed_region_removal_ack_is_zero_kubernetes_and_zero_ddb_write() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    endpoint = {
        "endpoint_name": "quiet-ep",
        "lifecycle_id": LIFECYCLE_ID,
        "desired_state": "running",
        "target_regions": ["eu-west-1"],
        "cleanup_regions": ["us-east-1", "eu-west-1"],
        "region_generations": {
            "us-east-1": REGION_GENERATION,
            "eu-west-1": "region-2",
        },
        "region_status": {
            "us-east-1": {
                "state": "deleted",
                "lifecycle_id": LIFECYCLE_ID,
                "region_generation": REGION_GENERATION,
                "absence_observations": 2,
            }
        },
        "spec": {},
    }

    with patch.object(monitor, "_delete_resources") as delete_resources:
        assert await monitor._reconcile_endpoint(endpoint) is None

    delete_resources.assert_not_called()
    store.update_region_status.assert_not_called()
    assert monitor.apps_v1.method_calls == []
    assert monitor.core_v1.method_calls == []
    assert monitor.networking_v1.method_calls == []


@pytest.mark.asyncio
async def test_region_remove_readd_remove_requires_current_membership_ack() -> None:
    store = MagicMock()
    store.update_region_status.return_value = True
    monitor = _make_monitor(store)
    endpoint = {
        "endpoint_name": "cycled-ep",
        "lifecycle_id": LIFECYCLE_ID,
        "desired_state": "running",
        "target_regions": ["eu-west-1"],
        "cleanup_regions": ["us-east-1", "eu-west-1"],
        "region_generations": {
            "us-east-1": "current-removal",
            "eu-west-1": "region-2",
        },
        "region_status": {
            "us-east-1": {
                "state": "deleted",
                "lifecycle_id": LIFECYCLE_ID,
                "region_generation": "old-removal",
                "absence_observations": 2,
            }
        },
        "spec": {},
    }

    with patch.object(
        monitor,
        "_delete_resources",
        return_value=ResourceCleanupResult(),
    ) as delete_resources:
        action = await monitor._reconcile_endpoint(endpoint)

    delete_resources.assert_called_once()
    assert action == {
        "action": "cleanup",
        "endpoint": "cycled-ep",
        "reason": "region_removed",
        "cleanup_complete": False,
    }
    store.update_region_status.assert_called_once_with(
        "cycled-ep",
        "us-east-1",
        "deleting",
        extra={"absence_observations": 1},
        expected_lifecycle_id=LIFECYCLE_ID,
        expected_region_generation="current-removal",
    )


@pytest.mark.parametrize(
    ("kind", "attribute", "child"),
    [
        (
            "replicaset",
            "apps_v1",
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="ep-rs",
                    labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
                    owner_references=[SimpleNamespace(kind="Deployment", name="ep")],
                )
            ),
        ),
        (
            "pod",
            "core_v1",
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="ep-pod",
                    labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
                )
            ),
        ),
        (
            "endpoints",
            "core_v1",
            SimpleNamespace(metadata=SimpleNamespace(name="ep", labels={})),
        ),
        (
            "endpointslice",
            "discovery_v1",
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="generated-slice",
                    labels={"kubernetes.io/service-name": "ep"},
                )
            ),
        ),
    ],
)
def test_each_generated_child_kind_blocks_terminal_absence(
    kind: str,
    attribute: str,
    child: SimpleNamespace,
) -> None:
    monitor = _make_monitor()
    monitor.discovery_v1 = MagicMock()
    monitor.apps_v1.list_namespaced_replica_set.return_value = MagicMock(items=[])
    monitor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[])
    monitor.core_v1.list_namespaced_endpoints.return_value = MagicMock(items=[])
    monitor.discovery_v1.list_namespaced_endpoint_slice.return_value = MagicMock(items=[])
    if kind == "replicaset":
        monitor.apps_v1.list_namespaced_replica_set.return_value = MagicMock(items=[child])
    elif kind == "pod":
        monitor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[child])
    elif kind == "endpoints":
        monitor.core_v1.list_namespaced_endpoints.return_value = MagicMock(items=[child])
    else:
        monitor.discovery_v1.list_namespaced_endpoint_slice.return_value = MagicMock(items=[child])

    pending: list[str] = []
    errors: list[str] = []
    found = monitor._observe_generated_children(
        "ep",
        NAMESPACE,
        monitor._endpoint_resource_inventory("ep"),
        pending,
        errors,
    )

    assert attribute in {"apps_v1", "core_v1", "discovery_v1"}
    assert found is True
    assert pending == [f"{kind}/{child.metadata.name}"]
    assert errors == []


def test_generated_child_inventory_ignores_shared_prefix_endpoint() -> None:
    monitor = _make_monitor()
    monitor.discovery_v1 = MagicMock()
    foreign_labels = {
        "app": "foo-v2",
        "project": "gco",
        "gco.io/type": "inference",
    }
    monitor.apps_v1.list_namespaced_replica_set.return_value = MagicMock(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="foo-v2-abc",
                    labels=foreign_labels,
                    owner_references=[SimpleNamespace(kind="Deployment", name="foo-v2")],
                )
            )
        ]
    )
    monitor.core_v1.list_namespaced_pod.return_value = MagicMock(
        items=[
            SimpleNamespace(metadata=SimpleNamespace(name="foo-v2-abc-pod", labels=foreign_labels))
        ]
    )
    monitor.core_v1.list_namespaced_endpoints.return_value = MagicMock(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="foo-v2", labels={}))]
    )
    monitor.discovery_v1.list_namespaced_endpoint_slice.return_value = MagicMock(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="foo-v2-slice",
                    labels={"kubernetes.io/service-name": "foo-v2"},
                )
            )
        ]
    )
    pending: list[str] = []
    errors: list[str] = []

    found = monitor._observe_generated_children(
        "foo",
        NAMESPACE,
        monitor._endpoint_resource_inventory("foo"),
        pending,
        errors,
    )

    assert found is False
    assert pending == []
    assert errors == []
    inventory = monitor._endpoint_resource_inventory("foo")
    assert {
        "keda-hpa-foo",
        "keda-hpa-foo-prefill",
        "keda-hpa-foo-decode",
    }.issubset(inventory.horizontal_pod_autoscalers)
    assert "keda-hpa-foo-v2" not in inventory.horizontal_pod_autoscalers


def test_active_cleanup_request_budget_is_explicit_and_bounded() -> None:
    monitor = _make_monitor()
    monitor.discovery_v1 = MagicMock()
    not_found = ApiException(status=404)
    for method_name in (
        "delete_namespaced_deployment",
        "read_namespaced_deployment",
    ):
        getattr(monitor.apps_v1, method_name).side_effect = not_found
    monitor.apps_v1.list_namespaced_replica_set.return_value = MagicMock(items=[])
    for method_name in (
        "delete_namespaced_service",
        "read_namespaced_service",
        "delete_namespaced_config_map",
        "read_namespaced_config_map",
        "read_namespaced_secret",
    ):
        getattr(monitor.core_v1, method_name).side_effect = not_found
    monitor.core_v1.list_namespaced_pod.return_value = MagicMock(items=[])
    monitor.core_v1.list_namespaced_endpoints.return_value = MagicMock(items=[])
    monitor.discovery_v1.list_namespaced_endpoint_slice.return_value = MagicMock(items=[])
    monitor.networking_v1.delete_namespaced_ingress.side_effect = not_found
    monitor.networking_v1.read_namespaced_ingress.side_effect = not_found
    hpa = MagicMock()
    hpa.delete_namespaced_horizontal_pod_autoscaler.side_effect = not_found
    hpa.read_namespaced_horizontal_pod_autoscaler.side_effect = not_found
    custom = MagicMock()
    custom.delete_namespaced_custom_object.side_effect = not_found
    custom.get_namespaced_custom_object.side_effect = not_found

    with (
        patch("gco.services.inference_monitor.client.AutoscalingV2Api", return_value=hpa),
        patch("gco.services.inference_monitor.client.CustomObjectsApi", return_value=custom),
    ):
        result = monitor._delete_resources(
            "ep",
            NAMESPACE,
            expected_lifecycle_id=LIFECYCLE_ID,
        )

    assert result.complete is True
    request_count = sum(
        len(api.method_calls)
        for api in (
            monitor.apps_v1,
            monitor.core_v1,
            monitor.networking_v1,
            monitor.discovery_v1,
            hpa,
            custom,
        )
    )
    # Read-before-delete avoids 27 unnecessary delete calls on an already
    # absent pass. The fixed 32 requests still cover every parent, generated
    # child, and legacy route exactly once; future expansion must update this
    # reviewed bound rather than introducing an unbounded hot loop.
    assert request_count == 32


class TestKubernetesLifecycleFencing:
    @staticmethod
    def _resource(
        *, lifecycle: str, region_generation: str, epoch: str, uid: str = "uid-1", rv: str = "7"
    ) -> SimpleNamespace:
        return SimpleNamespace(
            metadata=SimpleNamespace(
                annotations={
                    "gco.io/lifecycle-id": lifecycle,
                    "gco.io/region-generation": region_generation,
                    "gco.io/leader-epoch": epoch,
                },
                labels={"app": "ep", "project": "gco", "gco.io/type": "inference"},
                uid=uid,
                resource_version=rv,
            )
        )

    def test_stale_lifecycle_cannot_delete_same_name_replacement(self) -> None:
        monitor = _make_monitor()
        monitor._active_authority = ReconcileAuthority(
            endpoint_name="ep",
            lifecycle_id="life-1",
            region_generation="region-1",
            leader_epoch="epoch-1",
            deleting=True,
        )
        monitor.store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "lifecycle_id": "life-2",
            "desired_state": "running",
            "region_generations": {monitor.region: "region-2"},
        }
        replacement = self._resource(
            lifecycle="life-2",
            region_generation="region-2",
            epoch="epoch-2",
            uid="uid-2",
            rv="9",
        )
        delete = MagicMock()

        with pytest.raises(ReconcileFencedError, match="another endpoint lifecycle"):
            monitor._delete_and_confirm(
                kind="deployment",
                resource_name="ep",
                delete_call=delete,
                read_call=lambda: replacement,
                patch_metadata=None,
                pending=[],
                errors=[],
            )

        delete.assert_not_called()

    def test_current_authority_uid_deletes_previous_region_generation(self) -> None:
        monitor = _make_monitor()
        monitor._active_authority = ReconcileAuthority(
            endpoint_name="ep",
            lifecycle_id="life-2",
            region_generation="region-2",
            leader_epoch="epoch-2",
        )
        monitor.store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "lifecycle_id": "life-2",
            "desired_state": "running",
            "region_generations": {monitor.region: "region-2"},
        }
        previous = self._resource(
            lifecycle="life-2",
            region_generation="region-1",
            epoch="epoch-1",
        )
        delete = MagicMock()

        with pytest.raises(ReconcileFencedError, match="handoff deletion requested"):
            monitor._authorize_resource(
                previous,
                kind="deployment",
                resource_name="ep",
                patch_metadata=MagicMock(),
                read_resource=lambda: previous,
                delete_resource=delete,
            )

        options = delete.call_args.kwargs["body"]
        assert options.preconditions.uid == "uid-1"
        assert options.preconditions.resource_version == "7"

    def test_post_create_authority_loss_compensates_only_created_uid(self) -> None:
        monitor = _make_monitor()
        monitor._active_authority = ReconcileAuthority(
            endpoint_name="ep",
            lifecycle_id="life-1",
            region_generation="region-1",
            leader_epoch="epoch-1",
        )
        created = self._resource(
            lifecycle="life-1",
            region_generation="region-1",
            epoch="epoch-1",
        )
        monitor._assert_mutation_authority = MagicMock(  # type: ignore[method-assign]
            side_effect=ReconcileFencedError("authority changed")
        )
        delete = MagicMock()

        with pytest.raises(ReconcileFencedError, match="authority changed"):
            monitor._confirm_created_resource(
                kind="deployment",
                resource_name="ep",
                read_resource=lambda: created,
                delete_resource=delete,
            )

        options = delete.call_args.kwargs["body"]
        assert options.preconditions.uid == "uid-1"
        assert options.preconditions.resource_version == "7"

    def test_unannotated_legacy_route_is_patched_then_uid_deleted(self) -> None:
        monitor = _make_monitor()
        authority = ReconcileAuthority(
            endpoint_name="ep",
            lifecycle_id="life-1",
            region_generation="region-1",
            leader_epoch="epoch-1",
            deleting=True,
            deletion_generation="delete-1",
        )
        monitor._active_authority = authority
        monitor.store.get_endpoint.return_value = {
            "endpoint_name": "ep",
            "lifecycle_id": "life-1",
            "desired_state": "deleted",
            "deletion_generation": "delete-1",
            "region_generations": {monitor.region: "region-1"},
        }
        legacy = self._resource(
            lifecycle="life-1",
            region_generation="region-1",
            epoch="epoch-1",
        )
        legacy.metadata.annotations = {}
        claimed = self._resource(
            lifecycle="life-1",
            region_generation="region-1",
            epoch="epoch-1",
        )
        observations = iter((legacy, claimed, ApiException(status=404)))

        def read():
            value = next(observations)
            if isinstance(value, Exception):
                raise value
            return value

        patch_metadata = MagicMock()
        delete = MagicMock()
        assert monitor._delete_and_confirm(
            kind="ingress",
            resource_name="ep",
            delete_call=delete,
            read_call=read,
            patch_metadata=patch_metadata,
            pending=[],
            errors=[],
        )
        assert patch_metadata.call_args.kwargs["body"]["metadata"]["annotations"] == (
            authority.annotations
        )
        assert delete.call_args.kwargs["body"].preconditions.uid == "uid-1"

    def test_delete_is_bound_to_observed_uid_and_resource_version(self) -> None:
        monitor = _make_monitor()
        monitor._active_authority = ReconcileAuthority(
            endpoint_name="ep",
            lifecycle_id="life-1",
            region_generation="region-1",
            leader_epoch="epoch-1",
            deleting=True,
        )
        resource = self._resource(
            lifecycle="life-1",
            region_generation="region-1",
            epoch="epoch-1",
        )
        observations = iter((resource, ApiException(status=404)))

        def read():
            value = next(observations)
            if isinstance(value, Exception):
                raise value
            return value

        delete = MagicMock()
        assert monitor._delete_and_confirm(
            kind="deployment",
            resource_name="ep",
            delete_call=delete,
            read_call=read,
            patch_metadata=None,
            pending=[],
            errors=[],
        )
        options = delete.call_args.kwargs["body"]
        assert options.preconditions.uid == "uid-1"
        assert options.preconditions.resource_version == "7"

    def test_lost_lease_blocks_next_kubernetes_patch(self) -> None:
        monitor = _make_monitor()
        monitor._active_authority = ReconcileAuthority(
            endpoint_name="ep",
            lifecycle_id="life-1",
            region_generation="region-1",
            leader_epoch="epoch-1",
        )
        monitor._leadership_lost.set()
        monitor.apps_v1.read_namespaced_deployment.return_value = self._resource(
            lifecycle="life-1",
            region_generation="region-1",
            epoch="epoch-1",
        )

        with pytest.raises(ReconcileFencedError, match="Lease was lost"):
            monitor._scale_deployment("ep", NAMESPACE, 2)

        monitor.apps_v1.patch_namespaced_deployment.assert_not_called()

    def test_failed_background_renewal_sets_loss_fence(self) -> None:
        monitor = _make_monitor()
        waits = iter((False, True))
        stop_event = SimpleNamespace(wait=lambda _seconds: next(waits))
        monitor._renew_current_lease = MagicMock(return_value=False)  # type: ignore[method-assign]

        monitor._lease_renewal_loop(stop_event)  # type: ignore[arg-type]

        assert monitor._leadership_lost.is_set()
        monitor._renew_current_lease.assert_called_once()


def test_sglang_renderer_supplies_launcher_listener_and_model_from_env() -> None:
    """``--framework sglang`` renders the official launcher on the spec's port.

    The ``lmsysorg/sglang`` image has no entrypoint and the launcher binds
    127.0.0.1 by default, so the renderer must own the command, the listener
    address and port, and hand the ``MODEL`` convention to ``--model-path``.
    """
    monitor = _make_monitor()
    monitor._active_authority = ReconcileAuthority(
        endpoint_name="chat",
        lifecycle_id=LIFECYCLE_ID,
        region_generation=REGION_GENERATION,
        leader_epoch="epoch-1",
    )
    deployment = _build_deployment(
        monitor,
        {
            "image": "registry.example/team/private-server@sha256:" + "a" * 64,
            "framework": "sglang",
            "port": 30000,
            "health_check_path": "/health",
            "env": {"MODEL": "microsoft/Phi-3.5-mini-instruct"},
        },
    )

    container = deployment.spec.template.spec.containers[0]
    assert container.command == ["python3", "-m", "sglang.launch_server"]
    assert container.args == [
        "--host",
        "0.0.0.0",
        "--port",
        "30000",
        "--model-path",
        "$(MODEL)",
    ]
    assert container.ports[0].container_port == 30000
    assert container.startup_probe.http_get.path == "/health"
    assert container.startup_probe.http_get.port == 30000
    assert container.startup_probe.period_seconds == 15
    assert container.startup_probe.failure_threshold == 80
    assert container.readiness_probe.http_get.path == "/health"
    assert container.liveness_probe.http_get.path == "/health"
    env = {item.name: item.value for item in container.env}
    assert env["MODEL"] == "microsoft/Phi-3.5-mini-instruct"
    expected = {
        "gco.io/lifecycle-id": LIFECYCLE_ID,
        "gco.io/region-generation": REGION_GENERATION,
        "gco.io/leader-epoch": "epoch-1",
    }
    assert deployment.metadata.annotations == expected
    assert deployment.spec.template.metadata.annotations == expected


def test_sglang_renderer_never_duplicates_operator_supplied_launcher_flags() -> None:
    """Explicit ``--host``/``--port``/``--model-path`` args win over the injected defaults."""
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "port": 30000,
            "health_check_path": "/health",
            "env": {"MODEL": "test/model"},
            "args": [
                "--model-path",
                "test/model",
                "--revision",
                "b" * 40,
                "--host",
                "::",
                "--port",
                "30000",
            ],
        },
        extra_args=["--log-level", "warning"],
    )
    container = deployment.spec.template.spec.containers[0]
    assert container.command == ["python3", "-m", "sglang.launch_server"]
    assert container.args == [
        "--model-path",
        "test/model",
        "--revision",
        "b" * 40,
        "--host",
        "::",
        "--port",
        "30000",
        "--log-level",
        "warning",
    ]
    assert container.args.count("--host") == 1
    assert container.args.count("--port") == 1
    assert container.args.count("--model-path") == 1
    assert "$(MODEL)" not in container.args
    assert "--root-path" not in container.args


def test_sglang_renderer_accepts_the_short_model_alias_and_skips_env_injection() -> None:
    """``--model`` is SGLang's alias for ``--model-path``; no second model flag is added."""
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "port": 30000,
            "env": {"MODEL": "test/model"},
            "args": ["--model", "test/model"],
        },
    )
    container = deployment.spec.template.spec.containers[0]
    assert container.args == ["--model", "test/model", "--host", "0.0.0.0", "--port", "30000"]


def test_sglang_renderer_without_model_env_leaves_the_model_to_the_operator() -> None:
    """Without ``MODEL`` there is nothing to expand; the launcher gets only the listener."""
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "port": 30000,
            "env": {"HF_TOKEN": "x"},
        },
    )
    container = deployment.spec.template.spec.containers[0]
    assert container.command == ["python3", "-m", "sglang.launch_server"]
    assert container.args == ["--host", "0.0.0.0", "--port", "30000"]


def test_sglang_renderer_respects_an_operator_supplied_command() -> None:
    """A spec that carries its own ``command`` (raw manifest style) is rendered verbatim."""
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "port": 30000,
            "env": {"MODEL": "test/model"},
            "command": ["/bin/bash", "-c"],
            "args": ["python3 -m sglang.launch_server --model-path $MODEL --port 30000"],
        },
    )
    container = deployment.spec.template.spec.containers[0]
    assert container.command == ["/bin/bash", "-c"]
    assert container.args == ["python3 -m sglang.launch_server --model-path $MODEL --port 30000"]
    # The startup probe still follows the persisted framework.
    assert container.startup_probe is not None


def test_sglang_renderer_keeps_pods_off_pre_ampere_gpus() -> None:
    """SGLang pods carry a required NotIn affinity for GPUs its kernels do not target.

    The shipped inference NodePool's cheapest fit is a T4 (g4dn); SGLang's
    prebuilt sgl-kernel/FlashInfer binaries need compute capability >= 8.0 and
    crash-loop there ("no kernel image is available for execution on the
    device"). The affinity composes with the operator's node selector, which
    stays exactly as given.
    """
    deployment = _build_deployment(
        _make_monitor(),
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "port": 30000,
            "health_check_path": "/health",
            "env": {"MODEL": "Qwen/Qwen2.5-0.5B-Instruct"},
            "node_selector": {"eks.amazonaws.com/instance-family": "g6"},
        },
    )
    pod_spec = deployment.spec.template.spec
    assert pod_spec.node_selector == {"eks.amazonaws.com/instance-family": "g6"}
    terms = pod_spec.affinity.node_affinity.required_during_scheduling_ignored_during_execution
    (term,) = terms.node_selector_terms
    (requirement,) = term.match_expressions
    assert requirement.key == "eks.amazonaws.com/instance-gpu-name"
    assert requirement.operator == "NotIn"
    assert "t4" in requirement.values
    assert set(requirement.values) == {"k80", "m60", "v100", "t4"}
    # No preferred terms: an Ampere-or-newer GPU is a hard requirement.
    assert terms is not None
    assert (
        pod_spec.affinity.node_affinity.preferred_during_scheduling_ignored_during_execution is None
    )


@pytest.mark.parametrize(
    "spec",
    [
        # vLLM ships sm_70+ kernels: a T4 is a valid, cheaper placement.
        {
            "image": "vllm/vllm-openai:v0.11.0",
            "framework": "vllm",
            "port": 8000,
            "health_check_path": "/health",
            "env": {"MODEL": "facebook/opt-125m"},
        },
        # The GPU-name label is meaningless for Neuron devices.
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "accelerator": "neuron",
            "port": 30000,
            "health_check_path": "/health",
            "env": {"MODEL": "Qwen/Qwen2.5-0.5B-Instruct"},
        },
        # No accelerator requested at all: nothing to steer.
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "framework": "sglang",
            "gpu_count": 0,
            "port": 30000,
            "health_check_path": "/health",
            "env": {"MODEL": "Qwen/Qwen2.5-0.5B-Instruct"},
        },
    ],
    ids=["vllm", "sglang-neuron", "sglang-no-gpu"],
)
def test_pre_ampere_affinity_is_only_rendered_for_sglang_on_nvidia(spec: dict) -> None:
    deployment = _build_deployment(_make_monitor(), spec)
    assert deployment.spec.template.spec.affinity is None


def test_legacy_official_sglang_image_infers_the_sglang_contract() -> None:
    """Records written before ``framework`` existed resolve SGLang from the image name."""
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "lmsysorg/sglang:v0.5.19",
            "port": 30000,
            "health_check_path": "/health",
            "env": {"MODEL": "test/model"},
        },
    )
    container = deployment.spec.template.spec.containers[0]
    assert container.command == ["python3", "-m", "sglang.launch_server"]
    assert container.args == ["--host", "0.0.0.0", "--port", "30000", "--model-path", "$(MODEL)"]
    assert container.startup_probe is not None


def test_vllm_renderer_has_no_startup_probe_and_keeps_root_path() -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(
        monitor,
        {
            "image": "vllm/vllm-openai:v0.29.0",
            "framework": "vllm",
            "port": 8000,
            "health_check_path": "/health",
            "env": {"MODEL": "test/model"},
        },
    )
    container = deployment.spec.template.spec.containers[0]
    assert container.command is None
    assert container.args == ["--root-path", "/inference/chat"]
    assert container.startup_probe is None


@pytest.mark.parametrize(
    "spec",
    [
        # A record persisted under the retired TGI contract, and a plain
        # unknown image: neither gets a launcher, a root path, or the SGLang
        # startup probe. Such an endpoint has to be redeployed as sglang/vllm.
        {
            "image": "ghcr.io/huggingface/text-generation-inference@sha256:" + "a" * 64,
            "framework": "tgi",
            "port": 8080,
            "health_check_path": "/health",
            "env": {"MODEL_ID": "test/model", "PORT": "8080"},
        },
        {
            "image": "registry.example/team/private-server@sha256:" + "b" * 64,
            "port": 9000,
            "health_check_path": "/healthz",
            "env": {"MODEL": "test/model"},
        },
    ],
    ids=["retired-tgi-record", "unknown-image"],
)
def test_unrecognised_runtimes_get_no_adapter_behaviour(spec: dict) -> None:
    monitor = _make_monitor()
    deployment = _build_deployment(monitor, spec)
    container = deployment.spec.template.spec.containers[0]
    assert container.command is None
    assert container.args is None
    assert container.startup_probe is None
    assert container.ports[0].container_port == spec["port"]
    assert container.readiness_probe.http_get.path == spec["health_check_path"]
