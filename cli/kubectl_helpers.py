"""
Shared kubectl helper utilities for GCO CLI.

Provides common kubectl operations used across multiple CLI modules
to reduce code duplication and ensure consistent error handling.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gco.aws_names import is_aws_region_name

logger = logging.getLogger(__name__)

# EKS cluster names: 1-100 chars, alphanumeric and hyphens only.
_CLUSTER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,99}$")


def _validate_cluster_name(cluster_name: str) -> None:
    """Raise ValueError if cluster_name contains characters outside the EKS naming rules."""
    if not _CLUSTER_NAME_RE.fullmatch(cluster_name):
        raise ValueError(
            f"Invalid cluster name {cluster_name!r}: must be 1-100 alphanumeric/hyphen characters"
        )


def _validate_region(region: str) -> None:
    """Raise ValueError if region does not match the standard AWS region pattern."""
    if not is_aws_region_name(region):
        raise ValueError(f"Invalid AWS region {region!r}: expected format like 'us-east-1'")


#: Hostnames that identify a local API-server tunnel in ``cluster.server``.
_LOCAL_TUNNEL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _kubeconfig_file() -> Path:
    """The kubeconfig file ``aws eks update-kubeconfig`` would write."""
    kubeconfig_env = os.environ.get("KUBECONFIG", "")
    if kubeconfig_env:
        first = kubeconfig_env.split(os.pathsep)[0]
        if first:
            return Path(first)
    return Path.home() / ".kube" / "config"


#: What ``aws eks update-kubeconfig`` prints after writing the context it also
#: made current (awscli customizations/eks/update_kubeconfig.py).
_WRITTEN_CONTEXT_RE = re.compile(
    r"^(?:Updated context|Added new context) (?P<context>\S+) (?:in|to) ", re.MULTILINE
)


def _mappings(value: object) -> list[dict[str, Any]]:
    """The mapping items of a kubeconfig list (``[]`` for anything else)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _tunnel_pinned_server(cluster_name: str) -> tuple[str, str] | None:
    """Return ``(server, context)`` when *cluster_name*'s entry is pinned to a local tunnel.

    ``gco cluster tunnel`` (and callers of its machinery, e.g. the example-job
    validation harness) rewrite the cluster's kubeconfig entry to point at a
    localhost tunnel with ``tls-server-name`` pinned to the real endpoint
    host. Any entry matching that shape is a deliberate operator choice.

    The refresh that would make the cluster's context current is skipped for
    such an entry, so the context returned is one bound to the pinned entry:
    the current context when it is one of them, else the first.

    Raises:
        RuntimeError: If no context uses the pinned entry, so kubectl cannot
            reach it.
    """
    import yaml

    path = _kubeconfig_file()
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError, yaml.YAMLError:
        return None
    if not isinstance(config, dict):
        return None
    expected_suffix = f"cluster/{cluster_name}"
    for entry in _mappings(config.get("clusters")):
        name = str(entry.get("name", ""))
        if name != cluster_name and not name.endswith(expected_suffix):
            continue
        cluster = entry.get("cluster") or {}
        server = str(cluster.get("server", ""))
        host = urlsplit(server).hostname or ""
        if host not in _LOCAL_TUNNEL_HOSTS or not cluster.get("tls-server-name"):
            continue
        contexts = [
            str(item["name"])
            for item in _mappings(config.get("contexts"))
            if item.get("name") and (item.get("context") or {}).get("cluster") == name
        ]
        if not contexts:
            raise RuntimeError(
                f"kubeconfig {path} points cluster {name} at the local tunnel {server}, "
                "but no kubeconfig context uses it, so kubectl cannot reach it. Add one "
                f"(kubectl config set-context <name> --cluster {name} --user <user>) or "
                "remove the entry and re-run to refresh it."
            )
        current = config.get("current-context")
        return server, current if current in contexts else contexts[0]
    return None


def kubectl_context_flags(context: str | None) -> list[str]:
    """``--context`` for the context :func:`update_kubeconfig` returned (or nothing).

    The ``=`` form keeps a context name that starts with ``-`` from reading as
    a flag.
    """
    return [] if context is None else [f"--context={context}"]


#: kubectl's wording when it never reached the API server (connect, DNS, TLS
#: and request timeouts); kubectl prefixes most of them with the first phrase.
KUBECTL_CONNECT_FAILURES = (
    "unable to connect to the server",
    "i/o timeout",
    "no route to host",
    "connection refused",
    "dial tcp",
    "tls handshake timeout",
    "context deadline exceeded",
    "client.timeout exceeded",
    "no such host",
    "network is unreachable",
)


def kubectl_could_not_connect(stderr: str) -> bool:
    """Return whether a failed kubectl call never reached the API server."""
    lowered = stderr.lower()
    return any(phrase in lowered for phrase in KUBECTL_CONNECT_FAILURES)


def update_kubeconfig(cluster_name: str, region: str) -> str | None:
    """Update kubeconfig for an EKS cluster, preserving an active tunnel pin.

    When the cluster's kubeconfig entry already points at a localhost tunnel
    (``gco cluster tunnel`` rewrites ``cluster.server`` to the tunnel and pins
    ``tls-server-name`` to the real endpoint host), refreshing it with
    ``aws eks update-kubeconfig`` would silently rewrite the server back to
    the private endpoint — unreachable from outside the VPC — and break every
    kubectl-wrapping command mid-session. Caught live by example-job
    validation run ex241-66c02e71, where ``gco jobs submit-direct`` clobbered
    the harness's tunnel and every subsequent kubectl call timed out against
    the private endpoint. A tunnel-shaped entry is preserved untouched; a
    dead tunnel fails loudly at connect time, which beats a silent rewrite.

    Skipping the refresh also skips the context switch it makes, so the
    current context can name another cluster. Pass the returned context to
    every later kubectl call (:func:`kubectl_context_flags`); that also keeps
    a context switch made elsewhere from redirecting a multi-step operation.

    Args:
        cluster_name: Name of the EKS cluster
        region: AWS region where the cluster is located

    Returns:
        The kubeconfig context that targets the cluster, or ``None`` when the
        AWS CLI did not report one (its refresh made the cluster's context
        current).

    Raises:
        ValueError: If cluster_name or region contain unexpected characters
        RuntimeError: If the kubeconfig update fails
        FileNotFoundError: If the AWS CLI is not installed
    """
    _validate_cluster_name(cluster_name)
    _validate_region(region)

    pinned = _tunnel_pinned_server(cluster_name)
    if pinned is not None:
        server, context = pinned
        logger.info(
            "kubeconfig for %s points at local tunnel %s; preserving it (context %s)",
            cluster_name,
            server,
            context,
        )
        return context

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
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )  # nosemgrep: dangerous-subprocess-use-audit - inputs validated above; list form, no shell=True
        if result.returncode != 0:
            raise RuntimeError(f"Failed to update kubeconfig: {result.stderr}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to update kubeconfig: {e.stderr}") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "AWS CLI not found. Please install the AWS CLI and ensure it's in your PATH."
        ) from e
    written = _WRITTEN_CONTEXT_RE.search(result.stdout or "")
    return written.group("context") if written else None


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
    context: str | None = None,
) -> list[str]:
    """Build a validated ``kubectl port-forward`` argv (list form, never a shell string).

    ``target`` is a ``kind/name`` reference (e.g. ``svc/kube-prometheus-stack-grafana``).
    ``server`` / ``tls_server_name`` override the API endpoint and its TLS SNI —
    used when tunnelling to a private endpoint through an SSM local port, where
    kubectl talks to ``https://127.0.0.1:<port>`` but must present the real EKS
    hostname for certificate validation. ``context`` is the kubeconfig context
    :func:`update_kubeconfig` returned; it supplies the cluster's credentials
    and CA even when ``server`` overrides the endpoint.
    """
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(f"Invalid namespace {namespace!r}")
    if not _PF_TARGET_RE.fullmatch(target):
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
        if not re.fullmatch(r"^[a-zA-Z0-9.\-]{1,255}$", tls_server_name):
            raise ValueError(f"Invalid --tls-server-name {tls_server_name!r}")
        cmd += ["--tls-server-name", tls_server_name]
    return cmd + kubectl_context_flags(context)


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
            cmd,
            capture_output=True,
            text=True,
            check=False,
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
