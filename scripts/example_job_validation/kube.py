"""Cluster access for live harnesses: SSM tunnel plus kubectl execution.

The historical example harness keeps its default behavior when no explicit
kubeconfig is supplied. Sibling harnesses can instead pass an isolated path;
AWS CLI and kubectl receive ``--kubeconfig`` and every nested ``gco`` process
receives ``KUBECONFIG``, so those runs never rewrite ``~/.kube/config``.

A session that reaches the API through an SSM tunnel also keeps the tunnel
carrying traffic. A live run lost seven examples to a Session Manager session
that stalled for over an hour: the local plugin kept accepting connections,
but no TLS handshake completed, so every kubectl call failed with ``TLS
handshake timeout`` and the watchers read the submitted Jobs as missing. A
watchdog now probes a TLS handshake through the tunnel every 30 seconds and,
after two failures in a row (or at once when the session exited), reopens the
session on the same local port through the same instance, so the kubeconfig
and every ``gco`` process that reads it keep working. A call that fails on
the tunnel's transport reopens a broken tunnel at once and is repeated one
time (:meth:`SessionKubectl.through_tunnel`).
"""

from __future__ import annotations

import os
import socket
import ssl
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

#: How the harness invokes kubectl; a function taking kubectl args and
#: returning (exit_code, stdout, stderr).
KubectlRunner = Callable[..., tuple[int, str, str]]

#: How often the watchdog probes the tunnel, how long one TLS handshake may
#: take (client-go's own handshake timeout), and how many failed probes in a
#: row it takes before the watchdog reopens a session that is still running.
_TUNNEL_CHECK_INTERVAL_SECONDS = 30.0
_TUNNEL_HANDSHAKE_TIMEOUT_SECONDS = 10.0
_TUNNEL_FAILURES_BEFORE_REOPEN = 2
#: A reopen that failed (the instance is gone, SSM refused the session) is
#: not tried again sooner than this, so parallel callers cannot stampede SSM.
_TUNNEL_REOPEN_BACKOFF_SECONDS = 60.0
_TUNNEL_EVENT_TEXT_LIMIT = 500
#: kubectl / client-go failures that say the request did not get through the
#: tunnel. A match alone never repeats a call: the keeper must also find the
#: tunnel broken (and reopen it) or already replaced since the call started.
_TUNNEL_TRANSPORT_MARKERS = (
    "tls handshake timeout",
    "unable to connect to the server",
    "was refused",
    "connection refused",
    "connection reset by peer",
    "i/o timeout",
)

_CLUSTER_API_READY_TIMEOUT_SECONDS = 45.0
_CLUSTER_API_PROBE_TIMEOUT_SECONDS = 8
_CLUSTER_API_RETRY_SECONDS = 1.0
_PERMANENT_API_STARTUP_MARKERS = (
    "certificate signed by unknown authority",
    "error loading config file",
    "exec plugin: invalid apiversion",
    "forbidden",
    "invalid configuration",
    "no configuration has been provided",
    "the server has asked for the client to provide credentials",
    "tls: failed to verify certificate",
    "unauthorized",
    "x509:",
)


def _is_permanent_api_startup_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in _PERMANENT_API_STARTUP_MARKERS)


def _wait_for_cluster_api(
    kubectl: KubectlRunner,
    *,
    tunnel_process: subprocess.Popen[bytes] | None,
    timeout_seconds: float = _CLUSTER_API_READY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _CLUSTER_API_RETRY_SECONDS,
) -> None:
    """Wait until the tunnel can carry an authenticated Kubernetes API request."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    from cli import ssm_tunnel

    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    last_error = "probe was not attempted"
    while True:
        if tunnel_process is not None and (
            detail := ssm_tunnel.exited_api_tunnel_detail(tunnel_process)
        ):
            raise RuntimeError(
                "SSM tunnel exited before the Kubernetes API became ready: " + detail
            )

        remaining = deadline - time.monotonic()
        if attempts and remaining <= 0:
            raise RuntimeError(
                "Kubernetes API did not become ready through the SSM tunnel within "
                f"{timeout_seconds:.1f}s after {attempts} attempt(s). "
                f"Last transient error: {last_error[:1000]}"
            )
        command_timeout = max(
            1,
            min(_CLUSTER_API_PROBE_TIMEOUT_SECONDS, int(max(remaining, 0.0)) + 1),
        )
        request_timeout = min(command_timeout, 5)
        attempts += 1
        try:
            returncode, stdout, stderr = kubectl(
                f"--request-timeout={request_timeout}s",
                "get",
                "--raw=/readyz",
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            detail = f"kubectl readiness probe timed out after {exc.timeout}s"
        else:
            if returncode == 0:
                return
            detail = (stderr or stdout).strip() or f"kubectl exited with status {returncode}"
            if _is_permanent_api_startup_error(detail):
                raise RuntimeError(
                    "Kubernetes API readiness probe failed with a permanent error: " + detail[:1000]
                )
        last_error = detail

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Kubernetes API did not become ready through the SSM tunnel within "
                f"{timeout_seconds:.1f}s after {attempts} attempt(s). "
                f"Last transient error: {last_error[:1000]}"
            )
        time.sleep(min(poll_interval_seconds, remaining))


class _QuietFormatter:
    """Adapter for cli formatter callbacks used by the tunnel helpers."""

    @staticmethod
    def print_info(message: str) -> None:
        print(f"[tunnel] {message}")

    print_success = print_info
    print_warning = print_info
    print_error = print_info


def _kubeconfig_path(kubeconfig_path: Path | None = None) -> Path:
    return kubeconfig_path if kubeconfig_path is not None else Path.home() / ".kube" / "config"


def _environment_with_kubeconfig(
    kubeconfig_path: Path | None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    if kubeconfig_path is None:
        return dict(base) if base is not None else None
    environment = dict(base) if base is not None else dict(os.environ)
    environment["KUBECONFIG"] = str(kubeconfig_path)
    return environment


def _update_kubeconfig_command(
    cluster_name: str,
    region: str,
    kubeconfig_path: Path | None,
) -> list[str]:
    command = ["aws", "eks", "update-kubeconfig", "--name", cluster_name, "--region", region]
    if kubeconfig_path is not None:
        command.extend(("--kubeconfig", str(kubeconfig_path)))
    return command


def _validate_and_secure_isolated_kubeconfig(path: Path) -> None:
    """Require the AWS-written isolated kubeconfig to be a current-user regular file."""
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Isolated kubeconfig must be a regular file: {path}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError(f"Isolated kubeconfig is not owned by this user: {path}")
    if os.name != "nt":
        path.chmod(0o600)


def refresh_kubeconfig(
    cluster_name: str,
    region: str,
    *,
    kubeconfig_path: Path | None = None,
) -> Path:
    """Run AWS CLI update-kubeconfig against the selected config file."""
    subprocess.run(
        _update_kubeconfig_command(cluster_name, region, kubeconfig_path),
        check=True,
        capture_output=True,
        text=True,
        env=_environment_with_kubeconfig(kubeconfig_path),
        shell=False,
    )
    path = _kubeconfig_path(kubeconfig_path)
    if kubeconfig_path is not None:
        _validate_and_secure_isolated_kubeconfig(path)
    return path


def update_and_point_kubeconfig_at_tunnel(
    cluster_name: str,
    region: str,
    server: str,
    tls_server_name: str,
    *,
    kubeconfig_path: Path | None = None,
) -> None:
    """Refresh kubeconfig, point it at the tunnel, and preserve real TLS SNI."""
    path = refresh_kubeconfig(
        cluster_name,
        region,
        kubeconfig_path=kubeconfig_path,
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("clusters"), list):
        raise ValueError(f"Kubeconfig has no cluster list: {path}")
    expected_suffix = f"cluster/{cluster_name}"
    matched = False
    for entry in config["clusters"]:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).endswith(expected_suffix):
            continue
        cluster = entry.get("cluster")
        if not isinstance(cluster, dict):
            raise ValueError(f"Kubeconfig cluster entry is malformed: {path}")
        cluster["server"] = server
        cluster["tls-server-name"] = tls_server_name
        matched = True
    if not matched:
        raise ValueError(f"Kubeconfig did not contain the requested cluster: {cluster_name}")
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    if kubeconfig_path is not None:
        _validate_and_secure_isolated_kubeconfig(path)


def ensure_cluster_access_entry(
    repo_root: Path,
    region: str,
    *,
    kubeconfig_path: Path | None = None,
    gco_command: tuple[str, ...] = ("gco",),
) -> None:
    """Grant cluster-admin through an explicitly selected GCO checkout."""
    if not gco_command or any(not isinstance(part, str) or not part for part in gco_command):
        raise ValueError("gco_command must be a non-empty argv prefix")
    result = subprocess.run(
        [*gco_command, "stacks", "access", "--region", region],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=_environment_with_kubeconfig(kubeconfig_path),
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gco stacks access failed for {region}: {result.stderr.strip()[:500]}")


class TunnelUnavailableError(RuntimeError):
    """The session's SSM tunnel is broken and could not be reopened."""


def is_tunnel_transport_error(detail: str) -> bool:
    """True when kubectl's error says the request did not get through the tunnel."""
    normalized = detail.casefold()
    return any(marker in normalized for marker in _TUNNEL_TRANSPORT_MARKERS)


def _tunnel_handshake_error(port: int, server_name: str, timeout: float) -> str | None:
    """``None`` when a TLS handshake completes through the local tunnel port, else why not.

    A stalled Session Manager session still accepts the local TCP connection
    and never relays the server's answer, so the handshake times out: the
    state this probe exists to catch. The probe sends no request. It does not
    need to trust the API server's certificate either — the cluster CA is not
    in the system store — because a certificate the client could not verify
    has, by then, come back through the tunnel.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection,
            context.wrap_socket(connection, server_hostname=server_name),
        ):
            return None
    except ssl.SSLCertVerificationError:
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TunnelKeeper:
    """Keep one session's SSM tunnel carrying the Kubernetes API.

    "The session process runs" and "the port accepts" prove nothing about a
    Session Manager port-forward; only a TLS handshake answered through it
    does. The keeper reopens the session on the same local port through the
    same instance and counts every successful reopen in ``generation``, so a
    call can tell whether the tunnel it failed on has been replaced since.
    Every attempt is appended to ``events`` for the run's evidence.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        endpoint: str,
        local_port: int,
        region: str,
        server_name: str,
        process: subprocess.Popen[bytes] | None,
        wait_ready: Callable[[subprocess.Popen[bytes]], None],
        formatter: Any,
        events: list[dict[str, Any]],
    ) -> None:
        self._instance_id = instance_id
        self._endpoint = endpoint
        self._local_port = local_port
        self._region = region
        self._server_name = server_name
        self._original = process
        self.process = process
        self._wait_ready = wait_ready
        self._formatter = formatter
        self.events = events
        self.generation = 0
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        self._retry_after = 0.0
        self._last_error = ""

    # -- health -------------------------------------------------------------

    def _exit_detail(self) -> str | None:
        process = self.process
        if process is None:
            return None
        code = process.poll()
        return None if code is None else f"the SSM session exited with code {code}"

    def _broken_reason(self) -> str | None:
        if (exited := self._exit_detail()) is not None:
            return exited
        error = _tunnel_handshake_error(
            self._local_port, self._server_name, _TUNNEL_HANDSHAKE_TIMEOUT_SECONDS
        )
        if error is None:
            return None
        return (
            f"no TLS handshake through 127.0.0.1:{self._local_port} within "
            f"{_TUNNEL_HANDSHAKE_TIMEOUT_SECONDS:g}s ({error})"
        )

    # -- reopening (callers hold the lock) ----------------------------------

    def _reopen(self, reason: str) -> bool:
        """Replace the SSM session; True once the Kubernetes API answers through it."""
        if self._closed.is_set():
            # close() stops only the session it can see; never open one after it.
            self._last_error = "the cluster session is closing"
            return False
        if time.monotonic() < self._retry_after:
            # _last_error still describes the failure that started the backoff.
            return False
        from cli import ssm_tunnel

        event: dict[str, Any] = {
            "at": _utc_timestamp(),
            "reason": reason[:_TUNNEL_EVENT_TEXT_LIMIT],
        }
        self.events.append(event)
        self._formatter.print_warning(f"Reopening the SSM tunnel: {reason}")
        stale, self.process = self.process, None
        if stale is not None:
            try:
                ssm_tunnel.stop_api_tunnel(stale)
            except RuntimeError as exc:
                event["stop_error"] = str(exc)[:_TUNNEL_EVENT_TEXT_LIMIT]
        try:
            self.process = ssm_tunnel.start_api_tunnel(
                self._instance_id, self._endpoint, self._local_port, self._region
            )
            self._wait_ready(self.process)
        except Exception as exc:  # any failure leaves the tunnel down; say so and back off
            self._last_error = f"{type(exc).__name__}: {exc}"[:_TUNNEL_EVENT_TEXT_LIMIT]
            event["result"] = "failed"
            event["error"] = self._last_error
            self._retry_after = time.monotonic() + _TUNNEL_REOPEN_BACKOFF_SECONDS
            self._formatter.print_error(f"Could not reopen the SSM tunnel: {self._last_error}")
            return False
        self.generation += 1
        event["result"] = "reopened"
        self._formatter.print_success("SSM tunnel reopened; the Kubernetes API answers again.")
        return True

    # -- entry points -------------------------------------------------------

    def recover(self, since: int) -> bool:
        """After a call made at generation ``since`` failed on the transport.

        True when repeating the call can help: the tunnel was replaced after
        the call started, or it is broken now and has just been reopened. A
        healthy tunnel returns False, so a failure that was not the tunnel's
        is reported as it happened rather than repeated.
        """
        with self._lock:
            if self.generation != since:
                return True
            reason = self._broken_reason()
            if reason is None:
                return False
            return self._reopen(f"a call through the tunnel failed and {reason}")

    def ensure(self) -> None:
        """Reopen a broken tunnel now; raise :class:`TunnelUnavailableError` if that fails."""
        with self._lock:
            reason = self._broken_reason()
            if reason is None or self._reopen(reason):
                return
            raise TunnelUnavailableError(
                f"the SSM tunnel to the Kubernetes API is broken ({reason}) and could not "
                f"be reopened: {self._last_error}"
            )

    def check(self) -> None:
        """One watchdog pass: reopen an exited session, or after consecutive failed handshakes."""
        with self._lock:
            reason = self._broken_reason()
            if reason is None:
                self._failures = 0
                return
            self._failures += 1
            if self._failures < _TUNNEL_FAILURES_BEFORE_REOPEN and self._exit_detail() is None:
                return
            self._failures = 0
            self._reopen(f"watchdog: {reason}")

    # -- watchdog lifecycle -------------------------------------------------

    def _watch(self) -> None:
        while not self._closed.wait(_TUNNEL_CHECK_INTERVAL_SECONDS):
            try:
                self.check()
            except Exception as exc:  # the watchdog must outlive any one bad probe
                self._formatter.print_warning(
                    f"SSM tunnel watchdog check failed: {type(exc).__name__}: {exc}"
                )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="gco-tunnel-watchdog", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the watchdog, then any session it opened (the original is its opener's)."""
        self._closed.set()
        if self._thread is not None:
            self._thread.join()
        from cli import ssm_tunnel

        with self._lock:
            process = self.process
            if process is None or process is self._original:
                return
            try:
                ssm_tunnel.stop_api_tunnel(process)
            except RuntimeError as exc:
                self._formatter.print_error(f"Could not stop the reopened SSM tunnel: {exc}")


class SessionKubectl:
    """The kubectl runner :func:`cluster_session` yields.

    Calling it runs kubectl against the session's kubeconfig and returns
    ``(code, stdout, stderr)``; a non-zero exit is data, never an exception.
    With a tunnel keeper, a call that fails on the tunnel's transport is
    repeated once after the keeper has reopened a broken tunnel.
    """

    def __init__(self, kubeconfig_path: Path | None, keeper: _TunnelKeeper | None = None) -> None:
        self._kubeconfig_path = kubeconfig_path
        self.keeper = keeper

    def run_once(self, *args: str, timeout: float = 120, **kwargs: Any) -> tuple[int, str, str]:
        """One kubectl invocation, never repeated."""
        if kwargs.pop("shell", False):
            raise ValueError("cluster_session kubectl does not allow shell execution")
        command = ["kubectl"]
        if self._kubeconfig_path is not None:
            command.extend(("--kubeconfig", str(self._kubeconfig_path)))
        command.extend(args)
        caller_environment = kwargs.pop("env", None)
        environment = _environment_with_kubeconfig(self._kubeconfig_path, caller_environment)
        # Callers branch on the code, so check=True would only turn data into
        # an exception here.
        kwargs.pop("check", None)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            shell=False,
            check=False,
            **kwargs,
        )
        return result.returncode, result.stdout, result.stderr

    def __call__(self, *args: str, timeout: float = 120, **kwargs: Any) -> tuple[int, str, str]:
        return self.through_tunnel(lambda: self.run_once(*args, timeout=timeout, **kwargs))

    def through_tunnel(
        self,
        call: Callable[[], tuple[int, str, str]],
        *,
        safe_to_repeat: Callable[[], bool] | None = None,
    ) -> tuple[int, str, str]:
        """Run ``call``; repeat it once when it failed on a tunnel the keeper then reopened.

        ``call`` is anything that crosses the tunnel and returns ``(code,
        stdout, stderr)`` — kubectl itself, or a ``gco`` command that shells
        out to it. ``safe_to_repeat`` is asked after the tunnel is back, for a
        call whose repetition could duplicate work the first attempt did.
        """
        keeper = self.keeper
        if keeper is None:
            return call()
        generation = keeper.generation
        code, stdout, stderr = call()
        if (
            code != 0
            and is_tunnel_transport_error(f"{stderr}\n{stdout}")
            and keeper.recover(generation)
            and (safe_to_repeat is None or safe_to_repeat())
        ):
            return call()
        return code, stdout, stderr

    def ensure_tunnel(self) -> None:
        """Reopen a broken tunnel now (a no-op without one); raise when it cannot be."""
        if self.keeper is not None:
            self.keeper.ensure()


def through_tunnel(
    kubectl: KubectlRunner,
    call: Callable[[], tuple[int, str, str]],
    *,
    safe_to_repeat: Callable[[], bool] | None = None,
) -> tuple[int, str, str]:
    """:meth:`SessionKubectl.through_tunnel` for a session runner; ``call()`` for any other."""
    if isinstance(kubectl, SessionKubectl):
        return kubectl.through_tunnel(call, safe_to_repeat=safe_to_repeat)
    return call()


def ensure_tunnel(kubectl: KubectlRunner) -> None:
    """:meth:`SessionKubectl.ensure_tunnel` for a session runner; a no-op for any other."""
    if isinstance(kubectl, SessionKubectl):
        kubectl.ensure_tunnel()


@contextmanager
def cluster_session(
    repo_root: Path,
    cluster_name: str,
    region: str,
    *,
    kubeconfig_path: Path | None = None,
    gco_command: tuple[str, ...] = ("gco",),
    bastion_ttl_minutes: int | None = None,
    tunnel_events: list[dict[str, Any]] | None = None,
) -> Iterator[KubectlRunner]:
    """Access entry plus tunnel for one region; optionally isolate kubeconfig.

    ``bastion_ttl_minutes`` sizes the ephemeral bastion's self-termination
    backstop (the bastion's default otherwise); ``tunnel_events`` collects
    every tunnel reopen the session's keeper attempted.
    """
    from cli import cluster_tunnel

    formatter = _QuietFormatter()
    ensure_cluster_access_entry(
        repo_root,
        region,
        kubeconfig_path=kubeconfig_path,
        gco_command=gco_command,
    )
    with cluster_tunnel.open_api_server_tunnel(
        formatter,
        cluster=cluster_name,
        region=region,
        via_ssm=cluster_tunnel.AUTO_BASTION,
        assume_yes=True,
        bastion_ttl_minutes=bastion_ttl_minutes,
    ) as session:
        if session.active and session.server and session.tls_server_name:
            update_and_point_kubeconfig_at_tunnel(
                cluster_name,
                region,
                session.server,
                session.tls_server_name,
                kubeconfig_path=kubeconfig_path,
            )
        else:
            refresh_kubeconfig(
                cluster_name,
                region,
                kubeconfig_path=kubeconfig_path,
            )

        kubectl = SessionKubectl(kubeconfig_path)
        process = getattr(session, "process", None)
        if session.active:
            _wait_for_cluster_api(kubectl.run_once, tunnel_process=process)
        instance_id = getattr(session, "instance_id", None)
        plan = getattr(session, "plan", None)
        if not (session.active and instance_id and plan is not None and session.tls_server_name):
            yield kubectl
            return
        keeper = _TunnelKeeper(
            instance_id=instance_id,
            endpoint=plan.endpoint,
            local_port=plan.local_port,
            region=region,
            server_name=session.tls_server_name,
            process=process,
            wait_ready=lambda reopened: _wait_for_cluster_api(
                kubectl.run_once, tunnel_process=reopened
            ),
            formatter=formatter,
            events=[] if tunnel_events is None else tunnel_events,
        )
        kubectl.keeper = keeper
        keeper.start()
        try:
            yield kubectl
        finally:
            keeper.close()
