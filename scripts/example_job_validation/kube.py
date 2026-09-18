"""Cluster access for live harnesses: SSM tunnel plus kubectl execution.

The historical example harness keeps its default behavior when no explicit
kubeconfig is supplied. Sibling harnesses can instead pass an isolated path;
AWS CLI and kubectl receive ``--kubeconfig`` and every nested ``gco`` process
receives ``KUBECONFIG``, so those runs never rewrite ``~/.kube/config``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

#: How the harness invokes kubectl; a function taking kubectl args and
#: returning (exit_code, stdout, stderr).
KubectlRunner = Callable[..., tuple[int, str, str]]

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


@contextmanager
def cluster_session(
    repo_root: Path,
    cluster_name: str,
    region: str,
    *,
    kubeconfig_path: Path | None = None,
    gco_command: tuple[str, ...] = ("gco",),
) -> Iterator[KubectlRunner]:
    """Access entry plus tunnel for one region; optionally isolate kubeconfig."""
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

        def kubectl(*args: str, timeout: int = 120, **kwargs: Any) -> tuple[int, str, str]:
            if kwargs.pop("shell", False):
                raise ValueError("cluster_session kubectl does not allow shell execution")
            command = ["kubectl"]
            if kubeconfig_path is not None:
                command.extend(("--kubeconfig", str(kubeconfig_path)))
            command.extend(args)
            caller_environment = kwargs.pop("env", None)
            environment = _environment_with_kubeconfig(
                kubeconfig_path,
                caller_environment,
            )
            # The runner returns (code, stdout, stderr) and callers branch on
            # the code, so a non-zero exit is data here, never an exception.
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

        if session.active:
            _wait_for_cluster_api(
                kubectl,
                tunnel_process=getattr(session, "process", None),
            )
        yield kubectl
