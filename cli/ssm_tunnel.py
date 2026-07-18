"""SSM Session Manager tunnel helpers for reaching a private EKS API endpoint.

GCO clusters default to a PRIVATE EKS API endpoint (``eks_cluster.endpoint_access
= "PRIVATE"``), so ``kubectl`` — and therefore ``kubectl port-forward`` to
Grafana — cannot reach the API server from a laptop outside the VPC. This module
builds an ``aws ssm start-session`` port-forwarding tunnel through an SSM-managed
instance in the VPC to the cluster's API endpoint, giving kubectl a
``https://localhost:<port>`` server to talk to.

The command builder is pure and validated (list form, never a shell string) so
it is fully unit-testable; :func:`start_api_tunnel` is the thin runtime wrapper
that launches it as a background process.

Requires the Session Manager plugin on the local machine
(https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urlparse

# SSM managed-node ids: EC2 (i-...) and hybrid/managed (mi-...), 8 or 17 hex.
_INSTANCE_RE = re.compile(r"^(i|mi)-[0-9a-f]{8}([0-9a-f]{9})?$")
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-]{1,255}$")
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")

# AWS SSM document that forwards a local port to an arbitrary remote host
# reachable from the managed node (here, the private EKS API endpoint).
REMOTE_HOST_DOCUMENT = "AWS-StartPortForwardingSessionToRemoteHost"


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

    Forwards ``localhost:<local_port>`` through ``instance_id`` to
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


def start_api_tunnel(
    instance_id: str,
    endpoint: str,
    local_port: int,
    region: str,
    *,
    ready_wait_seconds: float = 3.0,
) -> subprocess.Popen[bytes]:
    """Launch a background SSM tunnel to the cluster API endpoint.

    Returns the :class:`subprocess.Popen` handle so the caller can terminate it
    when the port-forward session ends. Raises ``RuntimeError`` if the session
    process exits immediately (e.g. missing Session Manager plugin or IAM).
    """
    host = endpoint_host(endpoint)
    cmd = build_remote_host_port_forward_command(instance_id, host, local_port, region)
    try:
        proc = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit - argv validated by builder; list form, no shell=True
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "AWS CLI not found. Install the AWS CLI and the Session Manager plugin."
        ) from exc
    # Give the session a moment to establish; if it died immediately, surface it.
    time.sleep(ready_wait_seconds)
    if proc.poll() is not None:
        _, stderr = proc.communicate()
        detail = stderr.decode("utf-8", "replace").strip() if stderr else "(no output)"
        raise RuntimeError(
            "SSM port-forwarding session failed to start. Ensure the Session "
            "Manager plugin is installed and the target instance can reach the "
            f"cluster endpoint. Details: {detail}"
        )
    return proc
