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
import time
from typing import Any
from urllib.parse import urlparse

# SSM managed-node ids: EC2 (i-...) and hybrid/managed (mi-...), 8 or 17 hex.
_INSTANCE_RE = re.compile(r"^(i|mi)-[0-9a-f]{8}([0-9a-f]{9})?$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,255}$")
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")

# AWS SSM document that forwards a local port to an arbitrary remote host
# reachable from the managed node (here, the private EKS API endpoint).
REMOTE_HOST_DOCUMENT = "AWS-StartPortForwardingSessionToRemoteHost"
LOCAL_TUNNEL_HOST = "127.0.0.1"
_DEFAULT_READY_TIMEOUT_SECONDS = 30.0
_DEFAULT_READY_POLL_SECONDS = 0.25
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 0.5
_DEFAULT_STOP_TIMEOUT_SECONDS = 5.0


def _validate_instance_id(instance_id: str) -> None:
    if not _INSTANCE_RE.match(instance_id):
        raise ValueError(
            f"Invalid SSM target {instance_id!r}: expected an instance id like i-0123456789abcdef0"
        )


def _validate_host(host: str) -> None:
    if not _HOST_RE.match(host):
        raise ValueError(f"Invalid remote host {host!r}")


def _validate_region(region: str) -> None:
    if not _REGION_RE.match(region):
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


def stop_api_tunnel(
    proc: subprocess.Popen[bytes],
    *,
    wait_seconds: float = _DEFAULT_STOP_TIMEOUT_SECONDS,
) -> tuple[bytes, bytes]:
    """Terminate and reap the complete tunnel process group within bounded waits."""
    if wait_seconds <= 0:
        raise ValueError("wait_seconds must be positive")
    if proc.poll() is None:
        _signal_api_tunnel_tree(proc, force=False, wait_seconds=wait_seconds)
    try:
        return proc.communicate(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        # The AWS wrapper may have exited while session-manager-plugin still
        # owns the listener and inherited pipes, so force the whole group even
        # when proc.poll() now reports a wrapper exit.
        _signal_api_tunnel_tree(proc, force=True, wait_seconds=wait_seconds)
        try:
            return proc.communicate(timeout=wait_seconds)
        except subprocess.TimeoutExpired as exc:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
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
            ) from exc


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
    """Launch an SSM tunnel and return only after its IPv4 listener accepts.

    ``ready_wait_seconds`` is a compatibility-preserving name for the bounded
    listener-readiness timeout. Process exit and timeout paths include captured
    Session Manager diagnostics and always reap the complete process group.
    """
    if ready_wait_seconds < 0:
        raise ValueError("ready_wait_seconds must be non-negative")
    if ready_poll_seconds <= 0:
        raise ValueError("ready_poll_seconds must be positive")
    if connect_timeout_seconds <= 0:
        raise ValueError("connect_timeout_seconds must be positive")

    host = endpoint_host(endpoint)
    cmd = build_remote_host_port_forward_command(instance_id, host, local_port, region)
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

    deadline = time.monotonic() + ready_wait_seconds
    try:
        while True:
            if detail := exited_api_tunnel_detail(proc):
                raise RuntimeError(
                    "SSM port-forwarding session failed to start. Ensure the Session "
                    "Manager plugin is installed and the target instance can reach the "
                    f"cluster endpoint. Details: {detail}"
                )
            try:
                connection = socket.create_connection(
                    (LOCAL_TUNNEL_HOST, local_port),
                    timeout=connect_timeout_seconds,
                )
            except OSError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                connection.close()
                detail = exited_api_tunnel_detail(proc)
                if detail is None:
                    return proc
                raise RuntimeError(f"SSM port-forwarding session exited during readiness: {detail}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = stop_api_tunnel(proc)
                raise RuntimeError(
                    "SSM port-forwarding session did not accept local connections on "
                    f"{LOCAL_TUNNEL_HOST}:{local_port} within {ready_wait_seconds:.1f}s. "
                    f"Last error: {last_error}. SSM output: "
                    f"{_process_output_detail(stdout, stderr)}"
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
