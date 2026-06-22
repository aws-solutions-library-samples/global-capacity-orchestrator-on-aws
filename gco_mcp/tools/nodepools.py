"""Karpenter NodePool management MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "nodepools"})
@audit_logged
async def nodepools_list(region: str | None = None, cluster: str | None = None) -> str:
    """`gco nodepools list` — list Karpenter NodePools in a cluster.

    Args:
        region: AWS region to query.
        cluster: EKS cluster name (defaults to ``gco-<region>``).
    """
    args = ["nodepools", "list"]
    if region:
        args += ["-r", region]
    if cluster:
        args += ["--cluster", cluster]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "nodepools"})
@audit_logged
async def nodepools_describe(nodepool_name: str, region: str, cluster: str | None = None) -> str:
    """`gco nodepools describe` — describe a single NodePool.

    Args:
        nodepool_name: NodePool name.
        region: AWS region.
        cluster: EKS cluster name (defaults to ``gco-<region>``).
    """
    args = ["nodepools", "describe", nodepool_name, "-r", region]
    if cluster:
        args += ["--cluster", cluster]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "nodepools"})
@audit_logged
async def nodepools_create_odcr(
    name: str,
    region: str,
    instance_type: str,
    capacity_reservation_id: str,
    cluster: str | None = None,
    count: int = 1,
    taints: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """`gco nodepools create-odcr` — create a Karpenter NodePool tied to an ODCR.

    Args:
        name: NodePool name.
        region: AWS region.
        instance_type: EC2 instance type the NodePool will provision.
        capacity_reservation_id: EC2 Capacity Reservation ID (``cr-...``) or ODCR group ARN.
        cluster: EKS cluster name (defaults to ``gco-<region>``).
        count: Initial node count target.
        taints: Optional taints formatted as ``key=value:effect``; one per ``--taint`` flag.
        labels: Optional ``key=value`` labels; one per ``--label`` flag.
    """
    args = [
        "nodepools",
        "create-odcr",
        name,
        "-r",
        region,
        "--instance-type",
        instance_type,
        "--capacity-reservation-id",
        capacity_reservation_id,
        "--count",
        str(count),
    ]
    if cluster:
        args += ["--cluster", cluster]
    if taints:
        for taint in taints:
            args += ["--taint", taint]
    if labels:
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Destructive tools — gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS
# =============================================================================


import contextlib  # noqa: E402

from feature_flags import FLAG_DESTRUCTIVE_OPERATIONS, is_enabled  # noqa: E402


async def _ctx_warning(message: str) -> None:
    """Emit ``ctx.warning(...)`` from inside a tool body, no-op when no Context."""
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return
    with contextlib.suppress(Exception):
        await ctx.warning(message)


if is_enabled(FLAG_DESTRUCTIVE_OPERATIONS):

    @mcp.tool(tags={"destructive", "nodepools"})
    @audit_logged
    async def delete_nodepool(nodepool_name: str, region: str, cluster: str | None = None) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco nodepools delete` — delete a Karpenter NodePool.
        Cannot be undone — the NodePool, its EC2NodeClass, and any nodes
        currently provisioned through it are removed.

        Args:
            nodepool_name: NodePool name.
            region: AWS region.
            cluster: EKS cluster name (defaults to ``gco-<region>``).
        """
        await _ctx_warning(
            f"Deleting NodePool {nodepool_name!r} in {region} — this cannot be undone."
        )
        args = ["nodepools", "delete", nodepool_name, "-r", region, "-y"]
        if cluster:
            args += ["--cluster", cluster]
        return await asyncio.to_thread(cli_runner._run_cli, *args)
