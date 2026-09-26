"""The TLS proxy's keypair wait and its standalone-script contract.

Two properties of ``gco/services/tls_proxy.py`` that the in-cluster HTTPS
rollout depends on:

* **Keypair wait.** Chart pods GCO does not build (OpenCost, Grafana) start
  before the post-Helm Certificates have issued their Secrets, so their
  sidecars mount the Secret ``optional`` and set
  ``TLS_PROXY_KEYPAIR_WAIT_SECONDS``. The proxy then polls for the keypair,
  logs at most once a minute while it waits, binds as soon as the keypair
  loads, raises the original load error once the budget is spent, and exits
  cleanly if the pod is stopped first. The default (0) keeps today's
  fail-closed startup.
* **Standalone script.** The inference monitor ships this file's source to
  managed model pods, which run it as ``python3 tls_proxy.py`` on a stock
  Python image with no GCO package and no uvloop. The module must therefore
  import only the standard library (uvloop stays optional) and work as
  ``__main__`` with no site-packages at all.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gco.services import tls_proxy
from gco.services.tls_proxy import (
    KEYPAIR_WAIT_SECONDS_ENV,
    ProxyConfig,
    TlsProxy,
    load_proxy_config,
    run_proxy,
)

_MODULE_PATH = Path(tls_proxy.__file__).resolve()


def _config(tmp_path: Path, **overrides: Any) -> ProxyConfig:
    config = ProxyConfig(
        host="127.0.0.1",
        port=0,
        upstream_host="127.0.0.1",
        upstream_port=9,
        cert_file=tmp_path / "tls.crt",
        key_file=tmp_path / "tls.key",
        poll_seconds=0.01,
        graceful_shutdown_seconds=1,
    )
    return replace(config, **overrides)


def _write_keypair(cert_file: Path, key_file: Path, common_name: str = "localhost") -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # Certificate last: the proxy reads both files, and a test that writes
    # the pair mid-wait must never expose a certificate without its key.
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# ─── Configuration ─────────────────────────────────────────────────


def test_keypair_wait_defaults_to_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEYPAIR_WAIT_SECONDS_ENV, raising=False)
    assert load_proxy_config().keypair_wait_seconds == 0.0


def test_keypair_wait_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEYPAIR_WAIT_SECONDS_ENV, "1800")
    assert load_proxy_config().keypair_wait_seconds == 1800.0


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_keypair_wait_rejects_negative_or_nonfinite_budgets(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(KEYPAIR_WAIT_SECONDS_ENV, value)
    with pytest.raises(RuntimeError, match=KEYPAIR_WAIT_SECONDS_ENV):
        load_proxy_config()


def test_existing_positional_construction_keeps_working(tmp_path: Path) -> None:
    """The new field has a default, so callers built before it still construct configs."""
    config = ProxyConfig("0.0.0.0", 8443, "127.0.0.1", 9000, tmp_path / "c", tmp_path / "k", 5, 30)
    assert config.keypair_wait_seconds == 0.0


# ─── Startup without a wait: unchanged fail-closed behaviour ───────


async def test_start_without_a_wait_fails_immediately_on_a_missing_keypair(
    tmp_path: Path,
) -> None:
    proxy = TlsProxy(_config(tmp_path))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="does not reference a readable file"):
        await proxy.start()
    assert time.monotonic() - started < 1
    assert proxy._server is None


# ─── Startup with a wait ───────────────────────────────────────────


async def test_start_binds_once_the_keypair_appears(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path, keypair_wait_seconds=30, poll_seconds=0)
    proxy = TlsProxy(config)
    caplog.set_level(logging.INFO, logger=tls_proxy.logger.name)

    async def project_later() -> None:
        await asyncio.sleep(0.35)
        _write_keypair(config.cert_file, config.key_file)

    writer_task = asyncio.create_task(project_later())
    await asyncio.wait_for(proxy.start(), timeout=10)
    await writer_task
    try:
        assert proxy._server is not None
        assert proxy._keypair_digest
        port = proxy._server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=_client_context(), server_hostname="localhost"
        )
        assert writer.get_extra_info("ssl_object") is not None
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.shutdown()

    messages = [record.getMessage() for record in caplog.records]
    waiting = [message for message in messages if "not loadable yet" in message]
    # Several polls failed (poll_seconds=0 is floored to 0.1s), but the
    # waiting line is rate limited to one per interval.
    assert len(waiting) == 1
    assert "does not reference a readable file" in waiting[0]
    assert any(message.startswith("TLS keypair loaded after") for message in messages)


async def test_a_present_keypair_binds_at_once_even_with_a_wait_budget(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path, keypair_wait_seconds=1800)
    _write_keypair(config.cert_file, config.key_file)
    proxy = TlsProxy(config)
    caplog.set_level(logging.INFO, logger=tls_proxy.logger.name)

    await asyncio.wait_for(proxy.start(), timeout=5)
    try:
        assert proxy._server is not None
    finally:
        await proxy.shutdown()

    messages = [record.getMessage() for record in caplog.records]
    assert not any("not loadable yet" in message for message in messages)
    assert not any(message.startswith("TLS keypair loaded after") for message in messages)


async def test_waiting_is_logged_again_once_the_interval_elapses(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tls_proxy, "_KEYPAIR_WAIT_LOG_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(tls_proxy, "_MIN_KEYPAIR_WAIT_POLL_SECONDS", 0.01)
    proxy = TlsProxy(_config(tmp_path, keypair_wait_seconds=0.3))
    caplog.set_level(logging.INFO, logger=tls_proxy.logger.name)

    with pytest.raises(RuntimeError):
        await proxy.start()

    waiting = [record for record in caplog.records if "not loadable yet" in record.getMessage()]
    assert len(waiting) >= 2


async def test_start_raises_the_original_error_once_the_budget_is_spent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proxy = TlsProxy(_config(tmp_path, keypair_wait_seconds=0.3))
    caplog.set_level(logging.INFO, logger=tls_proxy.logger.name)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="does not reference a readable file"):
        await proxy.start()

    assert time.monotonic() - started >= 0.3
    assert proxy._server is None
    assert any(
        record.levelno == logging.ERROR and "No usable TLS keypair" in record.getMessage()
        for record in caplog.records
    )


async def test_an_invalid_keypair_is_waited_on_like_a_missing_one(tmp_path: Path) -> None:
    """A half-written or mismatched pair raises ssl.SSLError; the wait retries it too."""
    config = _config(tmp_path, keypair_wait_seconds=0.2)
    config.cert_file.write_text("not a certificate\n", encoding="utf-8")
    config.key_file.write_text("not a key\n", encoding="utf-8")
    proxy = TlsProxy(config)

    with pytest.raises(ssl.SSLError):
        await proxy.start()


async def test_shutdown_during_the_wait_returns_without_binding(tmp_path: Path) -> None:
    proxy = TlsProxy(_config(tmp_path, keypair_wait_seconds=60, poll_seconds=5))
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, proxy._stop.set)
    started = time.monotonic()

    await asyncio.wait_for(proxy.start(), timeout=5)

    assert time.monotonic() - started < 5
    assert proxy._server is None
    await proxy.shutdown()


async def test_run_proxy_exits_cleanly_when_stopped_during_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestration in run_proxy tolerates a start() that bound nothing."""
    config = _config(tmp_path, keypair_wait_seconds=60, poll_seconds=5)
    created: list[TlsProxy] = []
    real_init = TlsProxy.__init__

    def capture(self: TlsProxy, proxy_config: ProxyConfig) -> None:
        real_init(self, proxy_config)
        created.append(self)
        asyncio.get_running_loop().call_later(0.2, self._stop.set)

    monkeypatch.setattr(TlsProxy, "__init__", capture)
    await asyncio.wait_for(run_proxy(config), timeout=5)

    (proxy,) = created
    assert proxy._server is None
    assert proxy._stop.is_set()


# ─── Standalone script ─────────────────────────────────────────────


def test_module_imports_only_the_standard_library() -> None:
    """Model pods run this file with no gco package and no third-party wheels."""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports cannot resolve when run as a script"
            imported.add((node.module or "").split(".")[0])
    optional = {"uvloop"}
    assert imported - optional <= set(sys.stdlib_module_names) | {"__future__"}, imported
    assert "gco" not in imported


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while data := self.request.recv(4096):
            self.request.sendall(data)


@pytest.fixture
def echo_upstream() -> Iterator[int]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EchoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _spawn_script(
    tmp_path: Path, listen_port: int, upstream_port: int, **env: str
) -> subprocess.Popen[str]:
    """Run the module file as a script the way model pods do, with no site-packages.

    ``-I -S`` drops PYTHONPATH, the user site and site-packages, so neither
    the gco package nor uvloop is importable: exactly the stock image.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GCO_TLS_CERT_FILE": str(tmp_path / "tls.crt"),
        "GCO_TLS_KEY_FILE": str(tmp_path / "tls.key"),
        "TLS_PROXY_HOST": "127.0.0.1",
        "TLS_PROXY_PORT": str(listen_port),
        "TLS_PROXY_UPSTREAM_PORT": str(upstream_port),
        "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS": "1",
        **env,
    }
    return subprocess.Popen(
        [sys.executable, "-I", "-S", str(_MODULE_PATH)],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _tls_echo(port: int, payload: bytes, deadline: float) -> bytes:
    """Retry a TLS round trip through the proxy until it answers or the deadline passes."""
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with (
                socket.create_connection(("127.0.0.1", port), timeout=2) as raw,
                _client_context().wrap_socket(raw, server_hostname="localhost") as tls,
            ):
                tls.sendall(payload)
                return tls.recv(len(payload))
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"TLS proxy never answered on {port}: {last_error}")


def _stop(process: subprocess.Popen[str]) -> tuple[int, str]:
    process.send_signal(signal.SIGTERM)
    try:
        _stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path only
        process.kill()
        _stdout, stderr = process.communicate()
    return process.returncode, stderr


def test_script_serves_tls_on_the_stdlib_loop(tmp_path: Path, echo_upstream: int) -> None:
    _write_keypair(tmp_path / "tls.crt", tmp_path / "tls.key")
    port = _free_port()
    process = _spawn_script(tmp_path, port, echo_upstream)
    try:
        assert _tls_echo(port, b"model-hop", time.monotonic() + 20) == b"model-hop"
    finally:
        returncode, stderr = _stop(process)
    assert returncode == 0, stderr
    # No uvloop without site-packages: the stock-image event loop.
    assert "TLS proxy event loop: asyncio" in stderr


def test_script_waits_for_a_late_keypair_then_serves(tmp_path: Path, echo_upstream: int) -> None:
    port = _free_port()
    process = _spawn_script(
        tmp_path,
        port,
        echo_upstream,
        TLS_PROXY_KEYPAIR_WAIT_SECONDS="60",
        TLS_PROXY_POLL_SECONDS="0.1",
    )
    try:
        time.sleep(0.5)
        assert process.poll() is None, "the proxy must wait, not exit, while the Secret is absent"
        _write_keypair(tmp_path / "tls.crt", tmp_path / "tls.key")
        assert _tls_echo(port, b"late-secret", time.monotonic() + 20) == b"late-secret"
    finally:
        returncode, stderr = _stop(process)
    assert returncode == 0, stderr
    assert "TLS keypair not loadable yet" in stderr


def test_script_stopped_during_the_wait_exits_zero(tmp_path: Path, echo_upstream: int) -> None:
    process = _spawn_script(
        tmp_path,
        _free_port(),
        echo_upstream,
        TLS_PROXY_KEYPAIR_WAIT_SECONDS="600",
        TLS_PROXY_POLL_SECONDS="5",
    )
    time.sleep(0.5)
    started = time.monotonic()
    returncode, stderr = _stop(process)
    assert returncode == 0, stderr
    assert time.monotonic() - started < 10
    assert "Shutdown requested while waiting for the TLS keypair" in stderr


def test_script_fails_once_the_wait_budget_is_spent(tmp_path: Path, echo_upstream: int) -> None:
    process = _spawn_script(
        tmp_path,
        _free_port(),
        echo_upstream,
        TLS_PROXY_KEYPAIR_WAIT_SECONDS="0.5",
        TLS_PROXY_POLL_SECONDS="0.1",
    )
    _stdout, stderr = process.communicate(timeout=30)
    assert process.returncode != 0
    assert "does not reference a readable file" in stderr
