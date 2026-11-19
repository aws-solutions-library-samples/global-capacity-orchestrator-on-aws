"""Fleet-wide status MCP tool (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "status"})
@audit_logged
async def fleet_status(
    region: str | None = None,
    with_costs: bool = False,
    with_nodepools: bool = False,
) -> str:
    """`gco status` — whole-fleet deployment status as one document.

    Aggregates stacks, queue depth, central job-queue counts, capacity
    telemetry, and inference endpoints across every configured region. The
    jobs section counts only `gco queue submit` records (its data says
    ``source: central job queue``); `list_jobs` lists jobs submitted any
    other way. Each section carries its own status
    (ok/empty/partial/unavailable/error/skipped) plus a findings list of
    what looks wrong, so one call answers what would otherwise take eight.

    Args:
        region: Restrict the gather to a single region (default: every
            configured deployment region).
        with_costs: Include the costs section. Cost Explorer bills per
            request, so this is off by default.
        with_nodepools: Include Karpenter NodePools. Requires a publicly
            reachable cluster API endpoint; private endpoints are reported
            as unavailable.
    """
    args = ["status"]
    if region:
        args += ["-r", region]
    if with_costs:
        args.append("--with-costs")
    if with_nodepools:
        args.append("--with-nodepools")
    return await asyncio.to_thread(cli_runner._run_cli, *args)
