"""Slurm MCP tools (wrappers over ``gco slurm``).

The Slinky Slurm cluster GCO deploys when ``cdk.json`` ``helm.slurm.enabled``
is on, reached through the GCO API's ``/api/v1/slurm/*`` routes: every tool
is pinned to one region because each region runs its own cluster. The
interactive ``gco slurm shell`` and the ``--follow`` form of ``gco slurm
output`` have no tool on purpose (they hold a terminal open over the cluster
tunnel); ``slurm_cancel`` is gated like every other destructive operation.
"""

import asyncio
import contextlib

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_DESTRUCTIVE_OPERATIONS, is_enabled
from server import mcp


def _args(*parts: str | None, region: str) -> list[str]:
    """``gco slurm <parts...> -r REGION``, dropping omitted optional parts."""
    return ["slurm", *[part for part in parts if part is not None], "-r", region]


# =============================================================================
# Read-only tools
# =============================================================================


@mcp.tool(tags={"safe", "slurm"})
@audit_logged
async def slurm_status(region: str) -> str:
    """`gco slurm status` — controller reachability, partitions, node and job counts.

    Args:
        region: Region whose Slurm cluster to read (one cluster per region).
    """
    return await asyncio.to_thread(cli_runner._run_cli, *_args("status", region=region))


@mcp.tool(tags={"safe", "slurm"})
@audit_logged
async def slurm_partitions(region: str) -> str:
    """`gco slurm partitions` — partitions with state, time limit and node counts (sinfo).

    An autoscaled partition reads zero nodes while idle: the first pending job
    makes KEDA scale its NodeSet up.

    Args:
        region: Region whose Slurm cluster to read.
    """
    return await asyncio.to_thread(cli_runner._run_cli, *_args("partitions", region=region))


@mcp.tool(tags={"safe", "slurm"})
@audit_logged
async def slurm_nodes(region: str, partition: str | None = None) -> str:
    """`gco slurm nodes` — Slurm nodes (slurmd worker pods) with state, resources and gres.

    Args:
        region: Region whose Slurm cluster to read.
        partition: Only nodes in this partition.
    """
    args = _args("nodes", region=region)
    if partition:
        args += ["--partition", partition]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "slurm"})
@audit_logged
async def slurm_queue(
    region: str,
    state: str | None = None,
    partition: str | None = None,
    name: str | None = None,
    limit: int | None = None,
) -> str:
    """`gco slurm queue` — the live queue as slurmctld sees it (squeue).

    Args:
        region: Region whose Slurm cluster to read.
        state: Only jobs in this state (PENDING, RUNNING, COMPLETING, ...).
        partition: Only jobs in this partition.
        name: Only jobs with exactly this name.
        limit: Maximum jobs returned (default 200).
    """
    args = _args("queue", region=region)
    if state:
        args += ["--state", state]
    if partition:
        args += ["--partition", partition]
    if name:
        args += ["--name", name]
    if limit is not None:
        args += ["--limit", str(limit)]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "slurm"})
@audit_logged
async def slurm_job(region: str, job_id: int) -> str:
    """`gco slurm job` — one job in full (state, TRES, times, exit code, output paths).

    Args:
        region: Region whose Slurm cluster to read.
        job_id: The Slurm job id.
    """
    return await asyncio.to_thread(cli_runner._run_cli, *_args("job", str(job_id), region=region))


@mcp.tool(tags={"safe", "slurm"})
@audit_logged
async def slurm_accounting(
    region: str,
    since: str | None = None,
    start: str | None = None,
    end: str | None = None,
    user: str | None = None,
    partition: str | None = None,
    state: str | None = None,
    name: str | None = None,
    limit: int | None = None,
) -> str:
    """`gco slurm accounting` — job history from slurmdbd (sacct).

    Args:
        region: Region whose Slurm cluster to read.
        since: Relative window such as 30m, 24h, 7d or 2w (default 1d unless start is given).
        start: Window start as a Slurm time (2026-10-01, 2026-10-01T08:00, now-2days).
        end: Window end as a Slurm time (default now).
        user: Only this Slurm user (API submissions run as ``slurm``; the
            submitting caller is in each row's comment).
        partition: Only this partition.
        state: Only these states, comma-separated (COMPLETED,FAILED,TIMEOUT).
        name: Only jobs with exactly this name.
        limit: Maximum rows (default 200).
    """
    args = _args("accounting", region=region)
    for flag, value in (
        ("--since", since),
        ("--start", start),
        ("--end", end),
        ("--user", user),
        ("--partition", partition),
        ("--state", state),
        ("--name", name),
    ):
        if value:
            args += [flag, value]
    if limit is not None:
        args += ["--limit", str(limit)]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "slurm"})
@audit_logged
async def slurm_submit(
    region: str,
    script_path: str,
    name: str | None = None,
    partition: str | None = None,
    nodes: int | None = None,
    tasks: int | None = None,
    cpus_per_task: int | None = None,
    gpus_per_node: int | None = None,
    memory: str | None = None,
    time_limit: str | None = None,
    working_directory: str | None = None,
    environment: list[str] | None = None,
    comment: str | None = None,
    exclusive: bool = False,
) -> str:
    """`gco slurm submit` — submit a local batch script to Slurm (sbatch).

    The script's leading ``#SBATCH`` directives are honoured; the arguments
    here override them. The job runs as Slurm's own user with the caller
    identity in its comment, and allocates compute (an idle autoscaled
    partition scales up for it).

    Args:
        region: Region whose Slurm cluster runs the job.
        script_path: Local path of the batch script (an interpreter line first).
        name: Job name (default: the script's #SBATCH --job-name, else gco-job).
        partition: Partition (default: the cluster's default partition).
        nodes: Node count.
        tasks: Task count (--ntasks).
        cpus_per_task: CPUs per task.
        gpus_per_node: GPUs per node.
        memory: Memory per node: MiB or a K/M/G/T suffix such as 64G.
        time_limit: Wall time: minutes, HH:MM:SS or D-HH:MM:SS.
        working_directory: Working directory on the worker (default /home/slurm).
        environment: Extra environment variables as NAME=VALUE strings.
        comment: Free-text comment appended after the caller identity.
        exclusive: Whole-node allocation.
    """
    args = _args("submit", script_path, region=region)
    for flag, value in (
        ("--name", name),
        ("--partition", partition),
        ("--mem", memory),
        ("--time", time_limit),
        ("--chdir", working_directory),
        ("--comment", comment),
    ):
        if value:
            args += [flag, value]
    for flag, number in (
        ("--nodes", nodes),
        ("--tasks", tasks),
        ("--cpus-per-task", cpus_per_task),
        ("--gpus-per-node", gpus_per_node),
    ):
        if number is not None:
            args += [flag, str(number)]
    for item in environment or []:
        args += ["--env", item]
    if exclusive:
        args.append("--exclusive")
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Destructive tools — gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS
# =============================================================================


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

    @mcp.tool(tags={"destructive", "slurm"})
    @audit_logged
    async def slurm_cancel(region: str, job_id: int, signal: str | None = None) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco slurm cancel` — cancel a Slurm job (scancel). A running job's
        work is lost; a pending job leaves the queue.

        Args:
            region: Region whose Slurm cluster runs the job.
            job_id: The Slurm job id.
            signal: Signal to send instead of the default (e.g. SIGUSR1).
        """
        await _ctx_warning(f"Cancelling Slurm job {job_id} in {region} — its work is lost.")
        args = [*_args("cancel", str(job_id), region=region), "-y"]
        if signal:
            args += ["--signal", signal]
        return await asyncio.to_thread(cli_runner._run_cli, *args)
