"""Cluster observability MCP tools (thin wrappers over `gco monitoring`).

Mirrors :mod:`tools.analytics`: read-only tools are tagged ``safe``, cdk.json
toggles and user creation are ``low-risk``, and user deletion is ``destructive``
and gated behind ``GCO_ENABLE_DESTRUCTIVE_OPERATIONS``. There is intentionally no
``monitoring open`` tool — that is an interactive, long-running port-forward, not
a request/response operation.
"""

import asyncio

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "monitoring"})
@audit_logged
async def monitoring_status() -> str:
    """`gco monitoring status` — show the cluster observability toggle + config."""
    return await asyncio.to_thread(cli_runner._run_cli, "monitoring", "status")


@mcp.tool(tags={"safe", "monitoring"})
@audit_logged
async def monitoring_users_list() -> str:
    """`gco monitoring users list` — list Grafana users via the admin API.

    Reaches Grafana over a `gco monitoring open` port-forward; the admin
    credential is read from the chart-generated Secret (or $GCO_GRAFANA_ADMIN_PASSWORD).
    """
    return await asyncio.to_thread(cli_runner._run_cli, "monitoring", "users", "list")


# =============================================================================
# Mutating tools (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "monitoring"})
@audit_logged
async def enable_monitoring() -> str:
    """`gco monitoring enable` — flip cluster observability on in cdk.json.

    Note: this only edits the cdk.json toggle. It does not take effect until
    ``gco stacks deploy`` (or deploy-all) reinstalls kube-prometheus-stack.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "monitoring", "enable", "-y")


@mcp.tool(tags={"low-risk", "monitoring"})
@audit_logged
async def disable_monitoring() -> str:
    """`gco monitoring disable` — flip cluster observability off in cdk.json.

    Note: this only edits the cdk.json toggle. The in-cluster stack is removed
    on the next ``gco stacks deploy``.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "monitoring", "disable", "-y")


@mcp.tool(tags={"low-risk", "monitoring"})
@audit_logged
async def monitoring_user_add(username: str, password: str, email: str | None = None) -> str:
    """`gco monitoring users add` — create a Grafana user via the admin API.

    Args:
        username: Grafana login for the new user.
        password: Password to set for the new user.
        email: Optional email address for the new user.
    """
    args = ["monitoring", "users", "add", "--username", username, "--password", password]
    if email:
        args += ["--email", email]
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

    @mcp.tool(tags={"destructive", "monitoring"})
    @audit_logged
    async def monitoring_user_remove(username: str) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco monitoring users remove` — delete a Grafana user via the admin
        API. Cannot be undone — the user and their Grafana-owned resources are
        permanently removed.

        Args:
            username: Grafana login or email to remove.
        """
        await _ctx_warning(f"Removing Grafana user {username!r} — this cannot be undone.")
        return await asyncio.to_thread(
            cli_runner._run_cli,
            "monitoring",
            "users",
            "remove",
            "--username",
            username,
            "--yes",
        )
