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
from typing import Any

import cli_runner

from resources._eks import eks_context_for_region, is_valid_region

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
    """Return pending pods from the explicitly selected regional cluster."""
    try:
        context_arn = eks_context_for_region(region)
    except Exception as exc:  # AWS credential/session failures become resource errors
        return {"error": "unable to resolve EKS context", "detail": str(exc)[:200]}
    try:
        result = cli_runner.subprocess.run(  # type: ignore[attr-defined] # nosemgrep: dangerous-subprocess-use-audit - shell=False; argv built from validated region and account-qualified ARN
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
                context_arn,
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
    if not is_valid_region(region):
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
