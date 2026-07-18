"""
Shared kubectl helper utilities for GCO CLI.

Provides common kubectl operations used across multiple CLI modules
to reduce code duplication and ensure consistent error handling.
"""

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# EKS cluster names: 1-100 chars, alphanumeric and hyphens only.
# AWS region names: e.g. us-east-1, ap-southeast-2, eu-central-1.
_CLUSTER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,99}$")
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")


def _validate_cluster_name(cluster_name: str) -> None:
    """Raise ValueError if cluster_name contains characters outside the EKS naming rules."""
    if not _CLUSTER_NAME_RE.match(cluster_name):
        raise ValueError(
            f"Invalid cluster name {cluster_name!r}: must be 1-100 alphanumeric/hyphen characters"
        )


def _validate_region(region: str) -> None:
    """Raise ValueError if region does not match the standard AWS region pattern."""
    if not _REGION_RE.match(region):
        raise ValueError(f"Invalid AWS region {region!r}: expected format like 'us-east-1'")


def update_kubeconfig(cluster_name: str, region: str) -> None:
    """Update kubeconfig for an EKS cluster.

    Args:
        cluster_name: Name of the EKS cluster
        region: AWS region where the cluster is located

    Raises:
        ValueError: If cluster_name or region contain unexpected characters
        RuntimeError: If the kubeconfig update fails
        FileNotFoundError: If the AWS CLI is not installed
    """
    _validate_cluster_name(cluster_name)
    _validate_region(region)

    cmd = [
        "aws",
        "eks",
        "update-kubeconfig",
        "--name",
        cluster_name,
        "--region",
        region,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True
        )  # nosemgrep: dangerous-subprocess-use-audit - inputs validated above; list form, no shell=True
        if result.returncode != 0:
            raise RuntimeError(f"Failed to update kubeconfig: {result.stderr}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to update kubeconfig: {e.stderr}") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "AWS CLI not found. Please install the AWS CLI and ensure it's in your PATH."
        ) from e


# ---------------------------------------------------------------------------
# Port-forward + endpoint helpers (used by `gco monitoring open`)
# ---------------------------------------------------------------------------

# svc/name | service/name | pod/name | deploy/name | deployment/name, where the
# resource name follows the RFC 1123 rules kubectl accepts.
_PF_TARGET_RE = re.compile(
    r"^(svc|service|pod|deploy|deployment)/[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$"
)
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")


def _validate_port(port: int | str, *, what: str = "port") -> int:
    """Return the port as an int in 1..65535 or raise ValueError."""
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {what} {port!r}: must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"Invalid {what} {value}: must be between 1 and 65535")
    return value


def build_port_forward_command(
    namespace: str,
    target: str,
    local_port: int | str,
    remote_port: int | str,
    *,
    server: str | None = None,
    tls_server_name: str | None = None,
) -> list[str]:
    """Build a validated ``kubectl port-forward`` argv (list form, never a shell string).

    ``target`` is a ``kind/name`` reference (e.g. ``svc/kube-prometheus-stack-grafana``).
    ``server`` / ``tls_server_name`` override the API endpoint and its TLS SNI —
    used when tunnelling to a private endpoint through an SSM local port, where
    kubectl talks to ``https://localhost:<port>`` but must present the real EKS
    hostname for certificate validation.
    """
    if not _NAMESPACE_RE.match(namespace):
        raise ValueError(f"Invalid namespace {namespace!r}")
    if not _PF_TARGET_RE.match(target):
        raise ValueError(
            f"Invalid port-forward target {target!r}: expected kind/name "
            "(svc|service|pod|deploy|deployment)"
        )
    local = _validate_port(local_port, what="local port")
    remote = _validate_port(remote_port, what="remote port")

    cmd = ["kubectl", "port-forward", "-n", namespace, target, f"{local}:{remote}"]
    if server is not None:
        if not server.startswith("https://"):
            raise ValueError(f"Invalid --server {server!r}: must start with https://")
        cmd += ["--server", server]
    if tls_server_name is not None:
        if not re.match(r"^[a-zA-Z0-9.\-]{1,255}$", tls_server_name):
            raise ValueError(f"Invalid --tls-server-name {tls_server_name!r}")
        cmd += ["--tls-server-name", tls_server_name]
    return cmd


def describe_cluster_access(cluster_name: str, region: str) -> dict[str, object]:
    """Return the EKS API endpoint and its public/private access posture.

    Returns a dict with keys ``endpoint`` (str), ``public`` (bool),
    ``private`` (bool), and ``public_cidrs`` (list[str]). Used by
    ``gco monitoring open`` to decide whether a plain ``kubectl port-forward``
    can reach the API server or whether an SSM/VPN/bastion path is required.
    """
    _validate_cluster_name(cluster_name)
    _validate_region(region)

    cmd = [
        "aws",
        "eks",
        "describe-cluster",
        "--name",
        cluster_name,
        "--region",
        region,
        "--query",
        (
            "cluster.{endpoint:endpoint,"
            "public:resourcesVpcConfig.endpointPublicAccess,"
            "private:resourcesVpcConfig.endpointPrivateAccess,"
            "publicCidrs:resourcesVpcConfig.publicAccessCidrs}"
        ),
        "--output",
        "json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True
        )  # nosemgrep: dangerous-subprocess-use-audit - inputs validated above; list form, no shell=True
    except FileNotFoundError as exc:
        raise RuntimeError(
            "AWS CLI not found. Please install the AWS CLI and ensure it's in your PATH."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"Failed to describe cluster {cluster_name}: {result.stderr}")

    import json

    data = json.loads(result.stdout or "{}")
    return {
        "endpoint": data.get("endpoint") or "",
        "public": bool(data.get("public")),
        "private": bool(data.get("private")),
        "public_cidrs": data.get("publicCidrs") or [],
    }
