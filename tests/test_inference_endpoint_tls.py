"""In-cluster TLS for managed model pods: the ``endpoint-tls-proxy`` contract.

The inference monitor puts every managed model pod behind a TLS sidecar: the
classic Deployment, its canary, the prefill/decode (or store ``single``) role
Deployments, and the prefill-decode (PD) proxy. Each sidecar runs the
stdlib-only ``gco/services/tls_proxy.py`` source, shipped in the endpoint's
``{name}-tls-proxy`` ConfigMap, on a pinned multi-arch Python image; it serves
the ``gco-inference-tls`` wildcard certificate on 8443 and relays to the pod's
own server over loopback. Model Services publish only that port, the PD router
binds loopback and dials the role Services over verified HTTPS trusting only
the CA certificate projected on its own, and endpoints created before the
sidecar existed are moved onto it in place.

These checks drive the monitor's real render and reconcile methods against
mocked Kubernetes clients and inspect the objects and patch bodies it emits,
serialized the way the Kubernetes client puts them on the wire. The PD
router's exec probe is executed for real against loopback HTTP servers.
"""

from __future__ import annotations

import ast
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

import gco.services.inference_monitor as inference_monitor
from gco.services.inference_monitor import (
    EFA_RESOURCE_NAME,
    ENDPOINT_TLS_MOUNT_DIR,
    ENDPOINT_TLS_PORT,
    ENDPOINT_TLS_PORT_NAME,
    ENDPOINT_TLS_PROXY_CONFIG_MOUNT_DIR,
    ENDPOINT_TLS_PROXY_CONTAINER,
    ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION,
    ENDPOINT_TLS_PROXY_IMAGE,
    ENDPOINT_TLS_PROXY_IMAGE_ENV,
    ENDPOINT_TLS_PROXY_SCRIPT_FILENAME,
    ENDPOINT_TLS_PROXY_SCRIPT_PATH,
    ENDPOINT_TLS_PROXY_SCRIPT_VOLUME,
    ENDPOINT_TLS_SECRET,
    ENDPOINT_TLS_VOLUME,
    INTERNAL_CA_KEY,
    INTERNAL_CA_VOLUME,
    MODEL_POD_TERMINATION_GRACE_SECONDS,
    PD_PROXY_CA_FILE,
    PD_PROXY_PORT,
    SKIP_CONTAINERS_ANNOTATION,
    InferenceMonitor,
    ReconcileFencedError,
    ResourceCleanupResult,
    _merge_skip_containers,
    _model_server_port,
    _pd_proxy_health_probe,
    _service_publishes_only_tls,
    build_endpoint_tls_proxy,
)
from gco.services.internal_tls import DEFAULT_INTERNAL_CA_FILE

NAMESPACE = "gco-inference"
REGION = "us-east-1"
MODEL_IMAGE = "vllm/vllm-openai:v0.6.0"
ROUTER_IMAGE = "vllm/vllm-openai:v0.23.0"
TLS_PROXY_CONFIG_MAP = "chat-tls-proxy"
ADMIN_KEY_SECRET_NAME = "chat-admin"  # nosec B105  # the Secret's name, not a key value

# The program the sidecar runs, exactly as it exists in this checkout.
TLS_PROXY_SOURCE_FILE = Path(__file__).resolve().parents[1] / "gco" / "services" / "tls_proxy.py"

# The one port every model Service publishes, as the client sends it.
TLS_SERVICE_PORTS_WIRE = [{"name": "https", "port": 8443, "protocol": "TCP", "targetPort": "https"}]

_API_CLIENT = client.ApiClient()


def _wire(value: Any) -> Any:
    """Serialize client models (or dicts holding them) exactly as the client sends them."""
    return _API_CLIENT.sanitize_for_serialization(value)


def _tls_proxy_source() -> str:
    return TLS_PROXY_SOURCE_FILE.read_text(encoding="utf-8")


def _make_monitor() -> InferenceMonitor:
    """Build a monitor whose Kubernetes clients are all mocks."""
    with (
        patch("gco.services.inference_monitor.config.load_incluster_config"),
        patch("gco.services.inference_monitor.client.AppsV1Api") as apps,
        patch("gco.services.inference_monitor.client.CoreV1Api") as core,
        patch("gco.services.inference_monitor.client.NetworkingV1Api") as networking,
        patch("gco.services.inference_monitor.client.AutoscalingV2Api"),
    ):
        monitor = InferenceMonitor(
            cluster_id="test-cluster",
            region=REGION,
            store=MagicMock(),
            namespace=NAMESPACE,
            reconcile_interval=5,
        )
    monitor.apps_v1 = apps.return_value
    monitor.core_v1 = core.return_value
    monitor.networking_v1 = networking.return_value
    return monitor


@pytest.fixture
def monitor() -> InferenceMonitor:
    return _make_monitor()


@pytest.fixture(autouse=True)
def _pinned_sidecar_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render with the pinned default image unless a test sets the override itself."""
    monkeypatch.delenv(ENDPOINT_TLS_PROXY_IMAGE_ENV, raising=False)


def _by_name(objects: Mapping[str, Any]) -> Callable[..., Any]:
    """A namespaced ``read_*`` stand-in that serves ``objects`` and 404s the rest."""

    def _read(name: str, namespace: str, **_kwargs: Any) -> Any:
        assert namespace == NAMESPACE
        if name in objects:
            return objects[name]
        raise ApiException(status=404)

    return _read


def _model_container(port: int = 8000, *, image: str = MODEL_IMAGE) -> client.V1Container:
    return client.V1Container(
        name="inference",
        image=image,
        ports=[client.V1ContainerPort(container_port=port)],
    )


def _live_deployment(
    name: str,
    *,
    containers: list[client.V1Container] | None = None,
    annotations: dict[str, str] | None = None,
    resource_version: str | None = "7",
    replicas: int = 1,
    ready_replicas: int = 1,
) -> client.V1Deployment:
    """A Deployment as the API returns it; by default one that predates the sidecar."""
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name, namespace=NAMESPACE, resource_version=resource_version
        ),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}, annotations=annotations),
                spec=client.V1PodSpec(containers=containers or [_model_container()]),
            ),
        ),
        status=client.V1DeploymentStatus(replicas=replicas, ready_replicas=ready_replicas),
    )


def _legacy_ports() -> list[client.V1ServicePort]:
    """The plaintext port a Service created before the sidecar publishes."""
    return [client.V1ServicePort(port=80, target_port=8000, protocol="TCP")]


def _tls_ports(protocol: str | None = "TCP") -> list[client.V1ServicePort]:
    return [client.V1ServicePort(name="https", port=8443, target_port="https", protocol=protocol)]


def _live_service(
    name: str,
    *,
    ports: list[client.V1ServicePort] | None,
    resource_version: str | None = "41",
) -> client.V1Service:
    return client.V1Service(
        metadata=client.V1ObjectMeta(
            name=name, namespace=NAMESPACE, resource_version=resource_version
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={"app": name, "gco.io/type": "inference"},
            ports=ports,
        ),
    )


def _live_config_map(
    data: dict[str, str] | None, *, resource_version: str = "12"
) -> client.V1ConfigMap:
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=TLS_PROXY_CONFIG_MAP, namespace=NAMESPACE, resource_version=resource_version
        ),
        data=data,
    )


def _pd_spec() -> dict[str, Any]:
    return {
        "image": MODEL_IMAGE,
        "mooncake": {
            "mode": "disaggregated",
            "proxy": {"image": ROUTER_IMAGE, "admin_api_key_secret": ADMIN_KEY_SECRET_NAME},
        },
    }


def _admit_named_admin_secret(monitor: InferenceMonitor) -> None:
    """Resolve the user-named admin key Secret so the PD proxy may materialize."""
    monitor.core_v1.read_namespaced_secret.return_value = client.V1Secret(
        string_data={"ADMIN_API_KEY": "router-key"}
    )


def _env(container: client.V1Container) -> dict[str, str]:
    return {item.name: item.value for item in container.env or [] if item.value is not None}


def _created_deployment(
    monitor: InferenceMonitor, name: str, namespace: str = NAMESPACE
) -> client.V1Deployment:
    for created in monitor.apps_v1.create_namespaced_deployment.call_args_list:
        created_namespace, body = created.args[:2]
        if body.metadata.name == name:
            assert created_namespace == namespace
            return body
    raise AssertionError(f"deployment {name} was not created")


def _deployment_patch(monitor: InferenceMonitor, name: str) -> dict[str, Any]:
    """Return the single sidecar patch sent for ``name``, in wire form."""
    [patch_call] = [
        call
        for call in monitor.apps_v1.patch_namespaced_deployment.call_args_list
        if call.args[0] == name
    ]
    assert patch_call.args[1] == NAMESPACE
    # No explicit content type: the client sends a dict body as a strategic
    # merge patch, which adds the sidecar and its volumes by name and leaves
    # the model container exactly as reconciled.
    assert "_content_type" not in patch_call.kwargs
    body: dict[str, Any] = _wire(patch_call.kwargs["body"])
    return body


def _assert_moved_to_tls(
    monitor: InferenceMonitor, name: str, *, resource_version: str | None
) -> None:
    """The Service was patched in place onto the sidecar port, and only that."""
    monitor.core_v1.patch_namespaced_service.assert_called_once()
    patch_call = monitor.core_v1.patch_namespaced_service.call_args
    assert patch_call.args == (name, NAMESPACE)
    # A JSON merge patch replaces the whole ports list, so the plaintext port
    # cannot survive beside the TLS one (and the ClusterIP never changes).
    assert patch_call.kwargs["_content_type"] == "application/merge-patch+json"
    expected: dict[str, Any] = {"spec": {"ports": TLS_SERVICE_PORTS_WIRE}}
    if resource_version is not None:
        expected["metadata"] = {"resourceVersion": resource_version}
    assert _wire(patch_call.kwargs["body"]) == expected


def _assert_fronted_by_tls_sidecar(
    template: client.V1PodTemplateSpec, *, upstream_port: int, config_map: str
) -> client.V1Container:
    """Check one model pod template carries the complete TLS sidecar contract."""
    pod_spec = template.spec
    names = [container.name for container in pod_spec.containers]
    # The workload stays first (image reconciliation reads containers[0]) and
    # exactly one sidecar follows it.
    assert names[0] != ENDPOINT_TLS_PROXY_CONTAINER
    assert names[-1] == ENDPOINT_TLS_PROXY_CONTAINER
    assert names.count(ENDPOINT_TLS_PROXY_CONTAINER) == 1
    sidecar = pod_spec.containers[-1]

    assert sidecar.image == ENDPOINT_TLS_PROXY_IMAGE
    assert sidecar.command == ["python3", ENDPOINT_TLS_PROXY_SCRIPT_PATH]
    env = _env(sidecar)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["TLS_PROXY_PORT"] == "8443"
    assert env["TLS_PROXY_UPSTREAM_HOST"] == "127.0.0.1"
    assert env["TLS_PROXY_UPSTREAM_PORT"] == str(upstream_port)
    assert env["GCO_TLS_CERT_FILE"] == f"{ENDPOINT_TLS_MOUNT_DIR}/tls.crt"
    assert env["GCO_TLS_KEY_FILE"] == f"{ENDPOINT_TLS_MOUNT_DIR}/tls.key"
    # The sidecar drains accepted streams, then exits before the kubelet's SIGKILL.
    assert pod_spec.termination_grace_period_seconds == MODEL_POD_TERMINATION_GRACE_SECONDS
    assert 0 < int(env["GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"]) < MODEL_POD_TERMINATION_GRACE_SECONDS

    [port] = sidecar.ports
    assert (port.name, port.container_port, port.protocol) == ("https", 8443, "TCP")

    security = sidecar.security_context
    assert security.run_as_non_root is True
    assert security.run_as_user not in (None, 0)
    assert security.read_only_root_filesystem is True
    assert security.allow_privilege_escalation is False
    assert security.privileged is False
    assert security.capabilities.drop == ["ALL"]
    assert not security.capabilities.add
    assert security.seccomp_profile.type == "RuntimeDefault"

    # Socket probes against the sidecar's own listener only: a slow model
    # never restarts the sidecar, and a sidecar fault never restarts the model.
    for probe in (sidecar.readiness_probe, sidecar.liveness_probe):
        assert probe.tcp_socket.port == ENDPOINT_TLS_PORT_NAME
        assert probe.http_get is None
        assert probe._exec is None

    # A request on every container keeps autoscaler Resource metrics defined;
    # memory is bounded, and no CPU limit means no CFS throttling of tokens.
    assert set(sidecar.resources.requests) == {"cpu", "memory"}
    assert set(sidecar.resources.limits) == {"memory"}

    mounts = {mount.name: mount for mount in sidecar.volume_mounts}
    assert set(mounts) == {ENDPOINT_TLS_VOLUME, ENDPOINT_TLS_PROXY_SCRIPT_VOLUME}
    assert mounts[ENDPOINT_TLS_VOLUME].mount_path == ENDPOINT_TLS_MOUNT_DIR
    assert (
        mounts[ENDPOINT_TLS_PROXY_SCRIPT_VOLUME].mount_path == ENDPOINT_TLS_PROXY_CONFIG_MOUNT_DIR
    )
    assert all(mount.read_only for mount in mounts.values())

    volumes = {volume.name: volume for volume in pod_spec.volumes}
    keypair = volumes[ENDPOINT_TLS_VOLUME].secret
    assert keypair.secret_name == ENDPOINT_TLS_SECRET
    assert keypair.items is None  # the sidecar needs both tls.crt and tls.key
    # World-readable: model pods carry no fsGroup, and GCO does not own the
    # model image's users.
    assert keypair.default_mode == 0o444
    program = volumes[ENDPOINT_TLS_PROXY_SCRIPT_VOLUME].config_map
    assert program.name == config_map
    assert program.default_mode == 0o444

    # Only the sidecar ever mounts the private key.
    for container in pod_spec.containers[:-1]:
        assert ENDPOINT_TLS_VOLUME not in {mount.name for mount in container.volume_mounts or []}

    annotations = template.metadata.annotations
    assert ENDPOINT_TLS_PROXY_CONTAINER in annotations[SKIP_CONTAINERS_ANNOTATION].split(",")
    assert annotations[ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION] == (
        build_endpoint_tls_proxy(upstream_port, config_map).digest
    )
    return sidecar


# ---------------------------------------------------------------------------
# Every managed model pod kind is fronted by the sidecar
# ---------------------------------------------------------------------------


def test_classic_endpoint_pod_is_fronted_by_the_sidecar(monitor: InferenceMonitor) -> None:
    monitor._create_deployment("chat", NAMESPACE, {"image": MODEL_IMAGE, "port": 8080})

    template = _created_deployment(monitor, "chat").spec.template
    _assert_fronted_by_tls_sidecar(template, upstream_port=8080, config_map=TLS_PROXY_CONFIG_MAP)
    # The model server keeps its own port and HTTP probes; only the sidecar is new.
    model = template.spec.containers[0]
    assert model.name == "inference"
    assert [port.container_port for port in model.ports] == [8080]
    assert model.readiness_probe.http_get.port == 8080
    assert model.liveness_probe.http_get.port == 8080


def test_canary_pod_is_fronted_by_its_endpoint_program(monitor: InferenceMonitor) -> None:
    monitor.apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404)
    canary = {"image": "vllm/vllm-openai:v0.7.0", "weight": 10, "replicas": 1}
    spec = {"image": MODEL_IMAGE, "port": 8000, "replicas": 2, "canary": canary}

    status = monitor._reconcile_canary("chat", NAMESPACE, spec, canary, {})

    assert status["state"] == "creating"
    template = _created_deployment(monitor, "chat-canary").spec.template
    _assert_fronted_by_tls_sidecar(template, upstream_port=8000, config_map=TLS_PROXY_CONFIG_MAP)
    # The canary runs its endpoint's program: it has no ConfigMap of its own
    # to create or clean up.
    monitor.core_v1.create_namespaced_config_map.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "role", "deploy_name"),
    [
        ("disaggregated", "prefill", "chat-prefill"),
        ("disaggregated", "decode", "chat-decode"),
        ("both", "prefill", "chat-prefill"),
        ("both", "decode", "chat-decode"),
        ("store", "single", "chat"),
    ],
)
def test_role_pods_are_fronted_by_the_sidecar(
    monitor: InferenceMonitor, mode: str, role: str, deploy_name: str
) -> None:
    spec = {
        "image": MODEL_IMAGE,
        "port": 8100,
        "mooncake": {"mode": mode, "transfer": {"protocol": "rdma"}},
    }

    monitor._create_role_deployment("chat", NAMESPACE, spec, role)

    template = _created_deployment(monitor, deploy_name).spec.template
    assert template.metadata.labels["gco.io/role"] == role
    sidecar = _assert_fronted_by_tls_sidecar(
        template, upstream_port=8100, config_map=TLS_PROXY_CONFIG_MAP
    )
    # RDMA transfer lands the pod on EFA: the model container takes the
    # device; the loopback-only sidecar never does.
    model = template.spec.containers[0]
    assert model.resources.requests[EFA_RESOURCE_NAME] == "1"
    assert EFA_RESOURCE_NAME not in sidecar.resources.requests
    assert EFA_RESOURCE_NAME not in sidecar.resources.limits


def test_pd_proxy_pod_is_fronted_by_the_sidecar(monitor: InferenceMonitor) -> None:
    _admit_named_admin_secret(monitor)

    monitor._create_pd_proxy("chat", NAMESPACE, _pd_spec(), {"lifecycle_id": "life-1"})

    template = _created_deployment(monitor, "chat-proxy").spec.template
    _assert_fronted_by_tls_sidecar(
        template, upstream_port=PD_PROXY_PORT, config_map=TLS_PROXY_CONFIG_MAP
    )


# ---------------------------------------------------------------------------
# The PD router: loopback listener, verified HTTPS backends, CA-only trust
# ---------------------------------------------------------------------------


def test_pd_router_binds_loopback_and_trusts_only_the_projected_ca(
    monitor: InferenceMonitor,
) -> None:
    _admit_named_admin_secret(monitor)

    monitor._create_pd_proxy("chat", "team-a", _pd_spec(), {"lifecycle_id": "life-1"})

    pod_spec = _created_deployment(monitor, "chat-proxy", namespace="team-a").spec.template.spec
    [router] = [container for container in pod_spec.containers if container.name == "proxy"]
    env = _env(router)
    assert env["PD_PROXY_HOST"] == "127.0.0.1"
    assert env["PD_PROXY_PORT"] == str(PD_PROXY_PORT)
    # Both backends are the role Services' sidecars, by the fully qualified
    # name the wildcard certificate covers, in the endpoint's own namespace.
    for role in ("prefill", "decode"):
        url = urlsplit(env[f"PD_PROXY_{role.upper()}_URL"])
        assert (url.scheme, url.hostname, url.port, url.path) == (
            "https",
            f"chat-{role}.team-a.svc.cluster.local",
            ENDPOINT_TLS_PORT,
            "",
        )
    # The router's CA path is the convention every in-cluster GCO client uses.
    assert env["PD_PROXY_CA_FILE"] == PD_PROXY_CA_FILE == DEFAULT_INTERNAL_CA_FILE

    volumes = {volume.name: volume for volume in pod_spec.volumes}
    mounts = {mount.name: mount for mount in router.volume_mounts}
    assert ENDPOINT_TLS_VOLUME not in mounts
    ca_mount = mounts[INTERNAL_CA_VOLUME]
    assert ca_mount.read_only is True
    assert f"{ca_mount.mount_path}/{INTERNAL_CA_KEY}" == env["PD_PROXY_CA_FILE"]
    ca_source = volumes[INTERNAL_CA_VOLUME].secret
    assert ca_source.secret_name == ENDPOINT_TLS_SECRET
    assert [(item.key, item.path) for item in ca_source.items] == [("ca.crt", "ca.crt")]
    assert ca_source.default_mode == 0o444
    # No Secret volume the router mounts can surface the private key.
    for mount in router.volume_mounts:
        secret = volumes[mount.name].secret
        if secret is not None:
            assert {item.key for item in secret.items or []} == {INTERNAL_CA_KEY}

    # The kubelet dials probes at the pod IP, which a loopback listener never
    # answers, so both probes exec against the router process itself.
    for probe in (router.readiness_probe, router.liveness_probe):
        assert probe.http_get is None
        assert probe.tcp_socket is None
        command = probe._exec.command
        assert command[:4] == ["python3", "-I", "-S", "-c"]
        assert f"('127.0.0.1',{PD_PROXY_PORT})" in command[4]
        assert "GET /healthz" in command[4]
        # Each bounded socket wait (3 s) stays inside the probe's own timeout.
        assert probe.timeout_seconds > 3


@pytest.fixture
def health_server() -> Iterator[SimpleNamespace]:
    """A loopback HTTP server that answers every GET with ``state.status``."""
    state = SimpleNamespace(status=200, paths=[], port=0)

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.paths.append(self.path)
            self.send_response(state.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return  # keep the test output quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_probe(probe: client.V1Probe) -> subprocess.CompletedProcess[bytes]:
    """Run an exec probe's command with this interpreter in place of the image's."""
    command = list(probe._exec.command)
    assert command[0] == "python3"
    return subprocess.run(
        [sys.executable, *command[1:]], capture_output=True, timeout=30, check=False
    )


@pytest.mark.parametrize(("status", "exit_code"), [(200, 0), (503, 1)])
def test_pd_router_probe_passes_only_on_a_healthz_200(
    monkeypatch: pytest.MonkeyPatch, health_server: SimpleNamespace, status: int, exit_code: int
) -> None:
    health_server.status = status
    monkeypatch.setattr(inference_monitor, "PD_PROXY_PORT", health_server.port)

    results = [_run_probe(_pd_proxy_health_probe(liveness=live)) for live in (False, True)]

    assert [result.returncode for result in results] == [exit_code, exit_code], [
        result.stderr for result in results
    ]
    assert health_server.paths == ["/healthz", "/healthz"]


def test_pd_router_probe_fails_when_the_router_is_not_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bound but never listening: the connection is refused, deterministically.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setattr(inference_monitor, "PD_PROXY_PORT", reserved.getsockname()[1])
        result = _run_probe(_pd_proxy_health_probe(liveness=False))

    assert result.returncode != 0


# ---------------------------------------------------------------------------
# The sidecar program ConfigMap
# ---------------------------------------------------------------------------


def test_tls_proxy_configmap_ships_the_current_program_source(monitor: InferenceMonitor) -> None:
    monitor.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)

    monitor._ensure_tls_proxy_configmap("chat", NAMESPACE)

    [created] = monitor.core_v1.create_namespaced_config_map.call_args_list
    namespace, body = created.args[:2]
    assert namespace == NAMESPACE
    assert body.metadata.name == TLS_PROXY_CONFIG_MAP
    assert body.metadata.labels == {"app": "chat", "project": "gco", "gco.io/type": "inference"}
    # Byte for byte the program in this checkout, under the file name the
    # sidecar runs from its read-only mount.
    assert body.data == {ENDPOINT_TLS_PROXY_SCRIPT_FILENAME: _tls_proxy_source()}
    mount_dir, _, file_name = ENDPOINT_TLS_PROXY_SCRIPT_PATH.rpartition("/")
    assert (mount_dir, file_name) == (
        ENDPOINT_TLS_PROXY_CONFIG_MOUNT_DIR,
        ENDPOINT_TLS_PROXY_SCRIPT_FILENAME,
    )
    monitor.core_v1.patch_namespaced_config_map.assert_not_called()


def test_current_tls_proxy_configmap_costs_one_read(monitor: InferenceMonitor) -> None:
    monitor.core_v1.read_namespaced_config_map.return_value = _live_config_map(
        {ENDPOINT_TLS_PROXY_SCRIPT_FILENAME: _tls_proxy_source()}
    )

    monitor._ensure_tls_proxy_configmap("chat", NAMESPACE)

    monitor.core_v1.read_namespaced_config_map.assert_called_once_with(
        TLS_PROXY_CONFIG_MAP, NAMESPACE, _request_timeout=monitor._k8s_timeout
    )
    monitor.core_v1.create_namespaced_config_map.assert_not_called()
    monitor.core_v1.patch_namespaced_config_map.assert_not_called()


@pytest.mark.parametrize(
    "stale",
    [{ENDPOINT_TLS_PROXY_SCRIPT_FILENAME: "print('previous build')\n"}, {}, None],
    ids=["previous-program", "empty", "no-data"],
)
def test_stale_tls_proxy_configmap_is_patched_at_its_observed_version(
    monitor: InferenceMonitor, stale: dict[str, str] | None
) -> None:
    monitor.core_v1.read_namespaced_config_map.return_value = _live_config_map(stale)

    monitor._ensure_tls_proxy_configmap("chat", NAMESPACE)

    [patched] = monitor.core_v1.patch_namespaced_config_map.call_args_list
    name, namespace, body = patched.args
    assert (name, namespace) == (TLS_PROXY_CONFIG_MAP, NAMESPACE)
    assert body.metadata.resource_version == "12"
    assert body.data == {ENDPOINT_TLS_PROXY_SCRIPT_FILENAME: _tls_proxy_source()}
    monitor.core_v1.create_namespaced_config_map.assert_not_called()


def test_tls_proxy_configmap_read_failure_is_not_mistaken_for_absence(
    monitor: InferenceMonitor,
) -> None:
    monitor.core_v1.read_namespaced_config_map.side_effect = ApiException(status=500)

    with pytest.raises(ApiException) as raised:
        monitor._ensure_tls_proxy_configmap("chat", NAMESPACE)

    assert raised.value.status == 500
    monitor.core_v1.create_namespaced_config_map.assert_not_called()
    monitor.core_v1.patch_namespaced_config_map.assert_not_called()


@pytest.mark.parametrize(
    ("winner_program", "patched_version"),
    [("current", None), ("previous", "3")],
    ids=["winner-is-current", "winner-is-stale"],
)
def test_tls_proxy_configmap_create_race_converges_on_the_winner(
    monitor: InferenceMonitor, winner_program: str, patched_version: str | None
) -> None:
    source = _tls_proxy_source() if winner_program == "current" else "print('previous')\n"
    monitor.core_v1.read_namespaced_config_map.side_effect = [
        ApiException(status=404),
        _live_config_map({ENDPOINT_TLS_PROXY_SCRIPT_FILENAME: source}, resource_version="3"),
    ]
    # A concurrent writer creates the ConfigMap between the read and the create.
    monitor.core_v1.create_namespaced_config_map.side_effect = ApiException(status=409)

    monitor._ensure_tls_proxy_configmap("chat", NAMESPACE)

    assert monitor.core_v1.read_namespaced_config_map.call_count == 2
    versions = [
        patched.args[2].metadata.resource_version
        for patched in monitor.core_v1.patch_namespaced_config_map.call_args_list
    ]
    assert versions == ([patched_version] if patched_version else [])


def test_tls_proxy_configmap_create_failure_propagates(monitor: InferenceMonitor) -> None:
    monitor.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    monitor.core_v1.create_namespaced_config_map.side_effect = ApiException(status=403)

    with pytest.raises(ApiException) as raised:
        monitor._ensure_tls_proxy_configmap("chat", NAMESPACE)

    assert raised.value.status == 403
    monitor.core_v1.read_namespaced_config_map.assert_called_once()
    monitor.core_v1.patch_namespaced_config_map.assert_not_called()


# ---------------------------------------------------------------------------
# Model Services publish exactly the sidecar port
# ---------------------------------------------------------------------------

SERVICE_CREATORS = [
    pytest.param(
        lambda m: m._create_service("chat", NAMESPACE, {"port": 9000}), "chat", id="classic"
    ),
    pytest.param(
        lambda m: m._create_service("chat-canary", NAMESPACE, {"port": 9000}),
        "chat-canary",
        id="canary",
    ),
    pytest.param(
        lambda m: m._create_role_service("chat", NAMESPACE, "prefill", 9000),
        "chat-prefill",
        id="prefill",
    ),
    pytest.param(
        lambda m: m._create_role_service("chat", NAMESPACE, "decode"), "chat-decode", id="decode"
    ),
    pytest.param(
        lambda m: m._create_proxy_service("chat-proxy", NAMESPACE), "chat-proxy", id="pd-proxy"
    ),
]


@pytest.mark.parametrize(("create", "service_name"), SERVICE_CREATORS)
def test_every_endpoint_service_publishes_only_the_tls_port(
    monitor: InferenceMonitor,
    create: Callable[[InferenceMonitor], None],
    service_name: str,
) -> None:
    create(monitor)

    [created] = monitor.core_v1.create_namespaced_service.call_args_list
    namespace, service = created.args[:2]
    assert namespace == NAMESPACE
    assert service.metadata.name == service_name
    assert service.spec.type == "ClusterIP"
    # Selectors keep the app label and the inference marker NetworkPolicy peers use.
    assert service.spec.selector["app"] == service_name
    assert service.spec.selector["gco.io/type"] == "inference"
    # Whatever port the model serves on, only the sidecar is published.
    assert _wire(service.spec.ports) == TLS_SERVICE_PORTS_WIRE


@pytest.mark.parametrize(("create", "service_name"), SERVICE_CREATORS)
def test_existing_pre_sidecar_service_is_moved_onto_the_tls_port_in_place(
    monitor: InferenceMonitor,
    create: Callable[[InferenceMonitor], None],
    service_name: str,
) -> None:
    monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=409)
    monitor.core_v1.read_namespaced_service.return_value = _live_service(
        service_name, ports=_legacy_ports()
    )

    create(monitor)

    _assert_moved_to_tls(monitor, service_name, resource_version="41")
    monitor.core_v1.delete_namespaced_service.assert_not_called()


@pytest.mark.parametrize(
    "legacy_ports",
    [
        pytest.param(_legacy_ports(), id="port-80-to-model"),
        pytest.param([client.V1ServicePort(port=8000, target_port=8000)], id="role-port"),
        pytest.param(
            [*_tls_ports(), client.V1ServicePort(name="http", port=80, target_port=8000)],
            id="tls-beside-plaintext",
        ),
    ],
)
def test_ensure_service_moves_a_legacy_service_onto_the_tls_port(
    monitor: InferenceMonitor, legacy_ports: list[client.V1ServicePort]
) -> None:
    monitor.core_v1.read_namespaced_service.return_value = _live_service("chat", ports=legacy_ports)

    monitor._ensure_service("chat", NAMESPACE, {"port": 8000})

    _assert_moved_to_tls(monitor, "chat", resource_version="41")
    monitor.core_v1.create_namespaced_service.assert_not_called()


def test_service_patch_without_an_observed_version_carries_no_precondition(
    monitor: InferenceMonitor,
) -> None:
    service = _live_service("chat", ports=_legacy_ports(), resource_version=None)

    assert monitor._converge_service_ports("chat", NAMESPACE, service) is True

    _assert_moved_to_tls(monitor, "chat", resource_version=None)


def test_service_already_on_the_tls_port_is_left_alone(monitor: InferenceMonitor) -> None:
    monitor.core_v1.read_namespaced_service.return_value = _live_service("chat", ports=_tls_ports())

    monitor._ensure_service("chat", NAMESPACE, {"port": 8000})

    monitor.core_v1.patch_namespaced_service.assert_not_called()
    monitor.core_v1.create_namespaced_service.assert_not_called()


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        pytest.param(_live_service("chat", ports=_tls_ports()), True, id="tls-only"),
        pytest.param(
            _live_service("chat", ports=_tls_ports(protocol=None)), True, id="protocol-defaulted"
        ),
        pytest.param(_live_service("chat", ports=_legacy_ports()), False, id="plaintext"),
        pytest.param(
            _live_service(
                "chat",
                ports=[client.V1ServicePort(name="https", port=8443, target_port=8443)],
            ),
            False,
            id="numeric-target",
        ),
        pytest.param(
            _live_service(
                "chat",
                ports=[client.V1ServicePort(name="tls", port=8443, target_port="https")],
            ),
            False,
            id="renamed-port",
        ),
        pytest.param(_live_service("chat", ports=_tls_ports(protocol="UDP")), False, id="udp"),
        pytest.param(_live_service("chat", ports=None), False, id="no-ports"),
        pytest.param(SimpleNamespace(), False, id="no-spec"),
    ],
)
def test_service_port_shape_detection(service: Any, expected: bool) -> None:
    assert _service_publishes_only_tls(service) is expected


# ---------------------------------------------------------------------------
# Existing Deployments gain (or refresh) the sidecar in place
# ---------------------------------------------------------------------------


def test_pre_sidecar_deployment_gains_the_sidecar_by_strategic_merge(
    monitor: InferenceMonitor,
) -> None:
    deployment = _live_deployment(
        "chat",
        containers=[_model_container(9000)],
        annotations={
            SKIP_CONTAINERS_ANNOTATION: "log-shipper",
            "kubectl.kubernetes.io/restartedAt": "2026-01-01T00:00:00Z",
        },
    )

    patched = monitor._converge_endpoint_tls_proxy(
        "chat", NAMESPACE, deployment, config_map_name=TLS_PROXY_CONFIG_MAP, fallback_port=8000
    )

    assert patched is True
    # The sidecar forwards to where the live model listens (9000), not to the
    # spec default. The patch names only the sidecar, its volumes, the grace
    # period, and two annotations: the model container, other annotations and
    # every other field stay exactly as they are.
    expected = build_endpoint_tls_proxy(9000, TLS_PROXY_CONFIG_MAP)
    assert _deployment_patch(monitor, "chat") == {
        "metadata": {"resourceVersion": "7"},
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        SKIP_CONTAINERS_ANNOTATION: f"log-shipper,{ENDPOINT_TLS_PROXY_CONTAINER}",
                        ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION: expected.digest,
                    }
                },
                "spec": {
                    "terminationGracePeriodSeconds": MODEL_POD_TERMINATION_GRACE_SECONDS,
                    "containers": [_wire(expected.container)],
                    "volumes": _wire(list(expected.volumes)),
                },
            }
        },
    }
    assert _env(expected.container)["TLS_PROXY_UPSTREAM_PORT"] == "9000"


def test_deployment_carrying_the_current_sidecar_is_left_alone(
    monitor: InferenceMonitor,
) -> None:
    current = build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP)
    deployment = _live_deployment(
        "chat",
        containers=[_model_container(8000), current.container],
        annotations={ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION: current.digest},
    )

    patched = monitor._converge_endpoint_tls_proxy(
        "chat", NAMESPACE, deployment, config_map_name=TLS_PROXY_CONFIG_MAP, fallback_port=8000
    )

    assert patched is False
    monitor.apps_v1.patch_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize(
    ("with_sidecar", "stamp"),
    [(True, "stale"), (True, None), (False, "current")],
    ids=["stale-digest", "unstamped", "sidecar-removed"],
)
def test_deployment_with_a_drifted_sidecar_is_rolled(
    monitor: InferenceMonitor, with_sidecar: bool, stamp: str | None
) -> None:
    current = build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP)
    annotations: dict[str, str] = {}
    if stamp is not None:
        annotations[ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION] = (
            current.digest if stamp == "current" else "0" * 64
        )
    containers = [_model_container(8000)] + ([current.container] if with_sidecar else [])
    deployment = _live_deployment("chat", containers=containers, annotations=annotations)

    patched = monitor._converge_endpoint_tls_proxy(
        "chat", NAMESPACE, deployment, config_map_name=TLS_PROXY_CONFIG_MAP, fallback_port=8000
    )

    assert patched is True
    template = _deployment_patch(monitor, "chat")["spec"]["template"]
    assert template["metadata"]["annotations"][ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION] == (
        current.digest
    )
    assert template["spec"]["containers"] == [_wire(current.container)]


def test_image_override_rolls_the_new_sidecar_image_into_running_pods(
    monitor: InferenceMonitor, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamped = build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP)
    deployment = _live_deployment(
        "chat",
        containers=[_model_container(8000), stamped.container],
        annotations={ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION: stamped.digest},
    )
    override = "registry.example.com/mirror/python:3.14.7-slim@sha256:" + "b" * 64
    monkeypatch.setenv(ENDPOINT_TLS_PROXY_IMAGE_ENV, override)

    patched = monitor._converge_endpoint_tls_proxy(
        "chat", NAMESPACE, deployment, config_map_name=TLS_PROXY_CONFIG_MAP, fallback_port=8000
    )

    assert patched is True
    [sidecar] = _deployment_patch(monitor, "chat")["spec"]["template"]["spec"]["containers"]
    assert sidecar["image"] == override


def test_deployment_patch_without_an_observed_version_carries_no_precondition(
    monitor: InferenceMonitor,
) -> None:
    deployment = _live_deployment("chat", resource_version=None)

    monitor._converge_endpoint_tls_proxy(
        "chat", NAMESPACE, deployment, config_map_name=TLS_PROXY_CONFIG_MAP, fallback_port=8000
    )

    assert "metadata" not in _deployment_patch(monitor, "chat")


@pytest.mark.parametrize(
    ("containers", "expected"),
    [
        pytest.param(
            [
                SimpleNamespace(
                    name="inference",
                    ports=[
                        SimpleNamespace(container_port="8000"),
                        SimpleNamespace(container_port=True),
                        SimpleNamespace(container_port=9001),
                    ],
                )
            ],
            9001,
            id="skips-non-integer-ports",
        ),
        pytest.param(
            [SimpleNamespace(name="inference", ports=[SimpleNamespace(container_port=None)])],
            8000,
            id="no-usable-port",
        ),
        pytest.param([SimpleNamespace(name="inference", ports=None)], 8000, id="no-ports"),
        pytest.param(
            [
                SimpleNamespace(name="log-shipper", ports=[SimpleNamespace(container_port=7000)]),
                SimpleNamespace(name="inference", ports=[SimpleNamespace(container_port=9002)]),
            ],
            9002,
            id="only-the-model-container",
        ),
        pytest.param([], 8000, id="no-containers"),
    ],
)
def test_model_server_port_reads_only_a_usable_model_container_port(
    containers: list[Any], expected: int
) -> None:
    assert _model_server_port(containers, 8000) == expected


def test_model_port_reserved_for_the_sidecar_is_refused_at_render(
    monitor: InferenceMonitor,
) -> None:
    with pytest.raises(ValueError, match="port 8443 is reserved for the model pod's TLS sidecar"):
        monitor._create_deployment("chat", NAMESPACE, {"image": MODEL_IMAGE, "port": 8443})

    monitor.apps_v1.create_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize(
    ("base_port", "clashes"),
    [
        (8342, False),  # window 8342-8442 ends just below the sidecar
        (8343, True),  # window 8343-8443 ends on the sidecar port
        (8400, True),
        (8443, True),  # window starts on the sidecar port
        (8444, False),
        (8998, False),  # the default base port
    ],
)
def test_bootstrap_window_containing_the_sidecar_port_is_refused_at_render(
    monitor: InferenceMonitor, base_port: int, clashes: bool
) -> None:
    """Mooncake workers bind anywhere in the bootstrap window, so it must skip 8443."""
    spec = {
        "image": MODEL_IMAGE,
        "port": 8100,
        "mooncake": {
            "mode": "disaggregated",
            "transfer": {"protocol": "rdma", "bootstrap_base_port": base_port},
        },
    }

    if clashes:
        with pytest.raises(ValueError, match="contains port 8443, reserved for the model pod's"):
            monitor._create_role_deployment("chat", NAMESPACE, spec, "prefill")
        monitor.apps_v1.create_namespaced_deployment.assert_not_called()
    else:
        monitor._create_role_deployment("chat", NAMESPACE, spec, "prefill")
        env = _env(_created_deployment(monitor, "chat-prefill").spec.template.spec.containers[0])
        assert env["VLLM_MOONCAKE_BOOTSTRAP_PORT"] == str(base_port)


def test_live_model_on_the_sidecar_port_is_refused_in_place(monitor: InferenceMonitor) -> None:
    deployment = _live_deployment("chat", containers=[_model_container(8443)])

    with pytest.raises(ValueError, match="port 8443 is reserved for the model pod's TLS sidecar"):
        monitor._converge_endpoint_tls_proxy(
            "chat", NAMESPACE, deployment, config_map_name=TLS_PROXY_CONFIG_MAP, fallback_port=8000
        )

    monitor.apps_v1.patch_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize(
    ("existing", "merged"),
    [
        (None, "endpoint-tls-proxy"),
        ("", "endpoint-tls-proxy"),
        (7, "endpoint-tls-proxy"),
        ("log-shipper", "log-shipper,endpoint-tls-proxy"),
        (" log-shipper , ,istio-proxy ", "log-shipper,istio-proxy,endpoint-tls-proxy"),
        ("endpoint-tls-proxy,log-shipper", "endpoint-tls-proxy,log-shipper"),
        (" endpoint-tls-proxy ", "endpoint-tls-proxy"),
    ],
)
def test_skip_containers_merge_keeps_order_and_is_idempotent(existing: object, merged: str) -> None:
    assert _merge_skip_containers(existing) == merged
    assert _merge_skip_containers(merged) == merged


# ---------------------------------------------------------------------------
# Reconcile converges pre-sidecar endpoints in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_moves_a_pre_sidecar_endpoint_onto_tls_in_one_pass(
    monitor: InferenceMonitor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass moves the Service, publishes the program, and rolls the sidecar in.

    The replica count and model image are already converged, so the sidecar
    patch is the only Deployment write, and the endpoint keeps reporting
    running while the rollout proceeds.
    """
    spec = {"image": MODEL_IMAGE, "port": 8000, "replicas": 1}
    endpoint = {
        "endpoint_name": "chat",
        "desired_state": "running",
        "target_regions": [REGION],
        "spec": spec,
        "namespace": NAMESPACE,
    }
    monitor.apps_v1.read_namespaced_deployment.side_effect = _by_name(
        {"chat": _live_deployment("chat")}
    )
    monitor.core_v1.read_namespaced_service.side_effect = _by_name(
        {"chat": _live_service("chat", ports=_legacy_ports(), resource_version="5")}
    )
    monitor.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    monkeypatch.setattr(
        monitor, "_delete_autoscalers", MagicMock(return_value=ResourceCleanupResult())
    )
    store = MagicMock()
    monitor.store = store

    result = await monitor._reconcile_running("chat", NAMESPACE, spec, endpoint)

    assert result is None
    _assert_moved_to_tls(monitor, "chat", resource_version="5")
    [created] = monitor.core_v1.create_namespaced_config_map.call_args_list
    assert created.args[1].metadata.name == TLS_PROXY_CONFIG_MAP
    body = _deployment_patch(monitor, "chat")
    assert body["metadata"] == {"resourceVersion": "7"}
    assert [container["name"] for container in body["spec"]["template"]["spec"]["containers"]] == [
        ENDPOINT_TLS_PROXY_CONTAINER
    ]
    assert monitor.apps_v1.patch_namespaced_deployment.call_count == 1
    assert store.update_region_status.call_args.args == ("chat", REGION, "running")


def test_role_deployment_gains_the_sidecar_in_place(monitor: InferenceMonitor) -> None:
    spec = {
        "image": MODEL_IMAGE,
        "port": 8000,
        "mooncake": {"mode": "disaggregated", "topology": {"prefill": 2, "decode": 1}},
    }
    monitor.apps_v1.read_namespaced_deployment.side_effect = _by_name(
        {
            "chat-prefill": _live_deployment(
                "chat-prefill", containers=[_model_container(8100)], replicas=2, ready_replicas=1
            )
        }
    )

    assert monitor._ensure_role_deployment("chat", NAMESPACE, spec, "prefill") == (1, 2, False)

    # Role pods run the endpoint's program and forward to the live model port.
    expected = build_endpoint_tls_proxy(8100, TLS_PROXY_CONFIG_MAP)
    template = _deployment_patch(monitor, "chat-prefill")["spec"]["template"]
    assert template["metadata"]["annotations"][ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION] == (
        expected.digest
    )
    assert template["spec"]["volumes"] == _wire(list(expected.volumes))
    # Already at its topology count: the sidecar patch is the only write.
    assert monitor.apps_v1.patch_namespaced_deployment.call_count == 1


def test_existing_canary_is_moved_onto_tls_with_its_endpoint_program(
    monitor: InferenceMonitor,
) -> None:
    canary_image = "vllm/vllm-openai:v0.7.0"
    canary = {"image": canary_image, "replicas": 1, "weight": 10}
    monitor.apps_v1.read_namespaced_deployment.side_effect = _by_name(
        {
            "chat-canary": _live_deployment(
                "chat-canary", containers=[_model_container(8000, image=canary_image)]
            )
        }
    )
    monitor.core_v1.read_namespaced_service.side_effect = _by_name(
        {"chat-canary": _live_service("chat-canary", ports=_legacy_ports())}
    )

    status = monitor._reconcile_canary(
        "chat", NAMESPACE, {"image": MODEL_IMAGE, "port": 8000, "canary": canary}, canary, {}
    )

    assert status["state"] == "running"
    _assert_moved_to_tls(monitor, "chat-canary", resource_version="41")
    template = _deployment_patch(monitor, "chat-canary")["spec"]["template"]
    assert template["metadata"]["annotations"][ENDPOINT_TLS_PROXY_DIGEST_ANNOTATION] == (
        build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP).digest
    )
    volumes = {volume["name"]: volume for volume in template["spec"]["volumes"]}
    assert volumes[ENDPOINT_TLS_PROXY_SCRIPT_VOLUME]["configMap"]["name"] == TLS_PROXY_CONFIG_MAP
    monitor.core_v1.create_namespaced_config_map.assert_not_called()


def test_existing_pd_proxy_is_merge_patched_to_the_tls_shape(monitor: InferenceMonitor) -> None:
    _admit_named_admin_secret(monitor)
    legacy_router = client.V1Container(
        name="proxy",
        image=ROUTER_IMAGE,
        ports=[client.V1ContainerPort(container_port=PD_PROXY_PORT)],
        readiness_probe=client.V1Probe(tcp_socket=client.V1TCPSocketAction(port=PD_PROXY_PORT)),
    )
    monitor.apps_v1.create_namespaced_deployment.side_effect = ApiException(status=409)
    monitor.apps_v1.read_namespaced_deployment.side_effect = _by_name(
        {
            "chat-proxy": _live_deployment(
                "chat-proxy", containers=[legacy_router], resource_version="9"
            )
        }
    )
    monitor.core_v1.create_namespaced_service.side_effect = ApiException(status=409)
    monitor.core_v1.read_namespaced_service.side_effect = _by_name(
        {"chat-proxy": _live_service("chat-proxy", ports=_legacy_ports(), resource_version="13")}
    )

    monitor._create_pd_proxy("chat", NAMESPACE, _pd_spec(), {"lifecycle_id": "life-1"})

    [patched] = monitor.apps_v1.patch_namespaced_deployment.call_args_list
    assert patched.args == ("chat-proxy", NAMESPACE)
    # A JSON merge patch replaces the containers and volumes lists wholesale,
    # so the router's old tcpSocket probe cannot survive beside the exec one.
    assert patched.kwargs["_content_type"] == "application/merge-patch+json"
    body = _wire(patched.kwargs["body"])
    assert body["metadata"]["resourceVersion"] == "9"
    pod = body["spec"]["template"]["spec"]
    assert [container["name"] for container in pod["containers"]] == [
        "proxy",
        ENDPOINT_TLS_PROXY_CONTAINER,
    ]
    router_probe = pod["containers"][0]["readinessProbe"]
    assert "exec" in router_probe
    assert "tcpSocket" not in router_probe
    assert {volume["name"] for volume in pod["volumes"]} == {
        "pd-proxy-script",
        INTERNAL_CA_VOLUME,
        ENDPOINT_TLS_VOLUME,
        ENDPOINT_TLS_PROXY_SCRIPT_VOLUME,
    }
    _assert_moved_to_tls(monitor, "chat-proxy", resource_version="13")


@pytest.mark.parametrize(
    "mutation", ["service-ports", "deployment-sidecar", "configmap-create", "configmap-update"]
)
def test_tls_mutations_stop_when_authority_is_lost(
    monitor: InferenceMonitor, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    monkeypatch.setattr(
        monitor,
        "_assert_mutation_authority",
        MagicMock(side_effect=ReconcileFencedError("authority lost")),
    )
    if mutation == "configmap-create":
        monitor.core_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    else:
        monitor.core_v1.read_namespaced_config_map.return_value = _live_config_map({})
    mutations: dict[str, Callable[[], object]] = {
        "service-ports": lambda: monitor._converge_service_ports(
            "chat", NAMESPACE, _live_service("chat", ports=_legacy_ports())
        ),
        "deployment-sidecar": lambda: monitor._converge_endpoint_tls_proxy(
            "chat",
            NAMESPACE,
            _live_deployment("chat"),
            config_map_name=TLS_PROXY_CONFIG_MAP,
            fallback_port=8000,
        ),
        "configmap-create": lambda: monitor._ensure_tls_proxy_configmap("chat", NAMESPACE),
        "configmap-update": lambda: monitor._ensure_tls_proxy_configmap("chat", NAMESPACE),
    }

    with pytest.raises(ReconcileFencedError, match="authority lost"):
        mutations[mutation]()

    monitor.core_v1.patch_namespaced_service.assert_not_called()
    monitor.apps_v1.patch_namespaced_deployment.assert_not_called()
    monitor.core_v1.create_namespaced_config_map.assert_not_called()
    monitor.core_v1.patch_namespaced_config_map.assert_not_called()


# ---------------------------------------------------------------------------
# The sidecar image: pinned multi-arch release, overridable per deployment
# ---------------------------------------------------------------------------


def test_sidecar_image_pins_a_python_release_by_tag_and_index_digest() -> None:
    match = re.fullmatch(
        r"public\.ecr\.aws/docker/library/python:3\.(?P<minor>\d+)\.\d+-slim"
        r"@sha256:[0-9a-f]{64}",
        ENDPOINT_TLS_PROXY_IMAGE,
    )
    assert match is not None, ENDPOINT_TLS_PROXY_IMAGE
    # The pinned interpreter must parse the program the ConfigMap ships.
    ast.parse(_tls_proxy_source(), feature_version=(3, int(match["minor"])))


def test_sidecar_image_env_override_reaches_the_pod_and_its_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP)
    override = "registry.example.com/mirror/python:3.14.7-slim@sha256:" + "a" * 64
    monkeypatch.setenv(ENDPOINT_TLS_PROXY_IMAGE_ENV, f"  {override}  ")

    overridden = build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP)

    assert default.container.image == ENDPOINT_TLS_PROXY_IMAGE
    assert overridden.container.image == override
    # A changed image is a changed digest, which is what rolls existing pods.
    assert overridden.digest != default.digest
    monkeypatch.setenv(ENDPOINT_TLS_PROXY_IMAGE_ENV, "   ")
    assert build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP).container.image == (
        ENDPOINT_TLS_PROXY_IMAGE
    )


def test_sidecar_digest_is_stable_and_tracks_every_rendered_input() -> None:
    base = build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP)

    assert re.fullmatch(r"[0-9a-f]{64}", base.digest)
    assert build_endpoint_tls_proxy(8000, TLS_PROXY_CONFIG_MAP).digest == base.digest
    assert build_endpoint_tls_proxy(8001, TLS_PROXY_CONFIG_MAP).digest != base.digest
    assert build_endpoint_tls_proxy(8000, "other-tls-proxy").digest != base.digest


# ---------------------------------------------------------------------------
# The monitor's own metrics listener
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "port", "host"),
    [
        ({"METRICS_HOST": "127.0.0.1", "METRICS_PORT": "9555"}, 9555, "127.0.0.1"),
        ({}, 9090, "0.0.0.0"),
    ],
    ids=["loopback-behind-sidecar", "bare-local-run"],
)
async def test_monitor_metrics_listener_binds_metrics_host(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], port: int, host: str
) -> None:
    for key in ("METRICS_HOST", "METRICS_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    fake_monitor = MagicMock()
    fake_monitor.get_metrics.return_value = {}
    fake_monitor.start = AsyncMock(side_effect=KeyboardInterrupt)

    with (
        patch.object(
            inference_monitor, "create_inference_monitor_from_env", return_value=fake_monitor
        ),
        patch("gco.services.service_metrics.start_metrics_server") as start_metrics_server,
    ):
        await inference_monitor.main()

    start_metrics_server.assert_called_once_with(
        port, "inference-monitor", fake_monitor.get_metrics, host=host
    )
