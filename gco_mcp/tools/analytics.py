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
async def analytics_login_url(
    username: str, password: str | None = None, new_password: str | None = None
) -> str:
    """`gco analytics studio login` — get a SageMaker Studio presigned login URL.

    Signs the user in to Cognito (AdminInitiateAuth, with this server's AWS
    credentials) and prints the presigned Studio
    URL. The password is read from ``$GCO_STUDIO_PASSWORD`` when not passed here.
    A password passed here reaches the CLI through that variable in the
    child's environment, never on its command line. With neither, the tool
    returns ``code: password_required`` without running the CLI, whose
    password prompt the MCP server cannot answer.

    An account that still has its temporary password must choose a new one
    before Cognito signs it in: pass it as ``new_password`` (it reaches the
    CLI as ``$GCO_STUDIO_NEW_PASSWORD``) and it becomes the account's
    password. Without it such an account gets an error that says so.

    Args:
        username: Cognito username to sign in with.
        password: Cognito password. Omit to use ``$GCO_STUDIO_PASSWORD``.
        new_password: The new password for an account with a temporary one;
            it must meet the pool's password policy (12 characters or more,
            with upper and lower case letters, a digit and a symbol).
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
    secrets = {"GCO_STUDIO_PASSWORD": password, "GCO_STUDIO_NEW_PASSWORD": new_password}
    env = {name: value for name, value in secrets.items() if value} or None
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

    Cognito emails the user an invitation with a temporary password, which the
    result also carries once (``password``); the user chooses a new password
    at the first sign-in (``analytics_login_url`` with ``new_password``).

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
    async def analytics_user_remove(username: str, keep_studio_profile: bool = False) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco analytics users remove` — delete a Cognito user from the
        analytics user pool, and with it the user's SageMaker Studio profile,
        the spaces it owns and their apps. Cannot be undone. A profile whose
        owner tag names another Cognito user is left in place, and the files
        under /home/<username> on the Studio EFS stay. When the profile
        cannot be deleted the Cognito user is kept and the tool returns the
        error. Deleting the apps and spaces can take a few minutes.

        Args:
            username: Cognito username to remove.
            keep_studio_profile: Delete only the Cognito user.
        """
        await _ctx_warning(f"Removing analytics user {username!r} — this cannot be undone.")
        args = ["analytics", "users", "remove", "--username", username, "--yes"]
        if keep_studio_profile:
            args.append("--keep-studio-profile")
        # The SageMaker deletions wait for apps and spaces to go.
        return await asyncio.to_thread(cli_runner._run_cli, *args, timeout_seconds=1200)
