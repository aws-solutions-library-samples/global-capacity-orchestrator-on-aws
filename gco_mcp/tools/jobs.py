"""Job management MCP tools."""

import asyncio
import contextlib

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_DESTRUCTIVE_OPERATIONS, is_enabled
from server import mcp


async def _ctx_warning(message: str) -> None:
    """Emit ``ctx.warning(...)`` from inside a tool body, no-op when no Context.

    The destructive ``delete_job`` tool runs short — we don't need the
    full long-task progress stack, just an audited warning back to the
    operator (and the audit log via the middleware spy).
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return
    with contextlib.suppress(Exception):
        await ctx.warning(message)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def list_jobs(
    region: str | None = None, namespace: str | None = None, status: str | None = None
) -> str:
    """List jobs across GCO clusters.

    Args:
        region: AWS region (e.g. us-east-1). If omitted, lists across all regions.
        namespace: Filter by Kubernetes namespace.
        status: Filter by job status (pending, running, completed, succeeded, failed).
    """
    args = ["jobs", "list"]
    if region:
        args += ["-r", region]
    else:
        args += ["--all-regions"]
    if namespace:
        args += ["-n", namespace]
    if status:
        args += ["-s", status]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"low-risk", "jobs"})
@audit_logged
def submit_job_sqs(
    manifest_path: str, region: str, namespace: str | None = None, priority: int | None = None
) -> str:
    """Submit a job via SQS queue (recommended for production).

    Args:
        manifest_path: Path to the YAML manifest file (relative to project root).
        region: Target AWS region for the SQS queue.
        namespace: Override the namespace in the manifest.
        priority: Job priority (0-100, higher = more important).
    """
    args = ["jobs", "submit-sqs", manifest_path, "-r", region]
    if namespace:
        args += ["-n", namespace]
    if priority is not None:
        args += ["--priority", str(priority)]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"low-risk", "jobs"})
@audit_logged
def submit_job_api(manifest_path: str, namespace: str | None = None) -> str:
    """Submit a job via the authenticated API Gateway (SigV4).

    Args:
        manifest_path: Path to the YAML manifest file.
        namespace: Override the namespace in the manifest.
    """
    args = ["jobs", "submit", manifest_path]
    if namespace:
        args += ["-n", namespace]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def get_job(job_name: str, region: str, namespace: str = "gco-jobs") -> str:
    """Get details of a specific job.

    Args:
        job_name: Name of the job.
        region: AWS region where the job is running.
        namespace: Kubernetes namespace.
    """
    return cli_runner._run_cli("jobs", "get", job_name, "-r", region, "-n", namespace)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def get_job_logs(job_name: str, region: str, namespace: str = "gco-jobs", tail: int = 100) -> str:
    """Get logs from a job.

    Args:
        job_name: Name of the job.
        region: AWS region.
        namespace: Kubernetes namespace.
        tail: Number of log lines to return.
    """
    return cli_runner._run_cli(
        "jobs", "logs", job_name, "-r", region, "-n", namespace, "--tail", str(tail)
    )


if is_enabled(FLAG_DESTRUCTIVE_OPERATIONS):

    @mcp.tool(tags={"destructive", "jobs"})
    @audit_logged
    async def delete_job(job_name: str, region: str, namespace: str = "gco-jobs") -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        Delete a job. Cannot be undone — the Kubernetes Job and its pods
        are removed and any pod logs not yet shipped to CloudWatch are lost.

        Args:
            job_name: Name of the job to delete.
            region: AWS region.
            namespace: Kubernetes namespace.
        """
        await _ctx_warning(
            f"Deleting job {job_name!r} in {region}/{namespace} — this cannot be undone."
        )
        return await asyncio.to_thread(
            cli_runner._run_cli, "jobs", "delete", job_name, "-r", region, "-n", namespace, "-y"
        )


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def get_job_events(job_name: str, region: str, namespace: str = "gco-jobs") -> str:
    """Get Kubernetes events for a job (useful for debugging).

    Args:
        job_name: Name of the job.
        region: AWS region.
        namespace: Kubernetes namespace.
    """
    return cli_runner._run_cli("jobs", "events", job_name, "-r", region, "-n", namespace)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def cluster_health(region: str | None = None) -> str:
    """Get health status of GCO clusters.

    Args:
        region: Specific region, or omit for all regions.
    """
    args = ["jobs", "health"]
    if region:
        args += ["-r", region]
    else:
        args += ["--all-regions"]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def queue_status(region: str | None = None) -> str:
    """View SQS queue status (pending, in-flight, DLQ counts).

    Args:
        region: Specific region, or omit for all regions.
    """
    args = ["jobs", "queue-status"]
    if region:
        args += ["-r", region]
    else:
        args += ["--all-regions"]
    return cli_runner._run_cli(*args)
