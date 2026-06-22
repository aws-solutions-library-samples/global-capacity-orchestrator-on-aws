"""Job template management MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "templates"})
@audit_logged
async def templates_list(region: str | None = None) -> str:
    """`gco templates list` — list job templates.

    Args:
        region: Region to query (any region works).
    """
    args = ["templates", "list"]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "templates"})
@audit_logged
async def templates_get(name: str, region: str | None = None) -> str:
    """`gco templates get` — fetch a single job template by name.

    Args:
        name: Template name.
        region: Region to query (any region works).
    """
    args = ["templates", "get", name]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "templates"})
@audit_logged
async def templates_create(
    name: str,
    manifest_path: str,
    region: str | None = None,
    description: str | None = None,
) -> str:
    """`gco templates create` — register a new job template from a manifest.

    Args:
        name: Template name.
        manifest_path: Path to the source manifest YAML.
        region: Region to use (any region works).
        description: Optional human-readable description.
    """
    args = ["templates", "create", name, manifest_path]
    if region:
        args += ["-r", region]
    if description:
        args += ["-d", description]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"low-risk", "templates"})
@audit_logged
async def templates_run(
    name: str,
    region: str | None = None,
    override_namespace: str | None = None,
    override_priority: int | None = None,
) -> str:
    """`gco templates run` — instantiate a job from a stored template.

    Args:
        name: Template name to run.
        region: Region in which to run the resulting job.
        override_namespace: Override the namespace embedded in the template.
        override_priority: Override the priority embedded in the template.
    """
    args = ["templates", "run", name]
    if region:
        args += ["-r", region]
    if override_namespace:
        args += ["-n", override_namespace]
    if override_priority is not None:
        args += ["--priority", str(override_priority)]
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

    @mcp.tool(tags={"destructive", "templates"})
    @audit_logged
    async def delete_template(name: str, region: str | None = None) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco templates delete` — delete a job template.
        Cannot be undone — the template definition is permanently removed.

        Args:
            name: Template name.
            region: Region to use (any region works).
        """
        await _ctx_warning(f"Deleting template {name!r} — this cannot be undone.")
        args = ["templates", "delete", name, "-y"]
        if region:
            args += ["-r", region]
        return await asyncio.to_thread(cli_runner._run_cli, *args)
