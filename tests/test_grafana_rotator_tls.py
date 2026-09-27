"""The Grafana credential rotator talks to Grafana over verified HTTPS.

The rotator sends the Grafana admin credential with every call, so the shipped
CronJob reaches Grafana through the ``grafana-tls`` Service (the
``grafana-tls-proxy`` sidecar) and trusts only the GCO internal CA projected at
``/var/run/gco/ca/ca.crt``. These tests pin that contract on the real
``requests`` stack: a certificate chained to another CA, or naming another
host, is refused; a missing or unusable bundle fails closed before any
credential is read; and a plain ``http`` override keeps working for local runs.
"""

from __future__ import annotations

import base64
import json
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from gco.services import grafana_rotator, internal_tls

_HTTP_URL = "http://kube-prometheus-stack-grafana.monitoring.svc"


@pytest.fixture(autouse=True)
def _fresh_trust_cache() -> Iterator[None]:
    internal_tls.clear_cache()
    yield
    internal_tls.clear_cache()


def _ca(common_name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """A CA shaped like cert-manager's gco-internal-ca (strict-verification clean)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _leaf(
    ca_key: ec.EllipticCurvePrivateKey, ca_certificate: x509.Certificate, dns_name: str
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """A server leaf like the grafana-tls Certificate: empty subject, critical SAN."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, certificate


def _pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


class _GrafanaApi(BaseHTTPRequestHandler):
    """Just enough of Grafana's admin API for the rotator."""

    def do_GET(self) -> None:
        body = json.dumps({"id": 42, "login": "admin"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received.append(json.loads(self.rfile.read(length)))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output quiet."""
        del format, args


@pytest.fixture
def grafana_tls(tmp_path: Path) -> Iterator[tuple[str, Path, ThreadingHTTPServer]]:
    """An HTTPS 'Grafana' on localhost, serving a leaf issued by a test internal CA."""
    ca_key, ca_certificate = _ca("GCO internal CA")
    leaf_key, leaf_certificate = _leaf(ca_key, ca_certificate, "localhost")
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(_pem(ca_certificate))
    cert_file = tmp_path / "tls.crt"
    cert_file.write_bytes(_pem(leaf_certificate))
    key_file = tmp_path / "tls.key"
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GrafanaApi)
    server.received = []  # type: ignore[attr-defined]
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_address[1]}", ca_file, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# Test fixture: the literal is a fake "current" password used only to build the
# mock Secret payload the rotator reads back; it is not a real credential.
# nosemgrep: hardcoded-password-default-argument
def _secret(user: str = "admin", password: str = "old-password") -> MagicMock:
    secret = MagicMock()
    secret.data = {
        "admin-user": base64.b64encode(user.encode()).decode(),
        "admin-password": base64.b64encode(password.encode()).decode(),
    }
    return secret


# ─── Defaults ───────────────────────────────────────────────────────


def test_default_url_is_the_verified_https_front_door() -> None:
    assert grafana_rotator.DEFAULT_SERVICE_URL == (
        "https://grafana-tls.monitoring.svc.cluster.local:3443"
    )


def test_main_uses_the_https_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAFANA_SERVICE_URL", raising=False)
    with (
        patch.object(grafana_rotator.config, "load_incluster_config"),
        patch.object(grafana_rotator.client, "CoreV1Api", return_value=MagicMock()),
        patch.object(grafana_rotator, "rotate") as rotate,
    ):
        assert grafana_rotator.main() == 0
    assert rotate.call_args.args[3] == grafana_rotator.DEFAULT_SERVICE_URL


# ─── Trust resolution ───────────────────────────────────────────────


def test_http_override_needs_no_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(tmp_path / "absent.crt"))
    assert grafana_rotator.tls_verify_for(_HTTP_URL) is True


def test_https_resolves_to_the_internal_ca_bundle_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _key, certificate = _ca("GCO internal CA")
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(_pem(certificate))
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(ca_file))
    assert grafana_rotator.tls_verify_for("HTTPS://grafana-tls.monitoring.svc:3443") == str(ca_file)


def test_https_defaults_to_the_projected_bundle_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(internal_tls.INTERNAL_CA_FILE_ENV, raising=False)
    with patch.object(grafana_rotator.internal_tls, "internal_ssl_context") as context:
        verify = grafana_rotator.tls_verify_for(grafana_rotator.DEFAULT_SERVICE_URL)
    assert verify == "/var/run/gco/ca/ca.crt"
    context.assert_called_once_with(Path("/var/run/gco/ca/ca.crt"))


def test_https_without_a_bundle_fails_closed_with_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "absent.crt"
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(missing))
    with pytest.raises(internal_tls.InternalTLSError) as raised:
        grafana_rotator.tls_verify_for(grafana_rotator.DEFAULT_SERVICE_URL)
    message = str(raised.value)
    assert grafana_rotator.DEFAULT_SERVICE_URL in message
    assert str(missing) in message
    assert internal_tls.INTERNAL_CA_FILE_ENV in message


def test_https_with_an_unusable_bundle_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    garbage = tmp_path / "ca.crt"
    garbage.write_text("not a certificate\n", encoding="utf-8")
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(garbage))
    with pytest.raises(internal_tls.InternalTLSError, match="not a usable CA"):
        grafana_rotator.tls_verify_for(grafana_rotator.DEFAULT_SERVICE_URL)


def test_rotation_reads_no_credential_when_the_bundle_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(tmp_path / "absent.crt"))
    core = MagicMock()
    with (
        patch.object(grafana_rotator.requests, "get") as get,
        patch.object(grafana_rotator.requests, "put") as put,
        pytest.raises(internal_tls.InternalTLSError),
    ):
        grafana_rotator.rotate(
            core, "monitoring", "kube-prometheus-stack-grafana", "https://g:3443"
        )
    core.read_namespaced_secret.assert_not_called()
    core.patch_namespaced_secret.assert_not_called()
    get.assert_not_called()
    put.assert_not_called()


def test_local_dry_run_uses_kubeconfig_and_the_http_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside the cluster: kubeconfig credentials and a port-forwarded http URL."""
    monkeypatch.setenv("GRAFANA_SERVICE_URL", "http://127.0.0.1:3000/")
    with (
        patch.object(
            grafana_rotator.config,
            "load_incluster_config",
            side_effect=grafana_rotator.config.ConfigException("outside cluster"),
        ),
        patch.object(grafana_rotator.config, "load_kube_config") as kubeconfig,
        patch.object(grafana_rotator.client, "CoreV1Api", return_value=MagicMock()),
        patch.object(grafana_rotator, "rotate") as rotate,
    ):
        assert grafana_rotator.main() == 0
    kubeconfig.assert_called_once_with()
    assert rotate.call_args.args[3] == "http://127.0.0.1:3000"


def test_main_reports_a_missing_bundle_as_a_failed_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(tmp_path / "absent.crt"))
    monkeypatch.delenv("GRAFANA_SERVICE_URL", raising=False)
    core = MagicMock()
    with (
        patch.object(grafana_rotator.config, "load_incluster_config"),
        patch.object(grafana_rotator.client, "CoreV1Api", return_value=core),
    ):
        assert grafana_rotator.main() == 1
    core.read_namespaced_secret.assert_not_called()
    assert "Grafana admin credential rotation failed" in caplog.text
    assert "GCO internal CA" in caplog.text


# ─── The real requests stack against an HTTPS Grafana ───────────────


def test_rotation_over_verified_https_end_to_end(
    grafana_tls: tuple[str, Path, ThreadingHTTPServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, ca_file, server = grafana_tls
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(ca_file))
    core = MagicMock()
    core.read_namespaced_secret.return_value = _secret()

    with patch.object(grafana_rotator, "generate_password", return_value="rotated-pw"):
        grafana_rotator.rotate(core, "monitoring", "kube-prometheus-stack-grafana", url)

    assert server.received == [{"password": "rotated-pw"}]  # type: ignore[attr-defined]
    body = core.patch_namespaced_secret.call_args.args[2]
    assert body["data"]["admin-password"] == base64.b64encode(b"rotated-pw").decode()


def test_a_certificate_from_another_ca_is_refused(
    grafana_tls: tuple[str, Path, ThreadingHTTPServer], tmp_path: Path
) -> None:
    url, _ca_file, _server = grafana_tls
    _key, other = _ca("Some other CA")
    other_bundle = tmp_path / "other-ca.crt"
    other_bundle.write_bytes(_pem(other))
    with pytest.raises(requests.exceptions.SSLError):
        grafana_rotator.get_admin_user_id(url, ("admin", "pw"), str(other_bundle))


def test_a_certificate_for_another_host_is_refused(
    grafana_tls: tuple[str, Path, ThreadingHTTPServer],
) -> None:
    url, ca_file, _server = grafana_tls
    # Same server and CA, dialled by IP: the leaf names only "localhost".
    by_ip = url.replace("localhost", "127.0.0.1")
    with pytest.raises(requests.exceptions.SSLError):
        grafana_rotator.get_admin_user_id(by_ip, ("admin", "pw"), str(ca_file))


def test_the_public_trust_store_is_not_consulted(
    grafana_tls: tuple[str, Path, ThreadingHTTPServer],
) -> None:
    """Default verification (public roots) cannot validate the internal leaf."""
    url, _ca_file, _server = grafana_tls
    with pytest.raises(requests.exceptions.SSLError):
        grafana_rotator.get_admin_user_id(url, ("admin", "pw"))
