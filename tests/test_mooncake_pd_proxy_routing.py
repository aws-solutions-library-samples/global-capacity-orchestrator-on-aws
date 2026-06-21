"""Front-door routing for disaggregated prefill-decode endpoints.

A disaggregated (or ``both``-mode) endpoint is reached through a lightweight
proxy that runs the residency check and coordinates the prefill and decode
pods. Two pieces of that front door are pinned here:

- The proxy Service must resolve to the proxy pods alone. Its selector carries
  the ``{name}-proxy`` app label together with the proxy role marker, so it
  never fans traffic out to the prefill or decode role pods that share the
  namespace.
- The public Ingress must publish only the OpenAI-compatible serving paths. It
  routes the endpoint-scoped ``{ingress_path}/v1`` prefix to the proxy Service
  and leaves the proxy's ``/instances/add`` admin path off the public surface
  entirely.

These examples build a monitor with every Kubernetes client mocked, invoke the
proxy Service and Ingress creation directly, and inspect the objects handed to
the API.
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


def test_proxy_ingress_routes_only_v1_prefix_to_proxy(monitor):
    """The public Ingress publishes the endpoint-scoped ``/v1`` prefix only.

    Exactly one routing path is configured: the endpoint's own
    ``{ingress_path}/v1`` prefix (``/inference/my-endpoint/v1``) pointing at the
    proxy Service. Scoping to the endpoint prefix is what lets a client request
    to ``/inference/my-endpoint/v1/...`` reach the proxy on the shared ALB. No
    rule exposes the proxy's ``/instances/add`` admin path.
    """
    from gco.services.inference_monitor import PD_PROXY_PUBLIC_PATH_PREFIX

    monitor.networking_v1.create_namespaced_ingress.reset_mock()
    monitor._update_proxy_ingress("my-endpoint", "my-endpoint-proxy", "gco-inference", {})

    args, _ = monitor.networking_v1.create_namespaced_ingress.call_args
    namespace, ingress = args[0], args[1]
    assert namespace == "gco-inference"

    # Gather every routing path across every rule.
    paths = [path for rule in ingress.spec.rules for path in rule.http.paths]

    assert len(paths) == 1
    only_path = paths[0]
    # The published path is the endpoint's ingress prefix plus the serving
    # prefix — the same path a client (and `gco inference invoke`) targets.
    assert only_path.path == f"/inference/my-endpoint{PD_PROXY_PUBLIC_PATH_PREFIX}"
    assert only_path.path == "/inference/my-endpoint/v1"
    assert only_path.path_type == "Prefix"
    assert only_path.backend.service.name == "my-endpoint-proxy"

    # The admin path is never published on the public Ingress.
    rendered_paths = {path.path for path in paths}
    assert "/instances/add" not in rendered_paths
    assert all("/instances" not in path for path in rendered_paths)


def test_full_proxy_materialization_keeps_service_and_ingress_scoped(monitor):
    """Materializing the whole proxy front keeps the Service and Ingress scoped.

    Driving the full proxy creation path produces a Service whose selector is
    proxy-only and an Ingress whose sole public path is the ``/v1`` prefix.
    """
    from gco.services.inference_monitor import (
        PD_PROXY_PUBLIC_PATH_PREFIX,
        PD_PROXY_ROLE_LABEL,
    )

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

    monitor._create_pd_proxy("my-endpoint", "gco-inference", spec, {})

    # Service selector is proxy-only.
    svc_args, _ = monitor.core_v1.create_namespaced_service.call_args
    service = svc_args[1]
    assert service.spec.selector == {
        "app": "my-endpoint-proxy",
        "gco.io/role": PD_PROXY_ROLE_LABEL,
    }

    # Ingress publishes only the endpoint-scoped /v1 prefix to the proxy Service.
    ing_args, _ = monitor.networking_v1.create_namespaced_ingress.call_args
    ingress = ing_args[1]
    paths = [path for rule in ingress.spec.rules for path in rule.http.paths]
    assert [p.path for p in paths] == [f"/inference/my-endpoint{PD_PROXY_PUBLIC_PATH_PREFIX}"]
    assert paths[0].backend.service.name == "my-endpoint-proxy"


def test_role_service_resolves_only_that_role(monitor):
    """A role Service named ``{name}-{role}`` selects only that role's pods.

    The proxy addresses prefill and decode through these Services, so each must
    select exactly its own role's app label and expose the serving port.
    """
    monitor._create_role_service("ep", "gco-inference", "prefill", 8000)

    args, _ = monitor.core_v1.create_namespaced_service.call_args
    svc = args[1]
    assert svc.metadata.name == "ep-prefill"
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
