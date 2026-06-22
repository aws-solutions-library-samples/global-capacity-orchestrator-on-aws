"""Webhook management MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "webhooks"})
@audit_logged
async def webhooks_list(region: str | None = None) -> str:
    """`gco webhooks list` — list configured webhooks.

    Args:
        region: Region to query (any region works).
    """
    args = ["webhooks", "list"]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "webhooks"})
@audit_logged
async def webhooks_get(name: str, region: str | None = None) -> str:
    """`gco webhooks get` — fetch a single webhook by name.

    Args:
        name: Webhook name.
        region: Region to query (any region works).
    """
    args = ["webhooks", "get", name]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "webhooks"})
@audit_logged
async def webhooks_create(
    name: str,
    url: str,
    events: list[str],
    region: str | None = None,
    secret_name: str | None = None,
) -> str:
    """`gco webhooks create` — register a new webhook subscription.

    Args:
        name: Webhook name.
        url: Destination URL for webhook deliveries.
        events: Event names to subscribe to (one ``--event`` flag per entry).
        region: Region to use (any region works).
        secret_name: Optional Secrets Manager secret name for HMAC signing.
    """
    args = ["webhooks", "create", name, "--url", url]
    for event in events:
        args += ["--event", event]
    if region:
        args += ["-r", region]
    if secret_name:
        args += ["--secret-name", secret_name]
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

    @mcp.tool(tags={"destructive", "webhooks"})
    @audit_logged
    async def delete_webhook(name: str, region: str | None = None) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco webhooks delete` — delete a webhook subscription.
        Cannot be undone — the webhook record is permanently removed.

        Args:
            name: Webhook identifier.
            region: Region to use (any region works).
        """
        await _ctx_warning(f"Deleting webhook {name!r} — this cannot be undone.")
        args = ["webhooks", "delete", name, "-y"]
        if region:
            args += ["-r", region]
        return await asyncio.to_thread(cli_runner._run_cli, *args)
