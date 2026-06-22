"""Infrastructure stack management MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

import cli_runner
from audit import audit_logged
from feature_flags import (
    FLAG_INFRASTRUCTURE_DEPLOY,
    FLAG_INFRASTRUCTURE_DESTROY,
    is_enabled,
)
from server import mcp

from tools._long_task import _run_long_task

# FastMCP's Progress / Context dependencies are optional from this
# module's perspective — when ``fastmcp[tasks]`` is reachable they
# inject real instances per call; otherwise the gated long-running
# tools still register but rely on caller-provided fakes (the test path).
try:
    from fastmcp.server.dependencies import CurrentContext, Progress
except ImportError:  # pragma: no cover - degraded fastmcp install
    CurrentContext = None  # type: ignore[assignment]
    Progress = None  # type: ignore[misc,assignment]

# TaskConfig opts the gated stack-lifecycle tools into FastMCP's
# task protocol with ``mode="optional"`` — clients that support the task
# protocol receive a task ID immediately and poll for progress, while
# clients without task-protocol support fall back to inline execution
# with progress streamed through FastMCP's Progress dependency.
# Required-mode would lock out clients that don't speak the task protocol
# (e.g. the GCO MCP orchestrator's ``call_tool`` proxy), and these tools
# are useful enough that the inline fallback is worth keeping.
# If the import path moves between fastmcp versions, the tools register
# without the task config and run synchronously.
try:
    from fastmcp.server.tasks.config import TaskConfig

    _TASK_CONFIG_OPTIONAL: Any = TaskConfig(mode="optional")
except ImportError:  # pragma: no cover - degraded fastmcp install
    _TASK_CONFIG_OPTIONAL = None


def _expected_stack_count_for_all() -> int | None:
    """Return the number of stacks ``deploy-all`` / ``destroy-all`` will touch.

    Reads ``cdk.json``'s ``context.deployment_regions`` and counts the
    fixed-position stacks (gco-global, gco-api-gateway, gco-monitoring)
    plus one per regional region. Returns ``None`` when the config is
    unreadable or empty so the caller falls back to indeterminate
    progress instead of an inaccurate total.

    The count drives ``progress.set_total(...)`` so MCP clients render
    a real percentage during a multi-stack deploy or destroy.
    """
    try:
        from cli.config import _load_cdk_json
    except Exception:  # noqa: BLE001 — best-effort
        return None
    try:
        cdk_regions = _load_cdk_json()
    except Exception:  # noqa: BLE001 — best-effort
        return None
    if not isinstance(cdk_regions, dict):
        return None
    regional = cdk_regions.get("regional") or []
    if not isinstance(regional, list):
        return None
    # Three fixed stacks (global / api-gateway / monitoring) plus one
    # per regional region. Analytics is opt-in and omitted from the
    # baseline count — when enabled it adds one more stack but
    # under-reporting is preferable to over-reporting (the progress
    # bar rolls over rather than stopping at 95 %).
    return 3 + len(regional)


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
def list_stacks() -> str:
    """List all GCO CDK stacks."""
    return cli_runner._run_cli("stacks", "list")


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
def stack_status(stack_name: str, region: str) -> str:
    """Get detailed status of a CloudFormation stack.

    Args:
        stack_name: Stack name (e.g. gco-us-east-1).
        region: AWS region.
    """
    return cli_runner._run_cli("stacks", "status", stack_name, "-r", region)


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
def setup_cluster_access(cluster: str | None = None, region: str | None = None) -> str:
    """Configure kubectl access to a GCO EKS cluster.

    Updates kubeconfig, creates an EKS access entry for your IAM principal,
    and associates the cluster admin policy. Handles assumed roles automatically.

    Args:
        cluster: Cluster name (default: gco-{region}).
        region: AWS region (default: first deployment region from cdk.json).
    """
    args = ["stacks", "access"]
    if cluster:
        args.extend(["-c", cluster])
    if region:
        args.extend(["-r", region])
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
def fsx_status() -> str:
    """Check FSx for Lustre configuration status."""
    return cli_runner._run_cli("stacks", "fsx", "status")


# =============================================================================
# Read-only inspection tools (async)
# =============================================================================


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
async def stack_diff(stack_name: str | None = None) -> str:
    """`gco stacks diff` — show CloudFormation diff for a stack.

    Args:
        stack_name: Stack to diff. If omitted, diffs all stacks.
    """
    args = ["stacks", "diff"]
    if stack_name:
        args.append(stack_name)
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
async def stack_outputs(stack_name: str, region: str) -> str:
    """`gco stacks outputs` — fetch CloudFormation outputs for a stack.

    Args:
        stack_name: Stack name (e.g. gco-us-east-1).
        region: AWS region.
    """
    return await asyncio.to_thread(
        cli_runner._run_cli, "stacks", "outputs", stack_name, "-r", region
    )


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
async def stack_synth(stack_name: str | None = None, quiet: bool = True) -> str:
    """`gco stacks synth` — synthesize CloudFormation templates from CDK.

    Args:
        stack_name: Stack to synthesize. If omitted, synthesizes all stacks.
        quiet: When True, pass ``--quiet`` to suppress verbose CDK output.
    """
    args = ["stacks", "synth"]
    if stack_name:
        args.append(stack_name)
    if quiet:
        args.append("--quiet")
    return await asyncio.to_thread(cli_runner._run_cli, *args)


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
async def valkey_status() -> str:
    """`gco stacks valkey status` — show Valkey cache stack status."""
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "valkey", "status")


@mcp.tool(tags={"safe", "stacks"})
@audit_logged
async def aurora_status() -> str:
    """`gco stacks aurora status` — show Aurora database stack status."""
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "aurora", "status")


# =============================================================================
# Mutating cdk.json toggles (low-risk)
# =============================================================================


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
async def enable_fsx() -> str:
    """`gco stacks fsx enable` — flip FSx Lustre on in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to provision the FSx file system.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "fsx", "enable", "-y")


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
async def disable_fsx() -> str:
    """`gco stacks fsx disable` — flip FSx Lustre off in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to remove the FSx file system.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "fsx", "disable", "-y")


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
async def enable_valkey() -> str:
    """`gco stacks valkey enable` — flip Valkey Serverless on in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to provision the Valkey cache.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "valkey", "enable", "-y")


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
async def disable_valkey() -> str:
    """`gco stacks valkey disable` — flip Valkey Serverless off in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to remove the Valkey cache.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "valkey", "disable", "-y")


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
async def enable_aurora() -> str:
    """`gco stacks aurora enable` — flip Aurora pgvector on in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to provision the Aurora cluster.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "aurora", "enable", "-y")


@mcp.tool(tags={"low-risk", "stacks"})
@audit_logged
async def disable_aurora() -> str:
    """`gco stacks aurora disable` — flip Aurora pgvector off in cdk.json.

    Note: this only edits the cdk.json toggle. The change does not take effect
    until ``gco stacks deploy-all`` runs to remove the Aurora cluster.
    """
    return await asyncio.to_thread(cli_runner._run_cli, "stacks", "aurora", "disable", "-y")


# =============================================================================
# Long-running stack lifecycle tools — gated by GCO_ENABLE_INFRASTRUCTURE_DEPLOY
# =============================================================================
#
# deploy_stack / deploy_all / bootstrap_cdk drive CDK lifecycle operations
# that exceed the short-running ``cli_runner._run_cli`` 120-second timeout.
# They run via ``_run_long_task`` so progress streams back through the
# FastMCP Progress dependency and clients can poll task status through
# the standard MCP task protocol.

if is_enabled(FLAG_INFRASTRUCTURE_DEPLOY):
    # Build the decorator kwargs dict so we only pass ``task=...`` when
    # TaskConfig was importable on this fastmcp version.
    _deploy_decorator_kwargs: dict[str, Any] = {"tags": {"infrastructure", "stacks"}}
    if _TASK_CONFIG_OPTIONAL is not None:
        _deploy_decorator_kwargs["task"] = _TASK_CONFIG_OPTIONAL

    if Progress is not None and CurrentContext is not None:

        @mcp.tool(**_deploy_decorator_kwargs)  # type: ignore[untyped-decorator]
        @audit_logged
        async def deploy_stack(
            stack_name: str,
            yes: bool = True,
            outputs_file: str | None = None,
            tags: list[str] | None = None,
            *,
            ctx: Any = CurrentContext(),
            progress: Any = Progress(),
        ) -> str:
            """[gated by GCO_ENABLE_INFRASTRUCTURE_DEPLOY] long-running.

            `gco stacks deploy` — deploy a single CDK stack to AWS.

            Typical wall-clock: 15-30 minutes per regional stack. Clients that
            speak FastMCP's task protocol can receive a task ID immediately
            and poll `tasks://gco/{task_id}` for progress; clients that don't
            run the tool inline with progress streamed through the FastMCP
            Progress dependency. Cancellation sends SIGTERM to the running
            CDK process and partial CloudFormation state may remain — inspect
            via stack_status or the AWS console.

            Args:
                stack_name: Stack to deploy (e.g. ``gco-us-east-1``).
                yes: Skip approval prompts (passes ``-y``). Defaults to True.
                outputs_file: Optional path to write stack outputs JSON.
                tags: Optional list of ``key=value`` tag strings applied to the stack.
            """
            argv = [
                "gco",
                "stacks",
                "deploy",
                stack_name,
            ]
            if yes:
                argv.append("-y")
            if outputs_file:
                argv += ["--outputs-file", outputs_file]
            for tag in tags or []:
                argv += ["--tag", tag]
            return await _run_long_task(
                argv,
                ctx=ctx,
                progress=progress,
                is_stack_op=True,
                total_units=1,
            )

        @mcp.tool(**_deploy_decorator_kwargs)  # type: ignore[untyped-decorator]
        @audit_logged
        async def deploy_all(
            yes: bool = True,
            outputs_file: str | None = None,
            tags: list[str] | None = None,
            parallel: bool = False,
            max_workers: int | None = None,
            *,
            ctx: Any = CurrentContext(),
            progress: Any = Progress(),
        ) -> str:
            """[gated by GCO_ENABLE_INFRASTRUCTURE_DEPLOY] long-running.

            `gco stacks deploy-all` — deploy every CDK stack in dependency order.

            Typical wall-clock: 30-60 minutes for a fresh multi-region deploy.
            Clients that speak FastMCP's task protocol can receive a task ID
            immediately and poll `tasks://gco/{task_id}` for progress; clients
            that don't run the tool inline with progress streamed through the
            FastMCP Progress dependency. Cancellation sends SIGTERM to the
            running CDK process and partial CloudFormation state may remain —
            inspect via stack_status or the AWS console.

            Args:
                yes: Skip approval prompts (passes ``-y``). Defaults to True.
                outputs_file: Optional path to write stack outputs JSON.
                tags: Optional list of ``key=value`` tag strings applied to every stack.
                parallel: Deploy regional stacks concurrently when True.
                max_workers: Cap on parallel deployments when ``parallel=True``.
            """
            argv = [
                "gco",
                "stacks",
                "deploy-all",
            ]
            if yes:
                argv.append("-y")
            if outputs_file:
                argv += ["--outputs-file", outputs_file]
            for tag in tags or []:
                argv += ["--tag", tag]
            if parallel:
                argv.append("--parallel")
            if max_workers is not None:
                argv += ["--max-workers", str(max_workers)]
            return await _run_long_task(
                argv,
                ctx=ctx,
                progress=progress,
                is_stack_op=True,
                total_units=_expected_stack_count_for_all(),
            )

        @mcp.tool(**_deploy_decorator_kwargs)  # type: ignore[untyped-decorator]
        @audit_logged
        async def bootstrap_cdk(
            region: str,
            account: str | None = None,
            *,
            ctx: Any = CurrentContext(),
            progress: Any = Progress(),
        ) -> str:
            """[gated by GCO_ENABLE_INFRASTRUCTURE_DEPLOY] long-running.

            `gco stacks bootstrap` — bootstrap CDK in an AWS account/region.

            Typical wall-clock: 2-5 minutes. Required before any stack can be
            deployed to a new account/region. Clients that speak FastMCP's
            task protocol can receive a task ID immediately and poll
            `tasks://gco/{task_id}` for progress; clients that don't run the
            tool inline with progress streamed through the FastMCP Progress
            dependency. Cancellation sends SIGTERM to the running CDK process
            and partial CloudFormation state may remain — inspect via
            stack_status or the AWS console.

            Args:
                region: Target AWS region.
                account: Optional AWS account ID. Defaults to the caller's account.
            """
            argv = ["gco", "stacks", "bootstrap", "--region", region]
            if account:
                argv += ["--account", account]
            return await _run_long_task(
                argv,
                ctx=ctx,
                progress=progress,
                is_stack_op=True,
                total_units=1,
            )


# =============================================================================
# Long-running stack lifecycle tools — gated by GCO_ENABLE_INFRASTRUCTURE_DESTROY
# =============================================================================

if is_enabled(FLAG_INFRASTRUCTURE_DESTROY):
    _destroy_decorator_kwargs: dict[str, Any] = {"tags": {"infrastructure", "stacks"}}
    if _TASK_CONFIG_OPTIONAL is not None:
        _destroy_decorator_kwargs["task"] = _TASK_CONFIG_OPTIONAL

    if Progress is not None and CurrentContext is not None:

        @mcp.tool(**_destroy_decorator_kwargs)  # type: ignore[untyped-decorator]
        @audit_logged
        async def destroy_stack(
            stack_name: str,
            yes: bool = True,
            *,
            ctx: Any = CurrentContext(),
            progress: Any = Progress(),
        ) -> str:
            """[gated by GCO_ENABLE_INFRASTRUCTURE_DESTROY] long-running.

            `gco stacks destroy` — destroy a single CDK stack.

            Typical wall-clock: 5-20 minutes per stack. Clients that speak
            FastMCP's task protocol can receive a task ID immediately and
            poll `tasks://gco/{task_id}` for progress; clients that don't
            run the tool inline with progress streamed through the FastMCP
            Progress dependency. Cancellation sends SIGTERM to the running
            CDK process and partial CloudFormation state may remain —
            inspect via stack_status or the AWS console before retrying.

            Args:
                stack_name: Stack to destroy (e.g. ``gco-us-east-1``).
                yes: Skip the confirmation prompt (passes ``-y``). Defaults to True.
            """
            argv = ["gco", "stacks", "destroy", stack_name]
            if yes:
                argv.append("-y")
            return await _run_long_task(
                argv,
                ctx=ctx,
                progress=progress,
                is_stack_op=True,
                total_units=1,
            )

        @mcp.tool(**_destroy_decorator_kwargs)  # type: ignore[untyped-decorator]
        @audit_logged
        async def destroy_all(
            yes: bool = True,
            parallel: bool = False,
            max_workers: int | None = None,
            *,
            ctx: Any = CurrentContext(),
            progress: Any = Progress(),
        ) -> str:
            """[gated by GCO_ENABLE_INFRASTRUCTURE_DESTROY] long-running.

            `gco stacks destroy-all` — destroy every CDK stack in reverse dependency order.

            Typical wall-clock: 20-40 minutes for a multi-region teardown.
            Clients that speak FastMCP's task protocol can receive a task
            ID immediately and poll `tasks://gco/{task_id}` for progress;
            clients that don't run the tool inline with progress streamed
            through the FastMCP Progress dependency. Cancellation sends
            SIGTERM to the running CDK process and partial CloudFormation
            state may remain — inspect via stack_status or the AWS console
            before retrying.

            Args:
                yes: Skip the confirmation prompt (passes ``-y``). Defaults to True.
                parallel: Destroy regional stacks concurrently when True.
                max_workers: Cap on parallel destructions when ``parallel=True``.
            """
            argv = ["gco", "stacks", "destroy-all"]
            if yes:
                argv.append("-y")
            if parallel:
                argv.append("--parallel")
            if max_workers is not None:
                argv += ["--max-workers", str(max_workers)]
            return await _run_long_task(
                argv,
                ctx=ctx,
                progress=progress,
                is_stack_op=True,
                total_units=_expected_stack_count_for_all(),
            )
