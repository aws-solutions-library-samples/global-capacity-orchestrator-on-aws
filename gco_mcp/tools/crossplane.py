"""Self-managed Crossplane MCP tool (read-only wrapper over `gco crossplane status`).

There is intentionally no ``crossplane open`` / ``screenshot`` tool — those are
interactive, long-running port-forwards to the Crossview dashboard.
"""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "crossplane"})
@audit_logged
async def crossplane_status() -> str:
    """`gco crossplane status` — the configured self-managed Crossplane (cdk.json helm.crossplane).

    Reports whether Crossplane and its Crossview dashboard are enabled, the
    pinned charts, the composition functions GCO installs and how to reach
    the dashboard. Reads cdk.json, charts.yaml and the shipped manifest only.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "crossplane", "status")
