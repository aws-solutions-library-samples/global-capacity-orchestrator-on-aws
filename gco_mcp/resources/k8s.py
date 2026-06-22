"""Kubernetes manifest and live-state resources (k8s:// + gco://k8s/...) for
the GCO MCP server.

The static ``k8s://gco/manifests/...`` paths surface the cluster-bootstrap
manifests that ship under ``lambda/kubectl-applier-simple/manifests``. The
live ``gco://k8s/{namespace}/{kind}/{name}`` template wraps ``kubectl get``
so an LLM can pin any cluster resource for inspection across turns.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import cli_runner
from server import mcp

PROJECT_ROOT = Path(__file__).parent.parent.parent
MANIFESTS_DIR = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"

# Permissive RFC 1123 label rule for namespace and resource names.
# Bounded length plus the alphanumeric+hyphen alphabet rules out
# command-injection vectors when the value is forwarded to ``kubectl``.
_K8S_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$")

# Kubernetes resource kinds — alphanumeric only, including dotted CRD
# group forms like ``deployments.apps`` and ``ingresses.networking.k8s.io``.
# Kept deliberately tight so ``kubectl get <kind>`` cannot be coerced
# into a flag.
_K8S_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9-]+)*$")

_KUBECTL_TIMEOUT_SECONDS = 30


@mcp.resource("k8s://gco/manifests/index")
def k8s_manifests_index() -> str:
    """List all Kubernetes manifests deployed to the EKS cluster."""
    lines = ["# Kubernetes Cluster Manifests\n"]
    lines.append("Applied in order during `gco stacks deploy`:\n")
    for f in sorted(MANIFESTS_DIR.glob("*.yaml")):
        lines.append(f"- `k8s://gco/manifests/{f.name}` — {f.stem}")
    readme = MANIFESTS_DIR / "README.md"
    if readme.is_file():
        lines.append("\n- `k8s://gco/manifests/README.md` — manifest documentation")
    return "\n".join(lines)


@mcp.resource("k8s://gco/manifests/{filename}")
def k8s_manifest_resource(filename: str) -> str:
    """Read a Kubernetes manifest that gets applied to the EKS cluster."""
    path = MANIFESTS_DIR / filename
    if not path.is_file():
        available = sorted(f.name for f in MANIFESTS_DIR.glob("*") if f.is_file())
        return f"Manifest '{filename}' not found. Available:\n" + "\n".join(available)
    return path.read_text()


def _k8s_live_resource(namespace: str, kind: str, name: str) -> str:
    """Return the live YAML for ``<kind>/<name>`` in ``<namespace>``."""
    if not _K8S_NAME_RE.match(namespace):
        return json.dumps({"error": "invalid namespace", "value": namespace})
    if not _K8S_KIND_RE.match(kind):
        return json.dumps({"error": "invalid kind", "value": kind})
    if not _K8S_NAME_RE.match(name):
        return json.dumps({"error": "invalid name", "value": name})
    try:
        result = cli_runner.subprocess.run(  # type: ignore[attr-defined] # nosemgrep: dangerous-subprocess-use-audit - shell=False; every argv element is regex-validated above
            ["kubectl", "get", kind, name, "-n", namespace, "-o", "yaml"],
            capture_output=True,
            text=True,
            timeout=_KUBECTL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return json.dumps({"error": "kubectl not found"})
    except cli_runner.subprocess.TimeoutExpired:  # type: ignore[attr-defined]
        return json.dumps({"error": f"kubectl timed out after {_KUBECTL_TIMEOUT_SECONDS}s"})
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return json.dumps(
            {"error": err or "kubectl command failed", "exit_code": result.returncode}
        )
    return str(result.stdout)


def register(mcp_instance: Any) -> None:
    """Register the live Kubernetes resource template against ``mcp_instance``.

    The static ``k8s://gco/manifests/...`` resources are decorated at
    import time and don't need re-registration; this function exists
    so ``register_all_resources()`` can wire the live template in
    alongside the rest of the live-state modules.
    """
    mcp_instance.resource("gco://k8s/{namespace}/{kind}/{name}")(_k8s_live_resource)
