"""Front-door routing for disaggregated prefill-decode endpoints.

A disaggregated (or ``both``-mode) endpoint is reached through a lightweight
proxy that runs the residency check and coordinates the prefill and decode
pods. The shared ``gco-system/gco-gateway`` HTTPRoute sends ``/inference`` to
``gco-system/inference-proxy``; that authenticated platform proxy then reaches
the endpoint's internal ClusterIP Service. The endpoint front has two enforced
boundaries:

- The proxy Service resolves to proxy pods alone. Its selector carries the
  ``{name}-proxy`` app label and proxy role marker, so it never fans traffic out
  to prefill or decode role pods in the same namespace.
- There is no endpoint-specific Ingress, Gateway, or HTTPRoute. Reconciliation
  only removes the two historical direct-Ingress names.

These examples inspect the ClusterIP Services and cleanup calls emitted by the
monitor; the shared platform Gateway API resources are outside endpoint
reconciliation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_monitor(region: str = "us-east-1"):
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


@pytest.fixture
def monitor():
    return _make_monitor()


def test_proxy_service_selects_only_proxy_pods(monitor):
    """The proxy Service resolves to proxy pods and to nothing else.

    The created Service carries a selector of exactly the ``{name}-proxy`` app
    label and the proxy role marker. The prefill and decode pods carry a
    different app label and a different role marker, so neither matches this
    selector.
    """
    from gco.services.inference_monitor import PD_PROXY_ROLE_LABEL

    monitor._create_proxy_service("my-endpoint-proxy", "gco-inference")

    args, _ = monitor.core_v1.create_namespaced_service.call_args
    namespace, service = args[0], args[1]
    assert namespace == "gco-inference"

    assert service.spec.type == "ClusterIP"
    selector = service.spec.selector
    assert selector == {
        "app": "my-endpoint-proxy",
        "gco.io/role": PD_PROXY_ROLE_LABEL,
    }

    # The role pods materialized for the same endpoint carry their own app
    # label ({name}-prefill / {name}-decode) and role marker. A selector that
    # demands the proxy app label plus the proxy role cannot match them.
    prefill_pod_labels = {"app": "my-endpoint-prefill", "gco.io/role": "prefill"}
    decode_pod_labels = {"app": "my-endpoint-decode", "gco.io/role": "decode"}
    for pod_labels in (prefill_pod_labels, decode_pod_labels):
        assert any(pod_labels.get(key) != value for key, value in selector.items()), (
            f"proxy selector must not match {pod_labels}"
        )

    # The single public port forwards to the proxy container port.
    assert len(service.spec.ports) == 1


def test_endpoint_services_do_not_create_endpoint_routes(monitor):
    """Endpoint fronts stay ClusterIP-only and leave shared routing untouched."""
    with patch("gco.services.inference_monitor.client.CustomObjectsApi") as custom_api:
        monitor._create_service("plain", "gco-inference", {"port": 8000})
        monitor._create_proxy_service("split-proxy", "gco-inference")
        monitor._create_role_service("split", "gco-inference", "prefill", 8000)

    services = [call.args[1] for call in monitor.core_v1.create_namespaced_service.call_args_list]
    assert {service.metadata.name for service in services} == {
        "plain",
        "split-proxy",
        "split-prefill",
    }
    assert all(service.spec.type == "ClusterIP" for service in services)

    # ``gco-system/gco-gateway`` and its shared ``/inference`` HTTPRoute are
    # platform resources; endpoint reconciliation creates neither Gateway API
    # objects nor a replacement Ingress.
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()
    monitor.networking_v1.patch_namespaced_ingress.assert_not_called()
    custom_api.return_value.create_namespaced_custom_object.assert_not_called()


def test_full_proxy_materialization_keeps_internal_service_scoped(monitor):
    """The proxy stays ClusterIP-only and legacy direct routes are removed."""
    from gco.services.inference_monitor import PD_PROXY_ROLE_LABEL

    spec = {
        "mooncake": {
            "mode": "disaggregated",
            "proxy": {
                "image": "gco/proxy:pinned",
                "replicas": 2,
                "admin_api_key_secret": "my-endpoint-admin",
            },
        }
    }

    # The admin key Secret resolves with a non-empty key so the proxy front is
    # allowed to materialize.
    admin_secret = MagicMock()
    admin_secret.string_data = None
    admin_secret.data = {"ADMIN_API_KEY": "c2VjcmV0"}  # base64 of "secret"
    monitor.core_v1.read_namespaced_secret.return_value = admin_secret

    with patch("gco.services.inference_monitor.client.CustomObjectsApi") as custom_api:
        monitor._create_pd_proxy("my-endpoint", "gco-inference", spec, {})

    # The proxy is reachable only as an in-cluster Service.
    svc_args, _ = monitor.core_v1.create_namespaced_service.call_args
    service = svc_args[1]
    assert service.spec.type == "ClusterIP"
    assert service.spec.selector == {
        "app": "my-endpoint-proxy",
        "gco.io/role": PD_PROXY_ROLE_LABEL,
    }

    # Reconciliation cannot create a bypass route and removes both names used
    # by older releases.
    monitor.networking_v1.create_namespaced_ingress.assert_not_called()
    monitor.networking_v1.patch_namespaced_ingress.assert_not_called()
    custom_api.return_value.create_namespaced_custom_object.assert_not_called()
    deleted = [
        call.args[:2] for call in monitor.networking_v1.delete_namespaced_ingress.call_args_list
    ]
    assert deleted == [
        ("inference-my-endpoint", "gco-inference"),
        ("inference-my-endpoint-proxy", "gco-inference"),
    ]


def test_role_service_resolves_only_that_role(monitor):
    """A role Service named ``{name}-{role}`` selects only that role's pods.

    The proxy addresses prefill and decode through these Services, so each must
    select exactly its own role's app label and expose the serving port.
    """
    monitor._create_role_service("ep", "gco-inference", "prefill", 8000)

    args, _ = monitor.core_v1.create_namespaced_service.call_args
    svc = args[1]
    assert svc.metadata.name == "ep-prefill"
    assert svc.spec.type == "ClusterIP"
    assert svc.spec.selector == {"app": "ep-prefill"}
    assert svc.spec.ports[0].port == 8000
    assert svc.spec.ports[0].target_port == 8000


def test_proxy_runs_the_router_script_against_role_services(monitor):
    """The proxy pod runs the bundled router and targets the role Services.

    The proxy container is launched with ``python <script>`` (not the vLLM image
    default entrypoint), the router program is shipped as a ConfigMap mounted
    into the pod, and the prefill/decode backend URLs point at the per-role
    Services.
    """
    from gco.services.inference_monitor import (
        PD_PROXY_SCRIPT_FILENAME,
        PD_PROXY_SCRIPT_PATH,
    )

    spec = {"mooncake": {"mode": "disaggregated", "proxy": {"image": "vllm/vllm-openai:v0.23.0"}}}
    monitor._create_pd_proxy("ep", "gco-inference", spec, {})

    # The router program is published as a ConfigMap carrying the real script.
    cm_args, _ = monitor.core_v1.create_namespaced_config_map.call_args
    cm = cm_args[1]
    assert cm.metadata.name == "ep-pd-proxy"
    assert PD_PROXY_SCRIPT_FILENAME in cm.data
    assert "FastAPI" in cm.data[PD_PROXY_SCRIPT_FILENAME]

    # The proxy container runs that script, not the image's default server.
    dep_args, _ = monitor.apps_v1.create_namespaced_deployment.call_args
    pod_spec = dep_args[1].spec.template.spec
    container = pod_spec.containers[0]
    assert container.command == ["python3", PD_PROXY_SCRIPT_PATH]

    env = {e.name: e.value for e in (container.env or []) if e.value is not None}
    assert env["PD_PROXY_PREFILL_URL"] == "http://ep-prefill:8000"
    assert env["PD_PROXY_DECODE_URL"] == "http://ep-decode:8000"

    # The ConfigMap is mounted so the script is present at the run path.
    assert any(m.mount_path == "/etc/pd-proxy" for m in (container.volume_mounts or []))
    assert any(
        v.config_map is not None and v.config_map.name == "ep-pd-proxy"
        for v in (pod_spec.volumes or [])
    )


def test_disaggregated_reconcile_creates_prefill_and_decode_services(monitor):
    """Reconciling a disaggregated endpoint lays down both role Services.

    Without the per-role Services the proxy would have no stable backends to
    route to, so the reconcile path must create one Service per role.
    """
    import asyncio

    monitor._resolve_region_services = lambda *a, **k: __import__(
        "gco.services.inference_monitor", fromlist=["RegionServicesResolution"]
    ).RegionServicesResolution(region_services={"metadata_server": "http://m:8080/metadata"})
    monitor._resolve_regional_scope = lambda *a, **k: __import__(
        "gco.services.inference_monitor", fromlist=["RegionalScopeResolution"]
    ).RegionalScopeResolution(in_region=True)
    monitor._gate_on_mooncake_master = lambda *a, **k: __import__(
        "gco.services.inference_monitor", fromlist=["MasterReadinessGate"]
    ).MasterReadinessGate(proceed=True)
    monitor._ensure_mooncake_configmap = lambda *a, **k: None
    monitor._ensure_role_deployment = lambda *a, **k: None
    monitor._create_pd_proxy = lambda *a, **k: None
    monitor._report_role_status = lambda *a, **k: "creating"

    created = []
    monitor._create_role_service = lambda name, ns, role, port=8000: created.append(role)

    spec = {"mooncake": {"mode": "disaggregated", "topology": {"prefill": 1, "decode": 1}}}
    asyncio.run(monitor._reconcile_mooncake("ep", "gco-inference", spec, {}))

    assert set(created) == {"prefill", "decode"}
