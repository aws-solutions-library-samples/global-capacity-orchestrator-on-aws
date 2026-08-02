"""Focused behavior coverage for the inference monitor's orchestration branches."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from kubernetes.client.rest import ApiException

from gco.services.inference_monitor import (
    MOONCAKE_BOOTSTRAP_BASE_PORT,
    VLLM_MOONCAKE_BOOTSTRAP_PORT_ENV,
    AdminApiKeySecretError,
    InferenceMonitor,
    MasterReadinessGate,
    RegionalScopeResolution,
    RegionServicesResolution,
)

NAMESPACE = "gco-inference"
REGION_SERVICES = {
    "metadata_server": "http://mooncake-master:8080/metadata",
    "master_server_address": "mooncake-master:50051",
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
async def test_reconcile_purges_only_endpoints_deleted_in_every_target_region() -> None:
    store = MagicMock()
    eligible = {
        "endpoint_name": "eligible",
        "desired_state": "deleted",
        "target_regions": ["us-east-1", "eu-west-1"],
        "region_status": {
            "us-east-1": {"state": "deleted"},
            "eu-west-1": {"state": "deleted"},
        },
    }
    store.list_endpoints.return_value = [
        eligible,
        {
            "endpoint_name": "still-running",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "region_status": {"us-east-1": {"state": "deleted"}},
        },
        {
            "endpoint_name": "no-targets",
            "desired_state": "deleted",
            "target_regions": [],
            "region_status": {},
        },
        {
            "endpoint_name": "partially-deleted",
            "desired_state": "deleted",
            "target_regions": ["us-east-1", "eu-west-1"],
            "region_status": {
                "us-east-1": {"state": "deleted"},
                "eu-west-1": {"state": "stopping"},
            },
        },
        {
            "endpoint_name": "malformed-status",
            "desired_state": "deleted",
            "target_regions": ["us-east-1"],
            "region_status": {"us-east-1": "deleted"},
        },
    ]
    monitor = _make_monitor(store)

    with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
        actions = await monitor.reconcile()

    store.delete_endpoint.assert_called_once_with("eligible")
    assert actions == [{"action": "purge", "endpoint": "eligible"}]


@pytest.mark.asyncio
async def test_reconcile_keeps_fully_deleted_record_when_purge_fails() -> None:
    store = MagicMock()
    store.list_endpoints.return_value = [
        {
            "endpoint_name": "retry-later",
            "desired_state": "deleted",
            "target_regions": ["us-east-1"],
            "region_status": {"us-east-1": {"state": "deleted"}},
        }
    ]
    store.delete_endpoint.side_effect = RuntimeError("conditional delete failed")
    monitor = _make_monitor(store)

    with patch.object(monitor, "_reconcile_endpoint", AsyncMock(return_value=None)):
        actions = await monitor.reconcile()

    assert actions == []
    store.delete_endpoint.assert_called_once_with("retry-later")


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
    monitor._ensure_role_deployment = MagicMock()  # type: ignore[method-assign]
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

    monitor._resolve_region_services = MagicMock(side_effect=resolve)  # type: ignore[method-assign]
    monitor._resolve_regional_scope = MagicMock(side_effect=scope)  # type: ignore[method-assign]
    monitor._gate_on_mooncake_master = MagicMock(side_effect=gate)  # type: ignore[method-assign]
    monitor._ensure_mooncake_configmap = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *_args: events.append("config")
    )
    monitor._ensure_role_deployment = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _name, _ns, _spec, role: events.append(f"role:{role}")
    )
    monitor._create_role_hpa = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _name, _ns, _spec, role: events.append(f"autoscaler:{role}")
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
            "autoscaling": {"enabled": True},
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
async def test_reconcile_mooncake_reports_admin_key_failure_without_status_overwrite() -> None:
    store = MagicMock()
    monitor = _make_monitor(store)
    _admit_mooncake(monitor)
    monitor._ensure_mooncake_configmap = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_role_deployment = MagicMock()  # type: ignore[method-assign]
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
    monitor._ensure_role_deployment = MagicMock()  # type: ignore[method-assign]
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
