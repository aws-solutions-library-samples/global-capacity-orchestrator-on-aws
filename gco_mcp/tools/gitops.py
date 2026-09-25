"""Self-managed Argo CD MCP tool (read-only wrapper over `gco gitops status`).

There is intentionally no ``gitops open`` / ``screenshot`` tool — those are
interactive, long-running port-forwards — and no ``gitops password`` tool: the
generated Argo CD admin password never leaves the operator's terminal.
"""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "gitops"})
@audit_logged
async def gitops_status(region: str | None = None) -> str:
    """`gco gitops status` — the configured self-managed Argo CD (cdk.json helm.argocd).

    Reports whether Argo CD is enabled, the pinned argo-cd chart, the
    gco-tenants fence (destinations and source repositories) and the GitOps
    hand-off with its path rendered per Region, plus how to reach the UI.
    Reads cdk.json and charts.yaml only.

    Args:
        region: Render the GitOps path for this Region only. Omit for every
            regional deployment Region.
    """
    args = ["gitops", "status"]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)
