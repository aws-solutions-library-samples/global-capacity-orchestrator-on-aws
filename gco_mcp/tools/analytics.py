"""Analytics environment MCP tools (read-only)."""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "analytics"})
@audit_logged
async def analytics_doctor() -> str:
    """`gco analytics doctor` — run analytics environment health checks."""
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "doctor")


@mcp.tool(tags={"safe", "analytics"})
@audit_logged
async def analytics_login_url(username: str) -> str:
    """`gco analytics login-url` — get a SageMaker Studio login URL for a user.

    Args:
        username: Cognito username.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "login-url", username)


@mcp.tool(tags={"safe", "analytics"})
@audit_logged
async def analytics_users_list() -> str:
    """`gco analytics users list` — list Cognito users in the analytics user pool."""
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "users", "list")


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "analytics"})
@audit_logged
async def enable_analytics() -> str:
    """`gco stacks analytics enable` — flip the analytics environment on in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to provision the analytics stack.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "analytics", "enable", "-y")


@mcp.tool(tags={"low-risk", "analytics"})
@audit_logged
async def disable_analytics() -> str:
    """`gco stacks analytics disable` — flip the analytics environment off in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to tear down the analytics stack.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "analytics", "disable", "-y")


@mcp.tool(tags={"low-risk", "analytics"})
@audit_logged
async def analytics_user_add(username: str, email: str) -> str:
    """`gco analytics users add` — create a Cognito user in the analytics pool.

    Args:
        username: Cognito username for the new user.
        email: Email address for the new user.
    """
    return await asyncio.to_thread(
        cli_runner._run_cli, "analytics", "users", "add", username, "--email", email
    )


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

    @mcp.tool(tags={"destructive", "analytics"})
    @audit_logged
    async def analytics_user_remove(username: str) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco analytics users remove` — delete a Cognito user from the
        analytics user pool. Cannot be undone — the user record and any
        Studio profile artefacts owned by them are permanently removed.

        Args:
            username: Cognito username to remove.
        """
        await _ctx_warning(f"Removing analytics user {username!r} — this cannot be undone.")
        return await asyncio.to_thread(
            cli_runner._run_cli,
            "analytics",
            "users",
            "remove",
            "--username",
            username,
            "--yes",
        )
