"""SSM Session Manager tunnel helpers for reaching a private EKS API endpoint.

GCO clusters default to a PRIVATE EKS API endpoint (``eks_cluster.endpoint_access
= "PRIVATE"``), so ``kubectl`` — and therefore ``kubectl port-forward`` to
Grafana — cannot reach the API server from a laptop outside the VPC. This module
builds an ``aws ssm start-session`` port-forwarding tunnel through an SSM-managed
instance in the VPC to the cluster's API endpoint, giving kubectl a
``https://127.0.0.1:<port>`` server to talk to.

The command builder is pure and validated (list form, never a shell string) so
it is fully unit-testable; :func:`start_api_tunnel` is the thin runtime wrapper
that launches it as a background process and waits for its IPv4 listener.

Requires the Session Manager plugin on the local machine
(https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import weakref
from typing import IO, Any
from urllib.parse import urlparse

from gco.aws_names import is_aws_region_name

# SSM managed-node ids: EC2 (i-...) and hybrid/managed (mi-...), 8 or 17 hex.
_INSTANCE_RE = re.compile(r"^(i|mi)-[0-9a-f]{8}([0-9a-f]{9})?$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,255}$")

# AWS SSM document that forwards a local port to an arbitrary remote host
# reachable from the managed node (here, the private EKS API endpoint).
REMOTE_HOST_DOCUMENT = "AWS-StartPortForwardingSessionToRemoteHost"
LOCAL_TUNNEL_HOST = "127.0.0.1"
_DEFAULT_READY_TIMEOUT_SECONDS = 30.0
_DEFAULT_READY_POLL_SECONDS = 0.25
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 0.5
_DEFAULT_STOP_TIMEOUT_SECONDS = 5.0

#: What the Session Manager plugin prints once its local listener is open
#: (session-manager-plugin sessionmanagerplugin/session/portsession: "Port %s
#: opened for sessionId %s."). It prints it only after its own listen succeeded,
#: so seeing it for the requested port proves the listener is the tunnel's: a
#: process that already held the port makes the plugin's listen fail instead.
_PORT_OPENED_RE = re.compile(rb"Port (\d+) opened for sessionId ")
#: Newest bytes of each tunnel output stream kept for diagnostics.
_OUTPUT_TAIL_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def _validate_instance_id(instance_id: str) -> None:
    if not _INSTANCE_RE.fullmatch(instance_id):
        raise ValueError(
            f"Invalid SSM target {instance_id!r}: expected an instance id like i-0123456789abcdef0"
        )


def _validate_host(host: str) -> None:
    if not _HOST_RE.fullmatch(host):
        raise ValueError(f"Invalid remote host {host!r}")


def _validate_region(region: str) -> None:
    if not is_aws_region_name(region):
        raise ValueError(f"Invalid AWS region {region!r}: expected format like 'us-east-1'")


def _validate_port(port: int | str, *, what: str) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {what} {port!r}: must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"Invalid {what} {value}: must be between 1 and 65535")
    return value


def endpoint_host(endpoint: str) -> str:
    """Extract the bare hostname from an EKS endpoint URL (or a bare host).

    ``https://ABC123.gr7.us-east-1.eks.amazonaws.com`` -> ``ABC123.gr7...``.

    Uses ``netloc`` rather than ``urlparse(...).hostname`` because the latter
    lowercases the host — EKS endpoint IDs are case-sensitive in the server
    certificate SAN, and this value becomes kubectl's ``--tls-server-name``.
    """
    netloc = urlparse(endpoint).netloc if "://" in endpoint else endpoint
    # Strip any userinfo and :port, preserving original case.
    host = netloc.split("@")[-1].split(":")[0]
    if not host:
        raise ValueError(f"Could not parse a hostname from endpoint {endpoint!r}")
    return host


def build_remote_host_port_forward_command(
    instance_id: str,
    remote_host: str,
    local_port: int | str,
    region: str,
    remote_port: int | str = 443,
) -> list[str]:
    """Build a validated ``aws ssm start-session`` argv for a remote-host tunnel.

    Forwards ``127.0.0.1:<local_port>`` through ``instance_id`` to
    ``remote_host:<remote_port>`` (default 443, the EKS API port).
    """
    _validate_instance_id(instance_id)
    _validate_host(remote_host)
    _validate_region(region)
    local = _validate_port(local_port, what="local port")
    remote = _validate_port(remote_port, what="remote port")

    parameters = json.dumps(
        {
            "host": [remote_host],
            "portNumber": [str(remote)],
            "localPortNumber": [str(local)],
        }
    )
    return [
        "aws",
        "ssm",
        "start-session",
        "--target",
        instance_id,
        "--region",
        region,
        "--document-name",
        REMOTE_HOST_DOCUMENT,
        "--parameters",
        parameters,
    ]


def _process_output_detail(stdout: bytes | None, stderr: bytes | None) -> str:
    output = stderr or stdout
    return output.decode("utf-8", "replace").strip()[:2000] if output else "(no output)"


class _TunnelOutput:
    """Drain a tunnel's stdout and stderr for as long as the process tree holds them.

    Nothing else reads the pipes while the session runs, and the plugin writes
    a line for every connection it accepts, so an unread pipe fills and the
    plugin blocks on its next write: the tunnel stalls with its listener still
    accepting connections. A daemon thread per stream reads it to EOF, keeps the
    newest bytes for diagnostics, and sets ``opened`` when the plugin reports
    its listener on ``local_port``.
    """

    def __init__(self, proc: subprocess.Popen[bytes], local_port: int | None) -> None:
        self.opened = threading.Event()
        self._local_port = local_port
        self._lock = threading.Lock()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._threads = [
            threading.Thread(
                target=self._drain,
                args=(stream, buffer),
                name="gco-ssm-tunnel-output",
                daemon=True,
            )
            for stream, buffer in ((proc.stdout, self._stdout), (proc.stderr, self._stderr))
            if stream is not None
        ]
        for thread in self._threads:
            thread.start()

    def _drain(self, stream: IO[bytes], buffer: bytearray) -> None:
        read = getattr(stream, "read1", None) or stream.read
        try:
            while chunk := read(_READ_CHUNK_BYTES):
                with self._lock:
                    buffer += chunk
                    if buffer is self._stdout and not self.opened.is_set():
                        self._note_listener(buffer)
                    del buffer[: max(0, len(buffer) - _OUTPUT_TAIL_BYTES)]
        except OSError, ValueError:  # the stream was closed under the reader
            return

    def _note_listener(self, stdout: bytearray) -> None:
        for match in _PORT_OPENED_RE.finditer(stdout):
            if int(match.group(1)) == self._local_port:
                self.opened.set()
                return

    def join(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for both streams to reach EOF."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def output(self) -> tuple[bytes, bytes]:
        """The newest captured stdout and stderr bytes."""
        with self._lock:
            return bytes(self._stdout), bytes(self._stderr)


_OUTPUTS: weakref.WeakKeyDictionary[subprocess.Popen[bytes], _TunnelOutput] = (
    weakref.WeakKeyDictionary()
)
_OUTPUTS_LOCK = threading.Lock()


def _tunnel_output(proc: subprocess.Popen[bytes], local_port: int | None = None) -> _TunnelOutput:
    """The output drain of ``proc``, started on first use."""
    with _OUTPUTS_LOCK:
        output = _OUTPUTS.get(proc)
        if output is None:
            output = _OUTPUTS[proc] = _TunnelOutput(proc, local_port)
        return output


def _local_port_accepts(local_port: int, timeout: float) -> bool:
    """Whether something accepts TCP connections on the tunnel's local address."""
    try:
        connection = socket.create_connection((LOCAL_TUNNEL_HOST, local_port), timeout=timeout)
    except OSError:
        return False
    connection.close()
    return True


def _signal_api_tunnel_tree(
    proc: subprocess.Popen[bytes],
    *,
    force: bool,
    wait_seconds: float,
) -> None:
    """Signal the process group containing the AWS CLI and Session Manager plugin."""
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        (proc.kill if force else proc.terminate)()
        return

    if os.name == "nt":
        taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if taskkill is not None:
            command = [taskkill, "/PID", str(pid), "/T"]
            if force:
                command.append("/F")
            try:
                result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - resolved Windows utility and numeric child PID
                    command,
                    capture_output=True,
                    check=False,
                    timeout=wait_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError, subprocess.TimeoutExpired:
                pass
            else:
                if result.returncode == 0:
                    return
        # Preserve the tracked root PID through the graceful wait. If graceful
        # tree termination fails, stop_api_tunnel escalates to /T /F before
        # any wrapper-only fallback that could orphan the plugin.
        if not force:
            return
        proc.kill()
        return

    try:
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    except OSError:
        if proc.poll() is None:
            (proc.kill if force else proc.terminate)()


def _exited_with_output(
    proc: subprocess.Popen[bytes],
    output: _TunnelOutput,
    timeout: float,
) -> bool:
    """Wait up to ``timeout`` for ``proc`` to exit and its pipes to reach EOF."""
    deadline = time.monotonic() + timeout
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return output.join(max(0.0, deadline - time.monotonic()))


def stop_api_tunnel(
    proc: subprocess.Popen[bytes],
    *,
    wait_seconds: float = _DEFAULT_STOP_TIMEOUT_SECONDS,
) -> tuple[bytes, bytes]:
    """Terminate and reap the complete tunnel process group within bounded waits.

    Returns the newest captured ``(stdout, stderr)`` bytes.
    """
    if wait_seconds <= 0:
        raise ValueError("wait_seconds must be positive")
    output = _tunnel_output(proc)
    if proc.poll() is None:
        _signal_api_tunnel_tree(proc, force=False, wait_seconds=wait_seconds)
    if _exited_with_output(proc, output, wait_seconds):
        return output.output()
    # The AWS wrapper may have exited while session-manager-plugin still owns
    # the listener and the inherited pipes, so force the whole group even when
    # proc.poll() now reports a wrapper exit.
    _signal_api_tunnel_tree(proc, force=True, wait_seconds=wait_seconds)
    if _exited_with_output(proc, output, wait_seconds):
        return output.output()
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired as wait_exc:
            raise RuntimeError(
                "SSM tunnel process tree did not exit after forced termination"
            ) from wait_exc
    raise RuntimeError(
        "SSM tunnel process tree retained inherited output pipes after forced termination"
    )


def exited_api_tunnel_detail(proc: subprocess.Popen[bytes]) -> str | None:
    """Return diagnostics for an exited tunnel, or ``None`` while it is running."""
    returncode = proc.poll()
    if returncode is None:
        return None
    try:
        stdout, stderr = stop_api_tunnel(proc)
    except RuntimeError as exc:
        return f"exit code {returncode}; process-tree cleanup failed: {exc}"
    return f"exit code {returncode}; {_process_output_detail(stdout, stderr)}"


def start_api_tunnel(
    instance_id: str,
    endpoint: str,
    local_port: int,
    region: str,
    *,
    ready_wait_seconds: float = _DEFAULT_READY_TIMEOUT_SECONDS,
    ready_poll_seconds: float = _DEFAULT_READY_POLL_SECONDS,
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> subprocess.Popen[bytes]:
    """Launch an SSM tunnel and return once its own listener accepts connections.

    A port that already accepts connections is refused before anything starts:
    kubectl would reach that process, not the tunnel. The tunnel counts as
    ready only after the Session Manager plugin reports opening its listener on
    ``local_port`` and the port accepts a connection; a listener alone proves
    nothing about who owns it. The process tree's output is drained for its
    whole life (see :class:`_TunnelOutput`).

    ``ready_wait_seconds`` is a compatibility-preserving name for the bounded
    readiness timeout. Process exit and timeout paths include captured Session
    Manager diagnostics and always reap the complete process group.
    """
    if ready_wait_seconds < 0:
        raise ValueError("ready_wait_seconds must be non-negative")
    if ready_poll_seconds <= 0:
        raise ValueError("ready_poll_seconds must be positive")
    if connect_timeout_seconds <= 0:
        raise ValueError("connect_timeout_seconds must be positive")

    host = endpoint_host(endpoint)
    cmd = build_remote_host_port_forward_command(instance_id, host, local_port, region)
    if _local_port_accepts(local_port, connect_timeout_seconds):
        raise RuntimeError(
            f"Another process already listens on {LOCAL_TUNNEL_HOST}:{local_port}, the "
            "SSM tunnel's local port, so kubectl would reach it instead of the cluster. "
            "Stop it (an earlier tunnel or port-forward, for example) and retry."
        )
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "start_new_session": os.name == "posix",
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit - argv validated by builder; list form, no shell=True
            cmd,
            **popen_kwargs,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "AWS CLI not found. Install the AWS CLI and the Session Manager plugin."
        ) from exc

    output = _tunnel_output(proc, local_port)
    deadline = time.monotonic() + ready_wait_seconds
    last_error = "the Session Manager plugin has not reported its listener yet"
    try:
        while True:
            if detail := exited_api_tunnel_detail(proc):
                raise RuntimeError(
                    "SSM port-forwarding session failed to start. Ensure the Session "
                    "Manager plugin is installed, the local port is free, and the target "
                    f"instance can reach the cluster endpoint. Details: {detail}"
                )
            if output.opened.is_set():
                if _local_port_accepts(local_port, connect_timeout_seconds):
                    detail = exited_api_tunnel_detail(proc)
                    if detail is None:
                        return proc
                    raise RuntimeError(
                        f"SSM port-forwarding session exited during readiness: {detail}"
                    )
                last_error = "the reported listener did not accept a connection"

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = stop_api_tunnel(proc)
                raise RuntimeError(
                    "SSM port-forwarding session did not open its listener on "
                    f"{LOCAL_TUNNEL_HOST}:{local_port} within {ready_wait_seconds:.1f}s "
                    f"({last_error}). SSM output: {_process_output_detail(stdout, stderr)}"
                )
            time.sleep(min(ready_poll_seconds, remaining))
    except BaseException as exc:
        try:
            stop_api_tunnel(proc)
        except Exception as cleanup_exc:
            raise RuntimeError(
                f"SSM tunnel startup failed and process-tree cleanup also failed: {cleanup_exc}"
            ) from exc
        raise
