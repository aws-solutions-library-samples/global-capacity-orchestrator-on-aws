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
  reaches a backend, while a caller-supplied one does;
- one client per upstream call, with keep-alive off, taking its trust from a
  context cached on the CA file's identity, so a rotated CA is trusted from
  the next call without a restart and a CA that breaks after start answers
  like an unreachable decode backend;
- header values that cross both hops byte for byte; and
- the grammar: the program parses as the image's Python 3.12.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import ipaddress
import itertools
import json
import logging
import ssl
import sys
from collections.abc import Callable
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
def load_program(monkeypatch: pytest.MonkeyPatch) -> Callable[..., ModuleType]:
    """Execute a fresh copy of the program with exactly the given environment."""

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
        return module

    return load


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

    def __init__(self, context: ssl.SSLContext, *, extra_headers: bytes = b"") -> None:
        self._context = context
        self._extra_headers = extra_headers
        self.requests: list[str] = []
        self.raw_requests: list[bytes] = []
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
        self.raw_requests.append(head)
        self.requests.append(head.decode("latin-1").lower())
        body = b'{"data": []}'
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + self._extra_headers
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()


async def _drain(response: object) -> list[bytes]:
    return [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]


def _one_client(program: ModuleType) -> httpx2.AsyncClient | httpx.AsyncClient:
    client = program._new_client()
    asyncio.run(client.aclose())
    return client


def test_program_prefers_httpx2(load_program) -> None:
    program = load_program()

    assert program.httpx is httpx2
    client = _one_client(program)
    assert isinstance(client, httpx2.AsyncClient)
    assert "accept-encoding" not in client.headers


def test_program_falls_back_to_the_image_httpx(load_program, monkeypatch) -> None:
    """The vLLM image ships only httpx; the program must run on it unchanged."""
    monkeypatch.setitem(sys.modules, "httpx2", None)  # ``import httpx2`` -> ImportError

    program = load_program()

    assert program.httpx is httpx
    client = _one_client(program)
    assert isinstance(client, httpx.AsyncClient)
    assert "accept-encoding" not in client.headers
    assert isinstance(program._TIMEOUT, httpx.Timeout)


def test_the_program_parses_as_the_image_python() -> None:
    """The vLLM image runs Python 3.12; 3.14-only syntax would stop the pod at start."""
    ast.parse(PROGRAM_PATH.read_text(encoding="utf-8"), feature_version=(3, 12))


def test_each_call_gets_a_client_without_keep_alive(load_program, monkeypatch) -> None:
    """kube-proxy balances connections, so pooling would pin calls to one pod."""
    program = load_program()
    built: list[dict] = []
    real_client = program.httpx.AsyncClient

    def spy(**kwargs: object) -> object:
        built.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(program.httpx, "AsyncClient", spy)
    first, second = program._new_client(), program._new_client()
    asyncio.run(first.aclose())
    asyncio.run(second.aclose())

    assert first is not second
    assert [kwargs["limits"].max_keepalive_connections for kwargs in built] == [0, 0]
    assert all(kwargs["timeout"] is program._TIMEOUT for kwargs in built)


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


def _rotate(path: Path, cert: x509.Certificate) -> None:
    """Swap the bundle the way a projected Secret update does: a new file, renamed in."""
    staged = path.with_name(f".{path.name}.new")
    _write_pem(staged, cert)
    staged.replace(path)


def test_the_trust_context_is_cached_until_the_ca_file_changes(load_program, tmp_path) -> None:
    first_ca, _first_key = _make_ca("GCO internal CA")
    second_ca, _second_key = _make_ca("GCO internal CA (rotated)")
    ca_file = _write_pem(tmp_path / "ca.crt", first_ca)
    program = load_program(PD_PROXY_CA_FILE=str(ca_file))

    loaded = program._upstream_verify()
    # An unchanged bundle is a stat, not a reload.
    assert program._upstream_verify() is loaded

    _rotate(ca_file, second_ca)
    rotated = program._upstream_verify()

    assert rotated is not loaded
    [anchor] = rotated.get_ca_certs()
    assert anchor["subject"] == ((("commonName", "GCO internal CA (rotated)"),),)
    assert program._upstream_verify() is rotated


async def test_a_rotated_ca_is_trusted_from_the_next_call(load_program, tmp_path) -> None:
    """Decode re-issued under a new CA: refused before the bundle rotates, served after."""
    old_ca, _old_key = _make_ca("GCO internal CA")
    new_ca, new_key = _make_ca("GCO internal CA (rotated)")
    leaf_cert, leaf_key = _make_leaf(new_ca, new_key)
    server_context = _server_context(tmp_path, leaf_cert, leaf_key)
    ca_file = _write_pem(tmp_path / "ca.crt", old_ca)

    async with _DecodeServer(server_context) as decode:
        program = load_program(
            PD_PROXY_CA_FILE=str(ca_file),
            PD_PROXY_DECODE_URL=f"https://127.0.0.1:{decode.port}",
        )
        before = await program._stream_decode("GET", "/v1/models", headers=[])
        _rotate(ca_file, new_ca)
        after = await program._stream_decode("GET", "/v1/models", headers=[])

        assert before.status_code == program.NO_DECODE_STATUS
        assert after.status_code == 200
        assert await _drain(after) == [b'{"data": []}']

    assert len(decode.requests) == 1


@pytest.mark.parametrize("damage", ["removed", "not-a-ca"])
async def test_a_ca_that_breaks_after_start_fails_closed(
    load_program, tmp_path, caplog, damage
) -> None:
    ca_cert, _ca_key = _make_ca("GCO internal CA")
    ca_file = _write_pem(tmp_path / "ca.crt", ca_cert)
    program = load_program(
        PD_PROXY_CA_FILE=str(ca_file),
        PD_PROXY_PREFILL_URL="https://127.0.0.1:9",
        PD_PROXY_DECODE_URL="https://127.0.0.1:9",
    )
    if damage == "removed":
        ca_file.unlink()
    else:
        ca_file.write_text("not a certificate\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="mooncake-pd-proxy"):
        primed = await program._prime_prefill("/v1/completions", {"prompt": "hi"})
        response = await program._stream_decode("POST", "/v1/completions", {"stream": False})

    assert primed == {}
    assert response.status_code == program.NO_DECODE_STATUS
    assert json.loads(response.body)["error"]["type"] == "no_decode_backend"
    assert "prefill priming failed" in caplog.text
    assert "decode backend trust is unavailable" in caplog.text


async def test_header_values_cross_both_hops_byte_for_byte(load_program, tmp_path) -> None:
    """Over a real HTTPS decode: both failures were 500s, and latin-1 was transcoded.

    A non-ASCII request value failed in the client's ASCII header encoding,
    and a UTF-8 response value outside latin-1 in Starlette's latin-1 one.
    """
    ca_cert, ca_key = _make_ca("GCO internal CA")
    leaf_cert, leaf_key = _make_leaf(ca_cert, ca_key)
    server_context = _server_context(tmp_path, leaf_cert, leaf_key)
    ca_file = _write_pem(tmp_path / "ca.crt", ca_cert)
    model_name = "gpt—日本".encode()
    user_agent = "café-client/1.0".encode()

    async with _DecodeServer(
        server_context, extra_headers=b"X-Model-Name: " + model_name + b"\r\n"
    ) as decode:
        program = load_program(
            PD_PROXY_CA_FILE=str(ca_file),
            PD_PROXY_DECODE_URL=f"https://127.0.0.1:{decode.port}",
        )
        response = await program._stream_decode(
            "GET", "/v1/models", headers=[(b"user-agent", user_agent)]
        )
        sent: list[dict] = []

        async def receive() -> dict:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        await response(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "path": "/v1/models",
                "headers": [],
            },
            receive,
            send,
        )

    [head] = decode.raw_requests
    assert b"\r\nuser-agent: " + user_agent + b"\r\n" in head
    assert (b"x-model-name", model_name) in sent[0]["headers"]
    assert (b"content-type", b"application/json") in sent[0]["headers"]
    assert b"".join(message.get("body", b"") for message in sent[1:]) == b'{"data": []}'
