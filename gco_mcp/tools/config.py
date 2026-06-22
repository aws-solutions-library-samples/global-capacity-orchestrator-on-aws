"""CLI configuration MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "config"})
@audit_logged
async def config_get(key: str | None = None) -> str:
    """`gco config get` — read a CLI configuration value.

    Args:
        key: Configuration key to read. If omitted, returns the full config.
    """
    args = ["config", "get"]
    if key:
        args.append(key)
    return await asyncio.to_thread(cli_runner._run_cli, *args)
