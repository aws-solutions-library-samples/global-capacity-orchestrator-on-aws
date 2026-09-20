"""Hot-reloading TLS termination proxy for ALB-facing GCO API pods.

The application container listens on pod-loopback HTTP. A second container
using this module exposes only HTTPS to the pod network and forwards decrypted
bytes over loopback. Certificate files are treated as a pluggable projection:
today cert-manager supplies a Secret volume; a future Kubernetes PodCertificate
volume can replace it without changing the proxy or Service topology.

Rotation never touches the listening socket. The listener is bound once with a
dispatcher ``SSLContext`` that carries no keypair; its ``sni_callback`` points
every handshake at the currently active, fully validated keypair context.
OpenSSL runs that callback for every ClientHello -- with ``server_name=None``
when the peer sends no SNI, which is how the ALB connects to its targets -- so
activating a rotated leaf is a single attribute assignment: no accept gap while
a replacement listener binds, no rebind that could fail, and established
streams keep the session they already negotiated.

The process runs on uvloop when it is importable (the service images ship it
for uvicorn) and on the stdlib selector loop otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import signal
import ssl
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TLS_CERT_FILE_ENV = "GCO_TLS_CERT_FILE"
TLS_KEY_FILE_ENV = "GCO_TLS_KEY_FILE"
DEFAULT_CERT_FILE = "/var/run/gco/tls/tls.crt"
DEFAULT_KEY_FILE = "/var/run/gco/tls/tls.key"
_BUFFER_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProxyConfig:
    """Validated listener, upstream, rotation, and shutdown settings."""

    host: str
    port: int
    upstream_host: str
    upstream_port: int
    cert_file: Path
    key_file: Path
    poll_seconds: float
    graceful_shutdown_seconds: float


def _positive_port(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= 65535:
        raise RuntimeError(f"{name} must be between 1 and 65535")
    return value


def _non_negative_number(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be a finite non-negative number")
    return value


def load_proxy_config() -> ProxyConfig:
    """Resolve process configuration, failing closed on an incomplete keypair."""
    cert_value = os.getenv(TLS_CERT_FILE_ENV, DEFAULT_CERT_FILE).strip()
    key_value = os.getenv(TLS_KEY_FILE_ENV, DEFAULT_KEY_FILE).strip()
    if not cert_value or not key_value:
        raise RuntimeError(f"{TLS_CERT_FILE_ENV} and {TLS_KEY_FILE_ENV} must not be empty")
    cert_file = Path(cert_value)
    key_file = Path(key_value)
    return ProxyConfig(
        host=os.getenv("TLS_PROXY_HOST", "0.0.0.0"),  # pod listener
        port=_positive_port("TLS_PROXY_PORT", 8443),
        upstream_host=os.getenv("TLS_PROXY_UPSTREAM_HOST", "127.0.0.1"),
        upstream_port=_positive_port("TLS_PROXY_UPSTREAM_PORT", 9000),
        cert_file=cert_file,
        key_file=key_file,
        poll_seconds=_non_negative_number("TLS_PROXY_POLL_SECONDS", 5.0),
        graceful_shutdown_seconds=_non_negative_number("GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", 30.0),
    )


def _keypair_digest(config: ProxyConfig) -> str:
    """Return a digest of readable certificate material without logging it."""
    digest = hashlib.sha256()
    for variable, path in (
        (TLS_CERT_FILE_ENV, config.cert_file),
        (TLS_KEY_FILE_ENV, config.key_file),
    ):
        if not path.is_file() or not os.access(path, os.R_OK):
            raise RuntimeError(f"{variable} does not reference a readable file: {path}")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _server_context() -> ssl.SSLContext:
    """TLS 1.2+ server context with no keypair; handshakes fail closed until one is loaded."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _ssl_context(config: ProxyConfig) -> tuple[ssl.SSLContext, str]:
    """Build a validated keypair context and return it with its keypair digest."""
    digest = _keypair_digest(config)
    context = _server_context()
    context.load_cert_chain(config.cert_file, config.key_file)
    return context, digest


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(_BUFFER_BYTES):
        writer.write(data)
        await writer.drain()


class TlsProxy:
    """TLS-only TCP proxy with in-place certificate rotation and graceful stream drain."""

    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self._server: asyncio.Server | None = None
        # Replaced by the projected keypair in start(); until then the
        # keypair-less placeholder makes any handshake fail closed.
        self._active_context: ssl.SSLContext = _server_context()
        self._keypair_digest = ""
        self._stop = asyncio.Event()
        self._connections: set[asyncio.Task[Any]] = set()

    def _select_context(
        self,
        ssl_object: ssl.SSLObject,
        server_name: str | None,
        dispatcher: ssl.SSLContext,
    ) -> None:
        """Point one handshake at the keypair active when its ClientHello arrived.

        The ALB sends no SNI, so ``server_name`` is ``None`` in production; the
        active leaf serves every peer regardless of the name presented.
        """
        del server_name, dispatcher
        ssl_object.context = self._active_context

    async def _handle_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.config.upstream_host,
                self.config.upstream_port,
            )
            pumps = {
                asyncio.create_task(_pump(client_reader, upstream_writer)),
                asyncio.create_task(_pump(upstream_reader, client_writer)),
            }
            _done, pending = await asyncio.wait(
                pumps,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        except (ConnectionError, OSError) as exc:
            logger.debug("TLS proxy connection ended before upstream was ready: %s", exc)
        finally:
            if upstream_writer is not None:
                await _close_writer(upstream_writer)
            await _close_writer(client_writer)
            if task is not None:
                self._connections.discard(task)

    async def start(self) -> None:
        """Bind the ALB listener once; every handshake is routed to the active keypair."""
        self._active_context, self._keypair_digest = _ssl_context(self.config)
        dispatcher = _server_context()
        dispatcher.sni_callback = self._select_context
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.config.host,
            self.config.port,
            ssl=dispatcher,
        )
        logger.info(
            "TLS proxy listening on https://%s:%d; upstream=http://%s:%d",
            self.config.host,
            self.config.port,
            self.config.upstream_host,
            self.config.upstream_port,
        )

    def activate_keypair(self, context: ssl.SSLContext, digest: str) -> None:
        """Serve a validated rotated keypair to new handshakes.

        The listener and every established stream are untouched: only the
        context the next ClientHello is routed to changes.
        """
        self._active_context = context
        self._keypair_digest = digest
        logger.info("Activated the rotated workload certificate for new TLS handshakes")

    async def watch_certificates(self) -> None:
        """Activate atomically projected certificate changes without dropping streams."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_seconds)
            except TimeoutError:
                # The polling interval elapsed normally; inspect the projected
                # keypair and activate it only when its content digest changed.
                try:
                    context, digest = _ssl_context(self.config)
                except OSError, RuntimeError, ssl.SSLError:
                    logger.exception("Rejected an unreadable or invalid rotated TLS keypair")
                    continue
                if digest != self._keypair_digest:
                    self.activate_keypair(context, digest)
            else:
                break

    async def shutdown(self) -> None:
        """Stop accepting connections and drain established streams."""
        self._stop.set()
        server = self._server
        if server is not None:
            server.close()

        active = set(self._connections)
        if active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active, return_exceptions=True),
                    timeout=self.config.graceful_shutdown_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "Cancelling %d TLS proxy connection(s) after the %.1fs drain budget",
                    len(active),
                    self.config.graceful_shutdown_seconds,
                )
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)

        if server is not None:
            await server.wait_closed()


async def run_proxy(config: ProxyConfig | None = None) -> None:
    """Run until SIGTERM/SIGINT, then drain accepted proxy connections."""
    proxy = TlsProxy(config or load_proxy_config())
    loop = asyncio.get_running_loop()
    logger.info("TLS proxy event loop: %s.%s", type(loop).__module__, type(loop).__qualname__)
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError), warnings.catch_warnings():
            # uvloop 0.22 validates the callback with asyncio.iscoroutinefunction,
            # which Python 3.14 deprecates. The warning is attributed to this
            # call site and, with the module running as __main__, would print
            # into every sidecar's log at startup; nothing here can act on it.
            warnings.filterwarnings(
                "ignore",
                message=r"'asyncio\.iscoroutinefunction' is deprecated",
                category=DeprecationWarning,
            )
            loop.add_signal_handler(signum, proxy._stop.set)

    await proxy.start()
    watcher = asyncio.create_task(proxy.watch_certificates())
    stop_waiter = asyncio.create_task(proxy._stop.wait())
    done, _pending = await asyncio.wait(
        {watcher, stop_waiter},
        return_when=asyncio.FIRST_COMPLETED,
    )
    watcher_error: BaseException | None = None
    if watcher in done and not watcher.cancelled():
        watcher_error = watcher.exception()
        proxy._stop.set()
    stop_waiter.cancel()
    watcher.cancel()
    await asyncio.gather(stop_waiter, watcher, return_exceptions=True)
    await proxy.shutdown()
    if watcher_error is not None:
        raise watcher_error


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Prefer uvloop (shipped in the service images for uvicorn); else the stdlib loop."""
    try:
        import uvloop
    except ImportError:
        return None
    return uvloop.new_event_loop


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_proxy(), loop_factory=_loop_factory())


if __name__ == "__main__":
    main()
