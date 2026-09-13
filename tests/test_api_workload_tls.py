"""Contracts for TLS re-encryption from the regional ALB to GCO API pods."""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gco.services.tls_proxy import (
    DEFAULT_CERT_FILE,
    DEFAULT_KEY_FILE,
    TLS_CERT_FILE_ENV,
    TLS_KEY_FILE_ENV,
    ProxyConfig,
    TlsProxy,
    _keypair_digest,
    load_proxy_config,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_DIR = _REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
_API_WORKLOADS = {
    "30-health-monitor.yaml": ("health-monitor", "health-monitor-tls"),
    "31-manifest-processor.yaml": ("manifest-processor", "manifest-processor-tls"),
    "33-inference-proxy.yaml": ("inference-proxy", "inference-proxy-tls"),
}


def _documents(filename: str) -> list[dict]:
    text = (_MANIFEST_DIR / filename).read_text(encoding="utf-8")
    rendered = text.replace("{{INFERENCE_PROXY_TLS_CPU_REQUEST}}", "100m").replace(
        "{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}", "70"
    )
    rendered = re.sub(r"\{\{[A-Z0-9_]+\}\}", "test-value", rendered)
    return [document for document in yaml.safe_load_all(rendered) if document]


def test_inference_tls_autoscaling_tokens_are_scoped_to_inference_proxy() -> None:
    """Health and manifest TLS resource profiles remain fixed and untokenized."""
    tokens = (
        "{{INFERENCE_PROXY_TLS_CPU_REQUEST}}",
        "{{INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION}}",
    )
    inference_manifest = (_MANIFEST_DIR / "33-inference-proxy.yaml").read_text(encoding="utf-8")
    assert all(inference_manifest.count(token) == 1 for token in tokens)

    for filename in ("30-health-monitor.yaml", "31-manifest-processor.yaml"):
        manifest = (_MANIFEST_DIR / filename).read_text(encoding="utf-8")
        assert all(token not in manifest for token in tokens)


def _proxy_config(tmp_path: Path) -> ProxyConfig:
    return ProxyConfig(
        host="127.0.0.1",
        port=8443,
        upstream_host="127.0.0.1",
        upstream_port=9000,
        cert_file=tmp_path / "tls.crt",
        key_file=tmp_path / "tls.key",
        poll_seconds=0,
        graceful_shutdown_seconds=1,
    )


def _write_test_keypair(config: ProxyConfig, common_name: str = "localhost") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    config.cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    config.key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate.fingerprint(hashes.SHA256()).hex()


def test_tls_proxy_defaults_are_tls_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        TLS_CERT_FILE_ENV,
        TLS_KEY_FILE_ENV,
        "TLS_PROXY_PORT",
        "TLS_PROXY_UPSTREAM_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_proxy_config()

    assert config.port == 8443
    assert config.upstream_host == "127.0.0.1"
    assert config.upstream_port == 9000
    assert config.cert_file == Path(DEFAULT_CERT_FILE)
    assert config.key_file == Path(DEFAULT_KEY_FILE)
    assert not hasattr(config, "http_port")


def test_tls_proxy_fails_closed_when_projected_keypair_is_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not reference a readable file"):
        _keypair_digest(_proxy_config(tmp_path))


@pytest.mark.asyncio
async def test_tls_proxy_terminates_tls_and_forwards_only_to_loopback(tmp_path: Path) -> None:
    async def echo(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(await reader.read(1024))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    config = replace(_proxy_config(tmp_path), port=0, upstream_port=upstream_port)
    _write_test_keypair(config)
    proxy = TlsProxy(config)
    await proxy.start()
    assert proxy._server is not None
    tls_port = proxy._server.sockets[0].getsockname()[1]

    client_context = ssl.create_default_context()
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        tls_port,
        ssl=client_context,
        server_hostname="localhost",
    )
    writer.write(b"encrypted-hop")
    await writer.drain()
    assert await reader.read(1024) == b"encrypted-hop"
    writer.close()
    await writer.wait_closed()

    await proxy.shutdown()
    upstream.close()
    await upstream.wait_closed()


@pytest.mark.asyncio
async def test_real_certificate_rotation_preserves_stream_and_updates_new_handshake(
    tmp_path: Path,
) -> None:
    """A live stream survives acceptor replacement and new peers see the new leaf."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        fixed_tls_port = int(reservation.getsockname()[1])
    config = replace(
        _proxy_config(tmp_path),
        port=fixed_tls_port,
        upstream_port=upstream_port,
        poll_seconds=0.01,
    )
    first_fingerprint = _write_test_keypair(config, "rotation-a")
    proxy = TlsProxy(config)
    await proxy.start()
    assert proxy._server is not None
    first_port = proxy._server.sockets[0].getsockname()[1]
    assert first_port == fixed_tls_port
    watcher = asyncio.create_task(proxy.watch_certificates())

    client_context = ssl.create_default_context()
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    old_reader, old_writer = await asyncio.open_connection(
        "127.0.0.1",
        first_port,
        ssl=client_context,
        server_hostname="localhost",
    )
    old_ssl = old_writer.get_extra_info("ssl_object")
    assert old_ssl is not None
    old_leaf = x509.load_der_x509_certificate(old_ssl.getpeercert(binary_form=True))
    assert old_leaf.fingerprint(hashes.SHA256()).hex() == first_fingerprint
    old_writer.write(b"before-rotation")
    await old_writer.drain()
    assert await old_reader.readexactly(len(b"before-rotation")) == b"before-rotation"

    second_fingerprint = _write_test_keypair(config, "rotation-b")
    rotated_digest = _keypair_digest(config)

    async def wait_for_watcher_rotation() -> None:
        while proxy._keypair_digest != rotated_digest:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_for_watcher_rotation(), timeout=2)
    assert proxy._server is not None

    # The accepted TLS session keeps forwarding even though its acceptor is gone.
    old_writer.write(b"after-rotation")
    await old_writer.drain()
    assert await old_reader.readexactly(len(b"after-rotation")) == b"after-rotation"

    second_port = proxy._server.sockets[0].getsockname()[1]
    assert second_port == fixed_tls_port == first_port
    new_reader, new_writer = await asyncio.open_connection(
        "127.0.0.1",
        second_port,
        ssl=client_context,
        server_hostname="localhost",
    )
    new_ssl = new_writer.get_extra_info("ssl_object")
    assert new_ssl is not None
    new_leaf = x509.load_der_x509_certificate(new_ssl.getpeercert(binary_form=True))
    assert new_leaf.fingerprint(hashes.SHA256()).hex() == second_fingerprint
    assert second_fingerprint != first_fingerprint
    new_writer.write(b"new-handshake")
    await new_writer.drain()
    assert await new_reader.readexactly(len(b"new-handshake")) == b"new-handshake"

    new_writer.close()
    old_writer.close()
    await new_writer.wait_closed()
    await old_writer.wait_closed()
    await proxy.shutdown()
    await asyncio.wait_for(watcher, timeout=1)
    upstream.close()
    await upstream.wait_closed()


@pytest.mark.asyncio
async def test_tls_proxy_reloads_acceptor_without_closing_established_streams(
    tmp_path: Path,
) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    old_server = MagicMock()
    old_server.wait_closed = AsyncMock()
    new_server = MagicMock()
    proxy._server = old_server
    existing_connection = MagicMock()
    proxy._connections.add(existing_connection)

    with patch("asyncio.start_server", new=AsyncMock(return_value=new_server)) as start_server:
        await proxy._reload_certificate(MagicMock(), "new-digest")

    old_server.close.assert_called_once_with()
    await asyncio.sleep(0)
    old_server.wait_closed.assert_awaited_once_with()
    start_server.assert_awaited_once()
    assert proxy._server is new_server
    assert proxy._keypair_digest == "new-digest"
    assert proxy._connections == {existing_connection}


@pytest.mark.asyncio
async def test_tls_proxy_rebind_failure_requests_container_restart(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    old_server = MagicMock()
    old_server.wait_closed = AsyncMock()
    proxy._server = old_server

    with (
        patch("asyncio.start_server", new=AsyncMock(side_effect=OSError("address unavailable"))),
        pytest.raises(OSError, match="address unavailable"),
    ):
        await proxy._reload_certificate(MagicMock(), "new-digest")

    old_server.close.assert_called_once_with()
    assert proxy._server is None
    assert proxy._stop.is_set()


@pytest.mark.parametrize("filename,identity", _API_WORKLOADS.items())
def test_api_workload_uses_tls_only_sidecar_probe_and_service(
    filename: str,
    identity: tuple[str, str],
) -> None:
    app, secret_name = identity
    documents = _documents(filename)
    deployment = next(document for document in documents if document["kind"] == "Deployment")
    service = next(document for document in documents if document["kind"] == "Service")
    pod_spec = deployment["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod_spec["containers"]}
    application = containers[app]
    tls_proxy = containers["api-tls-proxy"]

    assert application["ports"] == [{"name": "app-http", "containerPort": 9000, "protocol": "TCP"}]
    app_environment = {item["name"]: item.get("value") for item in application["env"]}
    assert app_environment["HOST"] == "127.0.0.1"
    assert app_environment["PORT"] == "9000"
    assert all(mount["name"] != "api-tls" for mount in application["volumeMounts"])

    assert tls_proxy["command"] == ["python", "-m", "gco.services.tls_proxy"]
    assert tls_proxy["ports"] == [{"name": "https", "containerPort": 8443, "protocol": "TCP"}]
    expected_proxy_resources = {
        "health-monitor": {
            "requests": {"cpu": "25m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
        "manifest-processor": {
            "requests": {"cpu": "25m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
        "inference-proxy": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "250m", "memory": "256Mi"},
        },
    }
    assert tls_proxy["resources"] == expected_proxy_resources[app]
    proxy_environment = {item["name"]: item.get("value") for item in tls_proxy["env"]}
    assert proxy_environment[TLS_CERT_FILE_ENV] == "/var/run/gco/tls/tls.crt"
    assert proxy_environment[TLS_KEY_FILE_ENV] == "/var/run/gco/tls/tls.key"
    assert proxy_environment["TLS_PROXY_UPSTREAM_PORT"] == "9000"
    # Both containers delay SIGTERM by the same bounded preStop window so the
    # endpoint can disappear from ALB before the TLS acceptor closes.
    assert tls_proxy["lifecycle"]["preStop"] == application["lifecycle"]["preStop"]
    pre_stop_code = tls_proxy["lifecycle"]["preStop"]["exec"]["command"][2]
    pre_stop_match = re.fullmatch(r"import time; time\.sleep\((\d+)\)", pre_stop_code)
    assert pre_stop_match is not None
    shutdown_budget = int(pre_stop_match.group(1)) + int(
        proxy_environment["GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS"]
    )
    assert pod_spec["terminationGracePeriodSeconds"] > shutdown_budget

    for probe_name in ("startupProbe", "livenessProbe", "readinessProbe"):
        command = application[probe_name]["exec"]["command"]
        # A lean interpreter start (no site-packages scan, no environment) and
        # a bare-socket request to the loopback listener: the probe competes
        # with the server for the container's CPU, so it must stay cheap.
        assert command[:4] == ["python", "-I", "-S", "-c"]
        assert "socket.create_connection(('127.0.0.1',9000),3)" in command[4]
        assert "urllib" not in command[4]
    for probe_name in ("startupProbe", "livenessProbe"):
        assert tls_proxy[probe_name]["tcpSocket"] == {"port": "https"}
    readiness_request = tls_proxy["readinessProbe"]["httpGet"]
    assert readiness_request["port"] == "https"
    assert readiness_request["scheme"] == "HTTPS"

    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert annotations["eks.amazonaws.com/skip-containers"] == "api-tls-proxy"
    assert pod_spec["automountServiceAccountToken"] is False
    proxy_mount_names = {mount["name"] for mount in tls_proxy["volumeMounts"]}
    assert proxy_mount_names == {"api-tls"}
    application_mount_names = {mount["name"] for mount in application["volumeMounts"]}
    assert "aws-iam-token" in application_mount_names
    if app in {"health-monitor", "manifest-processor"}:
        assert "kubernetes-api-token" in application_mount_names
    else:
        assert "kubernetes-api-token" not in application_mount_names

    assert tls_proxy["volumeMounts"] == [
        {"name": "api-tls", "mountPath": "/var/run/gco/tls", "readOnly": True}
    ]
    tls_volume = next(item for item in pod_spec["volumes"] if item["name"] == "api-tls")
    assert tls_volume["secret"] == {"secretName": secret_name, "defaultMode": 0o440}

    assert service["metadata"]["name"] == app
    # Keep the named targetPort: the AWS controller may represent it with a
    # target-group-wide port-1 sentinel while registering every pod on 8443.
    # Live validation checks those per-target registrations, not the sentinel.
    assert service["spec"]["ports"] == [
        {"port": 443, "targetPort": "https", "protocol": "TCP", "name": "https"}
    ]


def test_cert_manager_issues_one_rotating_ecdsa_leaf_per_api_workload() -> None:
    documents = _documents("post-helm-api-workload-certificates.yaml")
    issuer = next(document for document in documents if document["kind"] == "Issuer")
    certificates = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Certificate"
    }

    assert issuer["metadata"]["name"] == "gco-api-selfsigned"
    assert issuer["spec"] == {"selfSigned": {}}
    assert set(certificates) == {
        "health-monitor-tls",
        "manifest-processor-tls",
        "inference-proxy-tls",
    }
    for name, certificate in certificates.items():
        spec = certificate["spec"]
        assert spec["secretName"] == name
        assert spec["privateKey"] == {
            "algorithm": "ECDSA",
            "encoding": "PKCS8",
            "size": 256,
            "rotationPolicy": "Always",
        }
        assert spec["issuerRef"] == {
            "name": "gco-api-selfsigned",
            "kind": "Issuer",
            "group": "cert-manager.io",
        }


def test_gateway_reencrypts_to_https_service_ports() -> None:
    documents = _documents("post-helm-gateway.yaml")
    target_groups = [
        document for document in documents if document["kind"] == "TargetGroupConfiguration"
    ]
    default_target_group = next(
        document
        for document in target_groups
        if document["metadata"]["name"] == "gco-default-target-group"
    )
    service_target_groups = {
        document["spec"]["targetReference"]["name"]: document
        for document in target_groups
        if "targetReference" in document["spec"]
    }
    route = next(document for document in documents if document["kind"] == "HTTPRoute")
    default = default_target_group["spec"]["defaultConfiguration"]

    assert default["targetType"] == "ip"
    assert default["protocol"] == "HTTPS"
    assert default["healthCheckConfig"]["healthCheckProtocol"] == "HTTPS"
    assert default["healthCheckConfig"]["healthCheckPath"] == "/healthz"
    assert set(service_target_groups) == {
        "health-monitor",
        "manifest-processor",
        "inference-proxy",
    }
    for backend, configuration in service_target_groups.items():
        assert configuration["spec"]["defaultConfiguration"]["tags"] == {"gco.aws/backend": backend}
    assert {
        backend["port"] for rule in route["spec"]["rules"] for backend in rule["backendRefs"]
    } == {443}


def test_api_podmonitors_scrape_over_encrypted_transport() -> None:
    documents = _documents("post-helm-monitoring-servicemonitors.yaml")
    monitors = {
        document["spec"]["selector"]["matchLabels"].get("app"): document
        for document in documents
        if document["kind"] == "PodMonitor"
    }

    for app, _secret_name in _API_WORKLOADS.values():
        endpoint = monitors[app]["spec"]["podMetricsEndpoints"][0]
        assert endpoint["port"] == "https"
        assert endpoint["scheme"] == "https"
        assert endpoint["tlsConfig"] == {"insecureSkipVerify": True}


def test_alb_network_policies_expose_only_the_tls_proxy_port() -> None:
    documents = _documents("03-network-policies.yaml")
    policies = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "NetworkPolicy"
    }

    for app, _secret_name in _API_WORKLOADS.values():
        policy = policies[f"allow-alb-to-{app}"]
        assert policy["spec"]["ingress"] == [{"ports": [{"protocol": "TCP", "port": 8443}]}]


def test_api_service_accounts_disable_ambient_kubernetes_tokens() -> None:
    documents = _documents("02-rbac.yaml")
    service_accounts = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "ServiceAccount"
    }

    for name in (
        "gco-health-monitor-sa",
        "gco-manifest-processor-sa",
        "gco-inference-proxy-sa",
    ):
        assert service_accounts[name]["automountServiceAccountToken"] is False
