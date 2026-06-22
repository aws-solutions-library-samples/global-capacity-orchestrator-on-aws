"""Queue management MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "queue"})
@audit_logged
async def queue_list(
    region: str | None = None,
    status: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
) -> str:
    """`gco queue list` — list jobs in the global queue.

    Args:
        region: Filter by target region.
        status: Filter by status (queued, claimed, running, succeeded, failed, cancelled).
        namespace: Filter by Kubernetes namespace.
        limit: Maximum results (default 50).
    """
    args = ["queue", "list"]
    if region:
        args += ["-r", region]
    if status:
        args += ["--status", status]
    if namespace:
        args += ["-n", namespace]
    args += ["--limit", str(limit)]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "queue"})
@audit_logged
async def queue_get(job_id: str, region: str | None = None) -> str:
    """`gco queue get` — fetch a single job from the global queue.

    Args:
        job_id: Job identifier.
        region: Region to query (any region works).
    """
    args = ["queue", "get", job_id]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "queue"})
@audit_logged
async def queue_stats(region: str | None = None) -> str:
    """`gco queue stats` — show aggregate stats for the global queue.

    Args:
        region: Region to query (any region works).
    """
    args = ["queue", "stats"]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "queue"})
@audit_logged
async def queue_submit(
    manifest_path: str,
    region: str,
    namespace: str | None = None,
    priority: int | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """`gco queue submit` — submit a job manifest to the global queue.

    Args:
        manifest_path: Path to the job manifest YAML.
        region: Target region for job execution.
        namespace: Kubernetes namespace (defaults to ``gco-jobs`` server-side).
        priority: Job priority (0-100, higher = more important).
        labels: Optional ``key=value`` labels to attach to the queued job.
    """
    args = ["queue", "submit", manifest_path, "-r", region]
    if namespace:
        args += ["-n", namespace]
    if priority is not None:
        args += ["--priority", str(priority)]
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

    @mcp.tool(tags={"destructive", "queue"})
    @audit_logged
    async def cancel_queue_job(job_id: str, region: str | None = None) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco queue cancel` — cancel a queued job (only works for jobs not
        yet running). Cannot be undone — the job's queue record is
        transitioned to ``cancelled`` and any pending claim attempts stop.

        Args:
            job_id: Job identifier.
            region: Region to query (any region works).
        """
        await _ctx_warning(f"Cancelling queue job {job_id!r} — this cannot be undone.")
        args = ["queue", "cancel", job_id, "-y"]
        if region:
            args += ["-r", region]
        return await asyncio.to_thread(cli_runner._run_cli, *args)
