"""DAG (multi-step job pipeline) MCP tools.

``dag_validate`` is read-only. ``dag_run`` submits the DAG's jobs and waits for
each step, so a real run goes through the long-task runner (progress, task
status on disk, cancellation) instead of the CLI runner's two-minute timeout.
"""

import asyncio
from pathlib import Path
from typing import Any

import cli_runner
import yaml
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


def _reject_paths_outside_project_root(manifest_path: str) -> str | None:
    """Confine ``manifest_path`` and every step manifest the DAG file names.

    ``gco dag validate`` checks that each ``steps[].manifest`` exists and
    ``gco dag run`` submits it, both relative to the project root the CLI
    runs from. Only the DAG file itself used to be confined, so a confined
    DAG could still name, test for or submit any file on the host. Each step
    manifest gets the same check as ``manifest_path``, reported as
    ``steps[<i>].manifest``. A DAG file that cannot be read or parsed is left
    to the CLI, which reports it.
    """
    rejected = cli_runner.reject_paths_outside_project_root(manifest_path=manifest_path)
    if rejected is not None:
        return rejected
    path = Path(manifest_path)
    if not path.is_absolute():
        path = cli_runner.PROJECT_ROOT / path
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, yaml.YAMLError:
        return None
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list):
        return None
    references = {
        f"steps[{index}].manifest": step["manifest"]
        for index, step in enumerate(steps)
        if isinstance(step, dict) and isinstance(step.get("manifest"), str)
    }
    return cli_runner.reject_paths_outside_project_root(**references)


@mcp.tool(tags={"safe", "dag"})
@audit_logged
async def dag_validate(manifest_path: str) -> str:
    """`gco dag validate` — statically validate a DAG manifest.

    Args:
        manifest_path: Path to the DAG manifest file, relative to the project
            root (an absolute path must also be inside it). Every step
            manifest it names must be inside the project root too.
    """
    rejected = _reject_paths_outside_project_root(manifest_path)
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
            root (an absolute path must also be inside it). Every step
            manifest it names must be inside the project root too.
        region: Target region for the DAG run.
        dry_run: When True, validate and plan without submitting jobs.
    """
    rejected = _reject_paths_outside_project_root(manifest_path)
    if rejected is not None:
        return rejected
    if dry_run:
        return await asyncio.to_thread(
            cli_runner._run_cli, "dag", "run", manifest_path, "-r", region, "--dry-run"
        )
    argv = ["gco", "dag", "run", manifest_path, "-r", region]
    return await _run_long_task(argv, ctx=ctx, progress=progress, is_stack_op=False)
