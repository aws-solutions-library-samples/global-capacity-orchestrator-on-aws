"""Cluster topology resources (gco://cluster/...) for the GCO MCP server.

Aggregates two views of regional cluster state into a single JSON
payload an LLM can pin: the Karpenter NodePool inventory (via the
``gco nodepools list`` CLI surface) and the list of pods currently
in ``Pending`` phase (via ``kubectl get pods``). The combination is
the cheapest read that answers "what shape is this cluster in right
now and what's stuck waiting for room to schedule".
"""

from __future__ import annotations

import json
import re
from typing import Any

import cli_runner

# AWS region IDs: lowercase letters, digits, and hyphens. The bounded
# length is generous (current AWS regions max out near 14 characters,
# but local-region constructs like ``gov-east-1`` may grow).
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")
_KUBECTL_TIMEOUT_SECONDS = 30


def _list_nodepools(region: str) -> dict[str, Any]:
    """Run ``gco nodepools list`` and return the parsed payload (or an error stub)."""
    raw = cli_runner._run_cli("nodepools", "list", "-r", region)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return {"error": "failed to parse nodepools output", "raw": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _pending_pods(region: str) -> dict[str, Any]:
    """Return pods currently in ``Pending`` phase across the regional cluster."""
    cluster_name = f"gco-{region}"
    try:
        result = cli_runner.subprocess.run(  # type: ignore[attr-defined] # nosemgrep: dangerous-subprocess-use-audit - shell=False; argv built from validated region literal
            [
                "kubectl",
                "get",
                "pods",
                "--all-namespaces",
                "--field-selector",
                "status.phase=Pending",
                "-o",
                "json",
                "--context",
                f"arn:aws:eks:{region}:cluster/{cluster_name}",
            ],
            capture_output=True,
            text=True,
            timeout=_KUBECTL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"error": "kubectl not found"}
    except cli_runner.subprocess.TimeoutExpired:  # type: ignore[attr-defined]
        return {"error": f"kubectl timed out after {_KUBECTL_TIMEOUT_SECONDS}s"}
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return {"error": err or "kubectl command failed", "exit_code": result.returncode}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError, ValueError:
        return {"error": "failed to parse kubectl output", "raw": result.stdout}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _topology_resource(region: str) -> str:
    """Return a structured snapshot of nodepools plus pending pods for ``region``."""
    if not _REGION_RE.match(region):
        return json.dumps({"error": "invalid region", "value": region})
    summary = {
        "region": region,
        "nodepools": _list_nodepools(region),
        "pending_pods": _pending_pods(region),
    }
    return json.dumps(summary, indent=2, default=str)


def register(mcp_instance: Any) -> None:
    """Register the cluster topology aggregator against the shared MCP server."""
    mcp_instance.resource("gco://cluster/{region}/topology")(_topology_resource)
