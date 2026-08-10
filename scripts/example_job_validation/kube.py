"""Cluster access for the examples action: SSM tunnel + kubectl execution.

GCO clusters default to a PRIVATE EKS API endpoint, so both the documented
``kubectl apply`` paths and ``gco jobs submit-direct`` (a kubectl wrapper)
need a tunnel when the harness runs from a laptop. This module reuses the
CLI's own machinery (``cli.cluster_tunnel.open_api_server_tunnel``, the
ephemeral SSM bastion) and then rewrites the kubeconfig cluster entry to
point at the tunnel — so every kubectl invocation this run makes, including
the ones inside ``gco jobs submit-direct``, travels the tunnel unmodified.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

#: How the harness invokes kubectl; a function taking kubectl args and
#: returning (exit_code, stdout, stderr).
KubectlRunner = Callable[..., tuple[int, str, str]]


class _QuietFormatter:
    """Adapter for cli formatter callbacks used by the tunnel helpers."""

    @staticmethod
    def print_info(message: str) -> None:
        print(f"[tunnel] {message}")

    print_success = print_info
    print_warning = print_info
    print_error = print_info


def _kubeconfig_path() -> Path:
    return Path.home() / ".kube" / "config"


def update_and_point_kubeconfig_at_tunnel(
    cluster_name: str, region: str, server: str, tls_server_name: str
) -> None:
    """Refresh kubeconfig for the cluster, then aim it at the local tunnel.

    ``aws eks update-kubeconfig`` writes the private endpoint URL; kubectl
    can't reach that from outside the VPC. Rewriting ``cluster.server`` to
    the tunnel and pinning ``tls-server-name`` to the real endpoint host
    makes every kubectl caller work without per-invocation flags.
    """
    subprocess.run(
        ["aws", "eks", "update-kubeconfig", "--name", cluster_name, "--region", region],
        check=True,
        capture_output=True,
        text=True,
    )
    path = _kubeconfig_path()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected_suffix = f"cluster/{cluster_name}"
    for entry in config.get("clusters", []):
        if str(entry.get("name", "")).endswith(expected_suffix):
            entry["cluster"]["server"] = server
            entry["cluster"]["tls-server-name"] = tls_server_name
            # The tunnel presents the cluster's own cert for the real
            # hostname; keep CA verification against it enabled.
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def ensure_cluster_access_entry(repo_root: Path, region: str) -> None:
    """Grant the calling principal EKS cluster-admin via the CLI's own path."""
    result = subprocess.run(
        ["gco", "stacks", "access", "--region", region],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gco stacks access failed for {region}: {result.stderr.strip()[:500]}")


@contextmanager
def cluster_session(repo_root: Path, cluster_name: str, region: str) -> Iterator[KubectlRunner]:
    """Access entry + tunnel + kubeconfig for one region; yields a kubectl runner."""
    from cli import cluster_tunnel

    formatter = _QuietFormatter()
    ensure_cluster_access_entry(repo_root, region)
    with cluster_tunnel.open_api_server_tunnel(
        formatter,
        cluster=cluster_name,
        region=region,
        via_ssm=cluster_tunnel.AUTO_BASTION,
        assume_yes=True,
    ) as session:
        if session.active and session.server and session.tls_server_name:
            update_and_point_kubeconfig_at_tunnel(
                cluster_name, region, session.server, session.tls_server_name
            )
        else:
            # Public endpoint (or pre-existing VPC connectivity): plain
            # kubeconfig is sufficient.
            subprocess.run(
                [
                    "aws",
                    "eks",
                    "update-kubeconfig",
                    "--name",
                    cluster_name,
                    "--region",
                    region,
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        def kubectl(*args: str, timeout: int = 120, **kwargs: Any) -> tuple[int, str, str]:
            result = subprocess.run(
                ["kubectl", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                **kwargs,
            )
            return result.returncode, result.stdout, result.stderr

        yield kubectl
