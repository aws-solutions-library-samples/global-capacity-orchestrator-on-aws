"""Transport contract of the Mooncake PD proxy program.

The proxy is shipped to the ``{name}-proxy`` pod as a standalone script and
runs on the upstream vLLM image, so its transport setup happens at import
time from its own environment. Each check here executes a fresh copy of the
program source (never touching the ``gco.services.mooncake_pd_proxy`` module
other suites share) and pins:

- the HTTP library choice: ``httpx2`` when present, else the image's ``httpx``;
- the listener: all interfaces by default, ``PD_PROXY_HOST`` (loopback behind
  the pod's TLS sidecar) when set;
- the backend trust: ``PD_PROXY_CA_FILE`` makes the client trust exactly that
  bundle with hostname verification on, proven against a real local TLS
  server, and a configured-but-missing bundle stops the program at start;
- the relayed encoding: the library's default ``Accept-Encoding`` never
  reaches a backend, while a caller-supplied one does.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import itertools
import json
import logging
import ssl
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import httpx
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import gco.services.mooncake_pd_proxy as shipped_program

PROGRAM_PATH = Path(shipped_program.__file__)
_PROGRAM_ENV = (
    "PD_PROXY_HOST",
    "PD_PROXY_PORT",
    "PD_PROXY_PREFILL_URL",
    "PD_PROXY_DECODE_URL",
    "PD_PROXY_CA_FILE",
)
_copies = itertools.count()


@pytest.fixture
def load_program(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., ModuleType]]:
    """Execute a fresh copy of the program with exactly the given environment."""
    loaded: list[ModuleType] = []

    def load(**env: str) -> ModuleType:
        for name in _PROGRAM_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        spec = importlib.util.spec_from_file_location(
            f"_mooncake_pd_proxy_copy_{next(_copies)}", PROGRAM_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
        return module

    yield load
    for module in loaded:
        asyncio.run(module._client.aclose())


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _make_ca(common_name: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
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
    ca_cert: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A serving certificate for the loopback address the test server binds."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("127.0.0.1"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
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


def _write_pem(path: Path, cert: x509.Certificate) -> Path:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def _server_context(
    tmp_path: Path, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey
) -> ssl.SSLContext:
    cert_file = _write_pem(tmp_path / "tls.crt", cert)
    key_file = tmp_path / "tls.key"
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


class _DecodeServer:
    """A one-route HTTPS stand-in for a decode Service, recording raw requests."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self._context = context
        self.requests: list[str] = []
        self._server: asyncio.Server | None = None
        self.port = 0

    async def __aenter__(self) -> _DecodeServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self._context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError, ConnectionError, ssl.SSLError:
            writer.close()
            return
        self.requests.append(head.decode("latin-1").lower())
        body = b'{"data": []}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()


async def _drain(response: object) -> list[bytes]:
    return [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]


def test_program_prefers_httpx2(load_program) -> None:
    program = load_program()

    assert program.httpx is httpx2
    assert isinstance(program._client, httpx2.AsyncClient)
    assert "accept-encoding" not in program._client.headers


def test_program_falls_back_to_the_image_httpx(load_program, monkeypatch) -> None:
    """The vLLM image ships only httpx; the program must run on it unchanged."""
    monkeypatch.setitem(sys.modules, "httpx2", None)  # ``import httpx2`` -> ImportError

    program = load_program()

    assert program.httpx is httpx
    assert isinstance(program._client, httpx.AsyncClient)
    assert "accept-encoding" not in program._client.headers
    assert isinstance(program._TIMEOUT, httpx.Timeout)


def test_program_binds_all_interfaces_unless_told_otherwise(load_program) -> None:
    assert load_program().HOST == "0.0.0.0"
    # In the pod the listener sits on loopback behind the TLS sidecar.
    assert load_program(PD_PROXY_HOST="127.0.0.1").HOST == "127.0.0.1"


def test_without_a_ca_file_backends_use_the_library_default(load_program) -> None:
    program = load_program()

    assert program.CA_FILE == ""
    assert program._upstream_verify() is True


def test_ca_file_trusts_exactly_that_bundle(load_program, tmp_path) -> None:
    ca_cert, _ca_key = _make_ca("GCO internal CA")
    ca_file = _write_pem(tmp_path / "ca.crt", ca_cert)

    program = load_program(PD_PROXY_CA_FILE=f"  {ca_file}  ")

    assert str(ca_file) == program.CA_FILE
    context = program._upstream_verify()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    # The internal CA is the only anchor: no system store was loaded.
    [anchor] = context.get_ca_certs()
    assert anchor["subject"] == ((("commonName", "GCO internal CA"),),)


def test_missing_ca_file_stops_the_program_at_start(load_program, tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_program(PD_PROXY_CA_FILE=str(tmp_path / "absent-ca.crt"))


async def test_decode_over_verified_https_relays_only_caller_encodings(
    load_program, tmp_path
) -> None:
    ca_cert, ca_key = _make_ca("GCO internal CA")
    leaf_cert, leaf_key = _make_leaf(ca_cert, ca_key)
    server_context = _server_context(tmp_path, leaf_cert, leaf_key)
    ca_file = _write_pem(tmp_path / "ca.crt", ca_cert)

    async with _DecodeServer(server_context) as decode:
        program = load_program(
            PD_PROXY_CA_FILE=str(ca_file),
            PD_PROXY_DECODE_URL=f"https://127.0.0.1:{decode.port}/",
        )
        plain = await program._stream_decode("GET", "/v1/models", headers=[])
        encoded = await program._stream_decode(
            "GET", "/v1/models", headers=[("accept-encoding", "gzip")]
        )

        assert plain.status_code == 200
        assert await _drain(plain) == [b'{"data": []}']
        assert encoded.status_code == 200
        assert await _drain(encoded) == [b'{"data": []}']

    first, second = decode.requests
    assert first.startswith("get /v1/models http/1.1\r\n")
    assert "accept-encoding" not in first
    assert "accept-encoding: gzip\r\n" in second


async def test_decode_signed_by_another_ca_is_refused_as_no_backend(
    load_program, tmp_path, caplog
) -> None:
    trusted_ca, _trusted_key = _make_ca("GCO internal CA")
    rogue_ca, rogue_key = _make_ca("Some other CA")
    leaf_cert, leaf_key = _make_leaf(rogue_ca, rogue_key)
    server_context = _server_context(tmp_path, leaf_cert, leaf_key)
    ca_file = _write_pem(tmp_path / "ca.crt", trusted_ca)

    async with _DecodeServer(server_context) as decode:
        program = load_program(
            PD_PROXY_CA_FILE=str(ca_file),
            PD_PROXY_DECODE_URL=f"https://127.0.0.1:{decode.port}",
        )
        with caplog.at_level(logging.WARNING, logger="mooncake-pd-proxy"):
            response = await program._stream_decode("POST", "/v1/completions", {"stream": False})

    assert response.status_code == program.NO_DECODE_STATUS
    assert json.loads(response.body)["error"]["type"] == "no_decode_backend"
    assert "decode backend unreachable" in caplog.text
    assert "CERTIFICATE_VERIFY_FAILED" in caplog.text
    assert decode.requests == []
