"""Front-door routing for disaggregated prefill-decode endpoints.

A disaggregated (or ``both``-mode) endpoint is reached through a lightweight
proxy that runs the residency check and coordinates the prefill and decode
pods. Two pieces of that front door are pinned here:

- The proxy Service must resolve to the proxy pods alone. Its selector carries
  the ``{name}-proxy`` app label together with the proxy role marker, so it
  never fans traffic out to the prefill or decode role pods that share the
  namespace.
- The public Ingress must publish only the OpenAI-compatible serving paths. It
  routes the ``/v1`` prefix to the proxy Service and leaves the proxy's
  ``/instances/add`` admin path off the public surface entirely.

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
    """The public Ingress publishes the ``/v1`` serving prefix and nothing else.

    Exactly one routing path is configured: the ``/v1`` prefix pointing at the
    proxy Service. No rule exposes the proxy's ``/instances/add`` admin path.
    """
    from gco.services.inference_monitor import PD_PROXY_PUBLIC_PATH_PREFIX

    monitor.networking_v1.create_namespaced_ingress.reset_mock()
    monitor._update_proxy_ingress("my-endpoint", "my-endpoint-proxy", "gco-inference")

    args, _ = monitor.networking_v1.create_namespaced_ingress.call_args
    namespace, ingress = args[0], args[1]
    assert namespace == "gco-inference"

    # Gather every routing path across every rule.
    paths = [path for rule in ingress.spec.rules for path in rule.http.paths]

    assert len(paths) == 1
    only_path = paths[0]
    assert only_path.path == PD_PROXY_PUBLIC_PATH_PREFIX
    assert only_path.path == "/v1"
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

    # Ingress publishes only the /v1 prefix to the proxy Service.
    ing_args, _ = monitor.networking_v1.create_namespaced_ingress.call_args
    ingress = ing_args[1]
    paths = [path for rule in ingress.spec.rules for path in rule.http.paths]
    assert [p.path for p in paths] == [PD_PROXY_PUBLIC_PATH_PREFIX]
    assert paths[0].backend.service.name == "my-endpoint-proxy"
