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
    capacity_reservation_id: str,
    instance_type: list[str] | None = None,
    max_nodes: int = 100,
    fallback_on_demand: bool = False,
    efa: bool = False,
) -> str:
    """`gco nodepools create-odcr` — create a Karpenter NodePool tied to an ODCR.

    Generates a Karpenter NodePool + EC2NodeClass that consume an On-Demand
    Capacity Reservation via ``capacityReservationSelectorTerms``.

    Args:
        name: NodePool name.
        region: AWS region.
        capacity_reservation_id: EC2 Capacity Reservation ID (``cr-...``) or ODCR group ARN.
        instance_type: Instance types the NodePool may provision (one per entry).
        max_nodes: Maximum nodes in the pool.
        fallback_on_demand: Fall back to on-demand when the ODCR is exhausted.
        efa: Enable EFA support (adds EFA taint and labels).
    """
    args = [
        "nodepools",
        "create-odcr",
        "-n",
        name,
        "-r",
        region,
        "-c",
        capacity_reservation_id,
        "--max-nodes",
        str(max_nodes),
    ]
    for it in instance_type or []:
        args += ["-i", it]
    if fallback_on_demand:
        args.append("--fallback-on-demand")
    if efa:
        args.append("--efa")
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"low-risk", "nodepools"})
@audit_logged
async def nodepools_create_capacity_block(
    name: str,
    region: str,
    capacity_reservation_id: str,
    instance_type: list[str] | None = None,
    max_nodes: int = 100,
    fallback_on_demand: bool = False,
    efa: bool = False,
) -> str:
    """`gco nodepools create-capacity-block` — NodePool for a purchased Capacity Block.

    The Capacity Block counterpart to ``nodepools_create_odcr``. Purchasing a
    Capacity Block yields an EC2 Capacity Reservation id (``cr-...``), which this
    NodePool consumes via ``capacityReservationSelectorTerms``. Because a block is
    prepaid for a fixed term, the NodePool holds the capacity rather than
    consolidating it early.

    Args:
        name: NodePool name.
        region: AWS region.
        capacity_reservation_id: Capacity Reservation ID (``cr-...``) of the block.
        instance_type: Instance types the NodePool may provision (one per entry).
        max_nodes: Maximum nodes in the pool.
        fallback_on_demand: Fall back to on-demand when the block is exhausted/expired.
        efa: Enable EFA support (adds EFA taint and labels).
    """
    args = [
        "nodepools",
        "create-capacity-block",
        "-n",
        name,
        "-r",
        region,
        "-c",
        capacity_reservation_id,
        "--max-nodes",
        str(max_nodes),
    ]
    for it in instance_type or []:
        args += ["-i", it]
    if fallback_on_demand:
        args.append("--fallback-on-demand")
    if efa:
        args.append("--efa")
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
