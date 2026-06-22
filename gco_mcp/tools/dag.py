"""DAG (multi-step job pipeline) MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "dag"})
@audit_logged
async def dag_validate(manifest_path: str) -> str:
    """`gco dag validate` — statically validate a DAG manifest.

    Args:
        manifest_path: Path to the DAG manifest file.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "dag", "validate", manifest_path)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "dag"})
@audit_logged
async def dag_run(manifest_path: str, region: str, dry_run: bool = False) -> str:
    """`gco dag run` — execute a DAG manifest end-to-end.

    Args:
        manifest_path: Path to the DAG manifest file.
        region: Target region for the DAG run.
        dry_run: When True, validate and plan without submitting jobs.
    """
    args = ["dag", "run", manifest_path, "-r", region]
    if dry_run:
        args.append("--dry-run")
    return await asyncio.to_thread(cli_runner._run_cli, *args)
