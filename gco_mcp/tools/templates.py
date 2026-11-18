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
    rejected = cli_runner.reject_traversal_segments(name=name)
    if rejected is not None:
        return rejected
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
        manifest_path: Path to the source manifest YAML, relative to the
            project root (an absolute path must also be inside it).
        region: Region to use (any region works).
        description: Optional human-readable description.
    """
    rejected = cli_runner.reject_paths_outside_project_root(manifest_path=manifest_path)
    if rejected is not None:
        return rejected
    # The CLI takes the manifest as its only positional argument and the
    # template name as --name.
    args = ["templates", "create", manifest_path, "--name", name]
    if region:
        args += ["-r", region]
    if description:
        args += ["-d", description]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"low-risk", "templates"})
@audit_logged
async def templates_run(
    template_name: str,
    job_name: str,
    region: str,
    namespace: str | None = None,
    params: list[str] | None = None,
) -> str:
    """`gco templates run` — instantiate and run a job from a stored template.

    Args:
        template_name: Name of the stored template to instantiate.
        job_name: Name for the resulting job.
        region: Region in which to run the job.
        namespace: Override the Kubernetes namespace (default: gco-jobs).
        params: Parameter overrides as ``key=value`` strings (one ``-p`` each).
    """
    rejected = cli_runner.reject_traversal_segments(template_name=template_name)
    if rejected is not None:
        return rejected
    args = ["templates", "run", template_name, "--name", job_name, "-r", region]
    if namespace:
        args += ["--namespace", namespace]
    for param in params or []:
        args += ["-p", param]
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
        rejected = cli_runner.reject_traversal_segments(name=name)
        if rejected is not None:
            return rejected
        await _ctx_warning(f"Deleting template {name!r} — this cannot be undone.")
        args = ["templates", "delete", name, "-y"]
        if region:
            args += ["-r", region]
        return await asyncio.to_thread(cli_runner._run_cli, *args)
