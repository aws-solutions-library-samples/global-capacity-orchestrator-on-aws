"""DAG (multi-step job pipeline) MCP tools.

``dag_validate`` is read-only. ``dag_run`` submits the DAG's jobs and waits for
each step, so a real run goes through the long-task runner (progress, task
status on disk, cancellation) instead of the CLI runner's two-minute timeout.
"""

import asyncio
from typing import Any

import cli_runner
from audit import audit_logged

# FastMCP's Progress / Context dependencies inject real instances per
# call inside an MCP request; unit tests supply caller-provided fakes.
from fastmcp.server.dependencies import CurrentContext, Progress

# TaskConfig lets clients that speak the MCP tasks extension (registered in
# gco_mcp/server.py) run dag_run as a background task; others run it inline
# with streamed progress.
from fastmcp.utilities.tasks import TaskConfig
from server import mcp

from tools._long_task import _run_long_task

_TASK_CONFIG_OPTIONAL = TaskConfig(mode="optional")


@mcp.tool(tags={"safe", "dag"})
@audit_logged
async def dag_validate(manifest_path: str) -> str:
    """`gco dag validate` — statically validate a DAG manifest.

    Args:
        manifest_path: Path to the DAG manifest file, relative to the project
            root (an absolute path must also be inside it).
    """
    rejected = cli_runner.reject_paths_outside_project_root(manifest_path=manifest_path)
    if rejected is not None:
        return rejected
    return await asyncio.to_thread(cli_runner._run_cli, "dag", "validate", manifest_path)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "dag"}, task=_TASK_CONFIG_OPTIONAL)
@audit_logged
async def dag_run(
    manifest_path: str,
    region: str,
    dry_run: bool = False,
    *,
    ctx: Any = CurrentContext(),
    progress: Any = Progress(),
) -> str:
    """`gco dag run` — execute a DAG manifest end-to-end.

    long-running. The CLI submits each step's job and waits for it (up to an
    hour per step), so a run is a long-running task: progress messages carry
    the CLI's step lines, and the result is ``{"status": "ok", "task_id",
    "duration_seconds", ...}``. A failed step raises a tool error with the
    exit code and the last stderr lines. ``task_tail(task_id)`` shows the
    step log. Cancelling the call stops the CLI; jobs it already submitted
    keep running. With ``dry_run`` nothing is submitted and the tool returns
    the CLI's validation document at once.

    Args:
        manifest_path: Path to the DAG manifest file, relative to the project
            root (an absolute path must also be inside it).
        region: Target region for the DAG run.
        dry_run: When True, validate and plan without submitting jobs.
    """
    rejected = cli_runner.reject_paths_outside_project_root(manifest_path=manifest_path)
    if rejected is not None:
        return rejected
    if dry_run:
        return await asyncio.to_thread(
            cli_runner._run_cli, "dag", "run", manifest_path, "-r", region, "--dry-run"
        )
    argv = ["gco", "dag", "run", manifest_path, "-r", region]
    return await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)
