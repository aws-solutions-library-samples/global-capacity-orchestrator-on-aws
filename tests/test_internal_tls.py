"""Tests for the in-cluster client trust helper (gco.services.internal_tls)."""

from __future__ import annotations

import os
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from gco.services import internal_tls
from gco.services.internal_tls import (
    DEFAULT_INTERNAL_CA_FILE,
    INTERNAL_CA_FILE_ENV,
    InternalTLSError,
    internal_ca_file,
    internal_ssl_context,
    verify_for_url,
)

SERVICE_HOST = "cost-monitor.gco-system.svc.cluster.local"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    internal_tls.clear_cache()


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _make_ca(
    common_name: str = "GCO internal CA",
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
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
    return cert, key


def _make_leaf(
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    dns_names: list[str],
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(dns_names[0]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _server_context(
    tmp_path: Path, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey
) -> ssl.SSLContext:
    cert_file = tmp_path / "tls.crt"
    key_file = tmp_path / "tls.key"
    cert_file.write_bytes(_pem(cert))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


def _handshake(client: ssl.SSLContext, server: ssl.SSLContext, server_hostname: str) -> None:
    """Complete a TLS handshake over memory BIOs, raising the client's verdict."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client_side = client.wrap_bio(client_in, client_out, server_hostname=server_hostname)
    server_side = server.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client_side.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        if not server_done:
            try:
                server_side.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError:
                # The server aborts once the client rejects it; the client's
                # own error below is the verdict under test.
                server_done = True
        client_in.write(server_out.read())
        if client_done and server_done:
            return
    raise AssertionError("TLS handshake did not complete")


def test_ca_file_defaults_to_the_projected_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INTERNAL_CA_FILE_ENV, raising=False)
    assert internal_ca_file() == Path(DEFAULT_INTERNAL_CA_FILE)
    monkeypatch.setenv(INTERNAL_CA_FILE_ENV, "   ")
    assert internal_ca_file() == Path(DEFAULT_INTERNAL_CA_FILE)
    monkeypatch.setenv(INTERNAL_CA_FILE_ENV, " /custom/ca.crt ")
    assert internal_ca_file() == Path("/custom/ca.crt")


def test_missing_ca_bundle_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InternalTLSError, match="not readable"):
        internal_ssl_context(tmp_path / "absent.crt")


def test_invalid_ca_bundle_fails_closed(tmp_path: Path) -> None:
    bundle = tmp_path / "ca.crt"
    bundle.write_text("not a certificate\n", encoding="utf-8")
    with pytest.raises(InternalTLSError, match="not a usable CA"):
        internal_ssl_context(bundle)


def test_context_verifies_hostname_and_requires_tls12(tmp_path: Path) -> None:
    ca_cert, _ = _make_ca()
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(ca_cert))
    context = internal_ssl_context(bundle)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    # Only the internal CA is trusted: no system anchors were loaded.
    assert [item["subject"] for item in context.get_ca_certs()] == [
        ((("commonName", "GCO internal CA"),),)
    ]


def test_env_configured_bundle_is_used_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_cert, _ = _make_ca()
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(ca_cert))
    monkeypatch.setenv(INTERNAL_CA_FILE_ENV, str(bundle))
    assert internal_ssl_context() is internal_ssl_context(str(bundle))


def test_context_is_cached_until_the_bundle_changes(tmp_path: Path) -> None:
    first_ca, _ = _make_ca("first CA")
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(first_ca))
    first = internal_ssl_context(bundle)
    assert internal_ssl_context(bundle) is first

    # Kubernetes swaps projected Secret content atomically; any identity
    # change (inode, mtime, size) yields a freshly loaded context.
    second_ca, _ = _make_ca("second rotated CA")
    replacement = tmp_path / "ca.crt.new"
    replacement.write_bytes(_pem(second_ca))
    os.replace(replacement, bundle)
    second = internal_ssl_context(bundle)
    assert second is not first
    assert second.get_ca_certs()[0]["subject"] == ((("commonName", "second rotated CA"),),)

    internal_tls.clear_cache()
    assert internal_ssl_context(bundle) is not second


def test_verify_for_url_uses_the_internal_ca_only_for_https(tmp_path: Path) -> None:
    ca_cert, _ = _make_ca()
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(ca_cert))
    assert verify_for_url(f"https://{SERVICE_HOST}:8443", bundle) is internal_ssl_context(bundle)
    assert verify_for_url(f"HTTPS://{SERVICE_HOST}:8443", bundle) is internal_ssl_context(bundle)
    # Plain-HTTP overrides (local runs, CI doubles) need no bundle at all.
    assert verify_for_url("http://127.0.0.1:8080", tmp_path / "absent.crt") is True


def test_handshake_accepts_a_leaf_for_the_dialled_service(tmp_path: Path) -> None:
    ca_cert, ca_key = _make_ca()
    leaf, leaf_key = _make_leaf(ca_cert, ca_key, [SERVICE_HOST, "cost-monitor.gco-system.svc"])
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(ca_cert))
    _handshake(
        internal_ssl_context(bundle), _server_context(tmp_path, leaf, leaf_key), SERVICE_HOST
    )


def test_handshake_rejects_a_leaf_for_another_service(tmp_path: Path) -> None:
    ca_cert, ca_key = _make_ca()
    leaf, leaf_key = _make_leaf(ca_cert, ca_key, ["opencost-tls.monitoring.svc.cluster.local"])
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(ca_cert))
    with pytest.raises(ssl.SSLCertVerificationError, match=r"Hostname mismatch|hostname"):
        _handshake(
            internal_ssl_context(bundle), _server_context(tmp_path, leaf, leaf_key), SERVICE_HOST
        )


def test_handshake_rejects_a_leaf_from_a_foreign_ca(tmp_path: Path) -> None:
    trusted_ca, _ = _make_ca()
    foreign_ca, foreign_key = _make_ca("some other CA")
    leaf, leaf_key = _make_leaf(foreign_ca, foreign_key, [SERVICE_HOST])
    bundle = tmp_path / "ca.crt"
    bundle.write_bytes(_pem(trusted_ca))
    with pytest.raises(ssl.SSLCertVerificationError, match="unable to get local issuer"):
        _handshake(
            internal_ssl_context(bundle), _server_context(tmp_path, leaf, leaf_key), SERVICE_HOST
        )
