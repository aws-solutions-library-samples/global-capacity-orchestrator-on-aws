"""Cluster observability MCP tools (thin wrappers over `gco monitoring`).

Mirrors :mod:`tools.analytics`: read-only tools are tagged ``safe``, cdk.json
toggles and user creation are ``low-risk``, and user deletion is ``destructive``
and gated behind ``GCO_ENABLE_DESTRUCTIVE_OPERATIONS``. There is intentionally no
``monitoring open`` tool — that is an interactive, long-running port-forward, not
a request/response operation.

The user tools read the Grafana admin Secret on the ``region`` cluster, through
an SSM tunnel to its private API endpoint when ``via_ssm`` names an SSM-managed
instance. Like the NodePool tools they never provision a bastion (``gco
monitoring users ... --via-ssm auto`` launches a billable EC2 instance), so
``via_ssm="auto"`` is refused.
"""

import asyncio
import json

import cli_runner
from audit import audit_logged
from server import mcp


def _refuse_auto_bastion(via_ssm: str | None, command: str) -> str | None:
    """Return an error envelope for ``via_ssm="auto"``, else ``None``."""
    if via_ssm != "auto":
        return None
    return json.dumps(
        {
            "error": (
                "via_ssm must be the id of an SSM-managed instance: MCP tools do not "
                f"provision a bastion. Run 'gco monitoring users {command} --via-ssm auto' "
                "yourself to have the CLI launch a self-terminating one."
            ),
            "code": "bastion_not_provisioned",
            "parameter": "via_ssm",
        }
    )


def _cluster_args(region: str | None, via_ssm: str | None) -> list[str]:
    """``--region`` / ``--via-ssm`` for the CLI's Grafana Secret read."""
    args: list[str] = []
    if region:
        args += ["--region", region]
    if via_ssm:
        args += ["--via-ssm", via_ssm]
    return args


@mcp.tool(tags={"safe", "monitoring"})
@audit_logged
async def monitoring_status() -> str:
    """`gco monitoring status` — show the cluster observability toggle + config."""
    return await asyncio.to_thread(cli_runner._run_cli, "monitoring", "status")


@mcp.tool(tags={"safe", "monitoring"})
@audit_logged
async def monitoring_users_list(region: str | None = None, via_ssm: str | None = None) -> str:
    """`gco monitoring users list` — list Grafana users via the admin API.

    Reaches Grafana over a `gco monitoring open` port-forward; the admin
    credential is read from the chart-generated Secret on the region's cluster
    (or $GCO_GRAFANA_ADMIN_PASSWORD).

    Args:
        region: Region whose cluster holds the Grafana Secret. Omit for the
            CLI's default Region (GCO_DEFAULT_REGION or the config file, else
            the first cdk.json regional entry).
        via_ssm: SSM-managed instance id to read the Secret through when the
            cluster's API endpoint is private (the default) and this host is
            outside the VPC. ``auto`` is refused: the tool never provisions a
            bastion.
    """
    refused = _refuse_auto_bastion(via_ssm, "list")
    if refused is not None:
        return refused
    return await asyncio.to_thread(
        cli_runner._run_cli, "monitoring", "users", "list", *_cluster_args(region, via_ssm)
    )


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
async def monitoring_user_add(
    username: str,
    password: str,
    email: str | None = None,
    region: str | None = None,
    via_ssm: str | None = None,
) -> str:
    """`gco monitoring users add` — create a Grafana user via the admin API.

    The password reaches the CLI through ``$GCO_GRAFANA_USER_PASSWORD`` in the
    child's environment, never on its command line. The admin credential is
    read from the Grafana Secret on the region's cluster.

    Args:
        username: Grafana login for the new user.
        password: Password to set for the new user.
        email: Optional email address for the new user.
        region: Region whose cluster holds the Grafana Secret. Omit for the
            CLI's default Region.
        via_ssm: SSM-managed instance id to read the Secret through when the
            cluster's API endpoint is private and this host is outside the
            VPC. ``auto`` is refused: the tool never provisions a bastion.
    """
    refused = _refuse_auto_bastion(via_ssm, "add")
    if refused is not None:
        return refused
    args = ["monitoring", "users", "add", "--username", username]
    if email:
        args += ["--email", email]
    args += _cluster_args(region, via_ssm)
    env = {"GCO_GRAFANA_USER_PASSWORD": password}
    return await asyncio.to_thread(cli_runner._run_cli, *args, env=env)


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
    async def monitoring_user_remove(
        username: str, region: str | None = None, via_ssm: str | None = None
    ) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco monitoring users remove` — delete a Grafana user via the admin
        API. Cannot be undone — the user and their Grafana-owned resources are
        permanently removed.

        Args:
            username: Grafana login or email to remove.
            region: Region whose cluster holds the Grafana Secret. Omit for
                the CLI's default Region.
            via_ssm: SSM-managed instance id to read the Secret through when
                the cluster's API endpoint is private and this host is outside
                the VPC. ``auto`` is refused: the tool never provisions a
                bastion.
        """
        refused = _refuse_auto_bastion(via_ssm, "remove")
        if refused is not None:
            return refused
        await _ctx_warning(f"Removing Grafana user {username!r} — this cannot be undone.")
        return await asyncio.to_thread(
            cli_runner._run_cli,
            "monitoring",
            "users",
            "remove",
            "--username",
            username,
            "--yes",
            *_cluster_args(region, via_ssm),
        )
