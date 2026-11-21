"""The inference monitor's steady-state cost.

Every pass used to try to create each object a Mooncake endpoint needs and
take the 409 as "already there". Each attempt was a failed request preceded by
the mutation fence (a Lease read and a strongly consistent DynamoDB read), and
every typed API built without a client opened its own ``ApiClient``, a new
connection pool and TLS handshake to the API server. The monitor now reads
before it creates, so a pass over an endpoint whose objects all exist attempts
no create and fences only the writes it makes, and every typed API shares the
process's one :class:`~gco.services.k8s_client.BoundedApiClient`.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from kubernetes.client.rest import ApiException

import gco.services.inference_monitor as im
from gco.services.k8s_client import BoundedApiClient

REGION = "us-east-1"
NAMESPACE = "gco-inference"
_ANNOTATIONS = {
    im._LIFECYCLE_ANNOTATION: "L",
    im._REGION_GENERATION_ANNOTATION: "G",
    im._LEADER_EPOCH_ANNOTATION: "E",
}
_LABELS = {"project": "gco", "gco.io/type": "inference"}


def _live(**fields: Any) -> MagicMock:
    """An object of this endpoint's lifecycle, already claimed by the current leader."""
    obj = MagicMock()
    obj.metadata.annotations = dict(_ANNOTATIONS)
    obj.metadata.labels = {**_LABELS, im.ENDPOINT_NAME_LABEL: "ep"}
    obj.metadata.uid = "uid"
    obj.metadata.resource_version = "1"
    for name, value in fields.items():
        setattr(obj, name, value)
    return obj


def _steady_monitor() -> tuple[im.InferenceMonitor, dict[str, Any], MagicMock]:
    """A leader monitor over a split Mooncake endpoint whose every object exists."""
    with (
        patch.object(im.config, "load_incluster_config"),
        patch.object(im.client, "AppsV1Api"),
        patch.object(im.client, "CoreV1Api"),
        patch.object(im.client, "NetworkingV1Api"),
        patch.object(im.client, "DiscoveryV1Api"),
    ):
        endpoint = {
            "endpoint_name": "ep",
            "lifecycle_id": "L",
            "region_generations": {REGION: "G"},
            "desired_state": "running",
            "target_regions": [REGION],
            "cleanup_regions": [REGION],
            "updated_at": "t",
            "namespace": NAMESPACE,
            "spec": {
                "image": "img",
                "mooncake": {
                    "mode": "disaggregated",
                    "topology": {"prefill": 1, "decode": 1},
                    "proxy": {"image": "img"},
                },
            },
        }
        store = MagicMock()
        store.get_endpoint.return_value = endpoint
        monitor = im.InferenceMonitor("cluster", REGION, store)
    monitor._lease_name, monitor._lease_holder, monitor._leader_epoch = "lease", "me", "E"
    sidecar = im.build_endpoint_tls_proxy(8000, "ep-tls-proxy")

    def deployment(name: str, namespace: str, **_kwargs: Any) -> MagicMock:
        template = MagicMock()
        template.metadata.annotations = {im.ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION: sidecar.digest}
        model = MagicMock()
        model.name = "inference"
        model.ports = []
        tls = MagicMock()
        tls.name = im.ENDPOINT_TLS_PROXY_CONTAINER
        template.spec.containers = [model, tls]
        live = _live()
        live.spec.template = template
        live.spec.replicas = 1
        live.status.ready_replicas = 1
        return live

    apps, core, networking = monitor.apps_v1, monitor.core_v1, monitor.networking_v1
    apps.read_namespaced_deployment.side_effect = deployment
    port = MagicMock(port=8443, target_port="https", protocol="TCP")
    port.name = "https"
    core.read_namespaced_service.return_value = _live(spec=MagicMock(ports=[port]))
    script = (Path(im.__file__).parent / "tls_proxy.py").read_text(encoding="utf-8")
    core.read_namespaced_config_map.return_value = _live(data={"tls_proxy.py": script})
    secret = _live(type="Opaque", string_data=None, data={"ADMIN_API_KEY": "YWJj"})
    secret.metadata.labels = {"app": "ep-admin", **_LABELS}
    core.read_namespaced_secret.return_value = secret
    master = MagicMock()
    master.status.ready_replicas = 1
    apps.read_namespaced_stateful_set_status.return_value = master
    apps.read_namespaced_stateful_set.return_value = master
    networking.read_namespaced_network_policy.return_value = MagicMock()
    return monitor, endpoint, store


def _lease() -> MagicMock:
    lease = MagicMock()
    lease.spec.holder_identity = "me"
    lease.spec.renew_time = datetime.now(UTC)
    lease.spec.lease_duration_seconds = 45
    lease.metadata.annotations = {im._LEADER_EPOCH_ANNOTATION: "E"}
    return lease


def test_a_steady_pass_attempts_no_create_and_fences_only_its_writes() -> None:
    monitor, endpoint, store = _steady_monitor()
    missing = ApiException(status=404)
    with (
        patch.object(im.client, "CoordinationV1Api") as coordination,
        patch.object(im.client, "AutoscalingV2Api") as autoscaling,
        patch.object(im.client, "CustomObjectsApi") as custom,
    ):
        coordination.return_value.read_namespaced_lease.return_value = _lease()
        autoscaling.return_value.read_namespaced_horizontal_pod_autoscaler.side_effect = missing
        custom.return_value.get_namespaced_custom_object.side_effect = missing
        monitor._active_authority = None

        result = asyncio.run(monitor._reconcile_endpoint(endpoint))

    assert result == {"action": "reconcile_mooncake", "endpoint": "ep", "state": "running"}
    apis = (monitor.apps_v1, monitor.core_v1, monitor.networking_v1)
    creates = {
        name: getattr(api, name).call_count
        for api in apis
        for name in dir(api)
        if name.startswith("create_namespaced") and getattr(api, name).call_count
    }
    assert creates == {}
    patches = sum(
        getattr(api, name).call_count
        for api in apis
        for name in dir(api)
        if name.startswith("patch_namespaced")
    )
    # Each write is fenced by one strong read; reading an existing object adds none.
    assert patches > 0
    assert store.get_endpoint.call_count == patches
    # Every typed API shares the monitor's one client.
    for api_class in (coordination, autoscaling, custom):
        assert api_class.call_args_list
        assert all(made == call(monitor.api_client) for made in api_class.call_args_list)


def _monitor() -> im.InferenceMonitor:
    with (
        patch.object(im.config, "load_incluster_config"),
        patch.object(im.client, "AppsV1Api"),
        patch.object(im.client, "CoreV1Api"),
        patch.object(im.client, "NetworkingV1Api"),
        patch.object(im.client, "DiscoveryV1Api"),
    ):
        return im.InferenceMonitor("cluster", REGION, MagicMock())


def test_an_existing_object_is_returned_without_a_fence_or_create() -> None:
    monitor = _monitor()
    monitor._assert_mutation_authority = MagicMock()  # type: ignore[method-assign]
    existing, create = MagicMock(), MagicMock()

    assert monitor._read_or_create(lambda: existing, create) is existing

    create.assert_not_called()
    monitor._assert_mutation_authority.assert_not_called()


def test_a_missing_object_is_created_under_the_fence() -> None:
    monitor = _monitor()
    order: list[str] = []
    monitor._assert_mutation_authority = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda: order.append("fence")
    )
    read = MagicMock(side_effect=ApiException(status=404))

    assert monitor._read_or_create(read, lambda: order.append("create")) is None

    assert order == ["fence", "create"]


def test_a_create_that_loses_a_race_returns_the_winner() -> None:
    monitor = _monitor()
    monitor._assert_mutation_authority = MagicMock()  # type: ignore[method-assign]
    winner = MagicMock()
    read = MagicMock(side_effect=[ApiException(status=404), winner])
    create = MagicMock(side_effect=ApiException(status=409))

    assert monitor._read_or_create(read, create) is winner


@pytest.mark.parametrize(
    ("read_error", "create_error"),
    [(ApiException(status=500), None), (ApiException(status=404), ApiException(status=403))],
    ids=["read-fails", "create-fails"],
)
def test_other_api_errors_propagate(
    read_error: ApiException, create_error: ApiException | None
) -> None:
    monitor = _monitor()
    monitor._assert_mutation_authority = MagicMock()  # type: ignore[method-assign]
    create = MagicMock(side_effect=create_error)

    with pytest.raises(ApiException) as raised:
        monitor._read_or_create(MagicMock(side_effect=read_error), create)

    assert raised.value is (create_error or read_error)


def test_the_monitor_builds_one_bounded_client_for_every_typed_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("K8S_API_TIMEOUT", "12")
    with (
        patch.object(im.config, "load_incluster_config"),
        patch.object(im.client, "AppsV1Api") as apps,
        patch.object(im.client, "CoreV1Api") as core,
        patch.object(im.client, "NetworkingV1Api") as networking,
        patch.object(im.client, "DiscoveryV1Api") as discovery,
    ):
        monitor = im.InferenceMonitor("cluster", REGION, MagicMock())

    assert isinstance(monitor.api_client, BoundedApiClient)
    assert monitor.api_client.default_request_timeout == 12 == monitor._k8s_timeout
    for api_class in (apps, core, networking, discovery):
        api_class.assert_called_once_with(monitor.api_client)


def test_every_typed_api_in_the_monitor_is_built_on_the_shared_client() -> None:
    """A typed API built without a client opens its own connection pool per call."""
    source = Path(im.__file__).read_text(encoding="utf-8")
    unshared = [
        f"{node.lineno}:{node.func.attr}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
        and node.func.attr.endswith("Api")
        and [ast.unparse(arg) for arg in node.args] != ["self.api_client"]
    ]

    assert unshared == []


def test_a_pd_proxy_created_concurrently_is_converged_by_the_merge_patch() -> None:
    """The proxy read as absent, then another writer created it: patch, do not fail."""
    monitor = _monitor()
    monitor._assert_mutation_authority = MagicMock()  # type: ignore[method-assign]
    monitor._ensure_admin_api_key_secret = MagicMock(return_value="ep-admin")  # type: ignore[method-assign]
    monitor._ensure_pd_proxy_configmap = MagicMock()  # type: ignore[method-assign]
    monitor._create_proxy_service = MagicMock()  # type: ignore[method-assign]
    winner = _live()
    winner.spec.template.metadata.annotations = {}
    monitor._get_deployment = MagicMock(side_effect=[None, winner])  # type: ignore[method-assign]
    monitor.apps_v1.create_namespaced_deployment.side_effect = ApiException(status=409)

    monitor._create_pd_proxy(
        "ep", NAMESPACE, {"mooncake": {"proxy": {"image": "proxy:v1"}}}, {"lifecycle_id": "L"}
    )

    [patched] = monitor.apps_v1.patch_namespaced_deployment.call_args_list
    assert patched.args[:2] == ("ep-proxy", NAMESPACE)
    assert patched.kwargs["_content_type"] == "application/merge-patch+json"
    assert patched.kwargs["body"].metadata.resource_version == "1"
    monitor._create_proxy_service.assert_called_once_with("ep-proxy", NAMESPACE)
