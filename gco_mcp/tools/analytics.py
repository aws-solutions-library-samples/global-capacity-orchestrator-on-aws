"""Analytics environment MCP tools (read-only)."""

import asyncio
import json
import os

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "analytics"})
@audit_logged
async def analytics_doctor() -> str:
    """`gco analytics doctor` — run analytics environment health checks.

    Returns ``{"passed", "checks": [{"name", "ok", "remediation"}]}``. When
    a check fails the CLI exits 1, and the result is the error envelope
    with that document under ``output``.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "doctor")


@mcp.tool(tags={"safe", "analytics"})
@audit_logged
async def analytics_status() -> str:
    """`gco analytics status` — show the analytics environment configuration."""
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "status")


@mcp.tool(tags={"safe", "analytics"})
@audit_logged
async def analytics_login_url(username: str, password: str | None = None) -> str:
    """`gco analytics studio login` — get a SageMaker Studio presigned login URL.

    Signs the user in to Cognito (AdminInitiateAuth, with this server's AWS
    credentials) and prints the presigned Studio
    URL. The password is read from ``$GCO_STUDIO_PASSWORD`` when not passed here.
    A password passed here reaches the CLI through that variable in the
    child's environment, never on its command line. With neither, the tool
    returns ``code: password_required`` without running the CLI, whose
    password prompt the MCP server cannot answer.

    Args:
        username: Cognito username to sign in with.
        password: Cognito password. Omit to use ``$GCO_STUDIO_PASSWORD``.
    """
    if not password and not os.environ.get("GCO_STUDIO_PASSWORD"):
        return json.dumps(
            {
                "error": "analytics_login_url needs a password: pass password, or set "
                "GCO_STUDIO_PASSWORD in the MCP server's environment.",
                "code": "password_required",
            }
        )
    args = ["analytics", "studio", "login", "--username", username]
    env = {"GCO_STUDIO_PASSWORD": password} if password else None
    return await asyncio.to_thread(cli_runner._run_cli, *args, env=env)


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
    """`gco analytics enable` — flip the analytics environment on in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to provision the analytics stack.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "enable", "-y")


@mcp.tool(tags={"low-risk", "analytics"})
@audit_logged
async def disable_analytics() -> str:
    """`gco analytics disable` — flip the analytics environment off in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to tear down the analytics stack.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "analytics", "disable", "-y")


@mcp.tool(tags={"low-risk", "analytics"})
@audit_logged
async def analytics_user_add(username: str, email: str) -> str:
    """`gco analytics users add` — create a Cognito user in the analytics pool.

    Args:
        username: Cognito username for the new user. It also names the
            user's SageMaker Studio profile, so it must be letters, digits
            and hyphens, starting and ending with a letter or digit, 63
            characters at most, and must not differ only in case from an
            existing user's name.
        email: Email address for the new user.
    """
    return await asyncio.to_thread(
        cli_runner._run_cli,
        "analytics",
        "users",
        "add",
        "--username",
        username,
        "--email",
        email,
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
