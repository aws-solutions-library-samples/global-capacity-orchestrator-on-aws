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
def get_job_pods(job_name: str, region: str, namespace: str = "gco-jobs") -> str:
    """Get pod details, placement, and container status for a job.

    Args:
        job_name: Name of the owning Kubernetes Job.
        region: AWS region where the job is running.
        namespace: Kubernetes namespace.
    """
    return cli_runner._run_cli("jobs", "pods", job_name, "-r", region, "-n", namespace)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def get_pod_logs(
    job_name: str,
    pod_name: str,
    region: str,
    namespace: str = "gco-jobs",
    tail: int = 100,
    container: str | None = None,
) -> str:
    """Get a bounded log tail from one specific pod belonging to a job.

    Args:
        job_name: Name of the owning Kubernetes Job.
        pod_name: Exact pod name returned by ``get_job_pods``.
        region: AWS region where the pod is running.
        namespace: Kubernetes namespace.
        tail: Maximum number of log lines to return.
        container: Container name for a multi-container pod.
    """
    args = [
        "jobs",
        "pod-logs",
        job_name,
        pod_name,
        "-r",
        region,
        "-n",
        namespace,
        "--tail",
        str(tail),
    ]
    if container:
        args += ["--container", container]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def get_job_metrics(job_name: str, region: str, namespace: str = "gco-jobs") -> str:
    """Get CPU and memory usage for all pods in a job.

    Requires metrics-server in the target cluster.

    Args:
        job_name: Name of the Kubernetes Job.
        region: AWS region where the job is running.
        namespace: Kubernetes namespace.
    """
    return cli_runner._run_cli("jobs", "metrics", job_name, "-r", region, "-n", namespace)


@mcp.tool(tags={"low-risk", "jobs"})
@audit_logged
def retry_job(job_name: str, region: str, namespace: str = "gco-jobs") -> str:
    """Retry a failed job by creating a new Job while preserving the original.

    Args:
        job_name: Failed Kubernetes Job to retry.
        region: AWS region where the job ran.
        namespace: Kubernetes namespace.
    """
    return cli_runner._run_cli("jobs", "retry", job_name, "-r", region, "-n", namespace, "--yes")


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
def get_job_validation_policy(region: str) -> str:
    """Get the job validation policy a region actually enforces, as deployed.

    Use this before submitting to check whether a manifest will be admitted,
    rather than paying to provision a region and discovering the conflict at
    submit time. Returns the per-manifest cpu/memory/gpu caps,
    allowed_namespaces, allowed_kinds, trusted_registries, the pod-security
    block_* flags, and the namespace's live ResourceQuota / LimitRange
    ceilings.

    This reads the deployed cluster, not a local cdk.json. The two diverge
    whenever a stack was deployed from a different checkout, and CDK augments
    trusted_registries with the project's own ECR hostnames at synth time, so
    the effective allowlist is strictly larger than the configured one.

    A manifest must clear all three layers: the front-door policy, the
    per-container LimitRange, and the aggregate ResourceQuota.

    Args:
        region: AWS region (e.g. us-east-1).
    """
    return cli_runner._run_cli("jobs", "policy", "-r", region)


@mcp.tool(tags={"safe", "jobs"})
@audit_logged
def check_job_policy(
    manifest_path: str,
    regions: list[str] | None = None,
    namespace: str | None = None,
    offline: bool = False,
) -> str:
    """Check which regions would admit a manifest, and whether regions agree.

    Answers two questions get_job_validation_policy leaves to the caller.

    Which regions would take this job: the same manifest is evaluated against
    each region's deployed policy using the code the manifest processor runs,
    so a job that is admissible in one region and over-cap in another is
    reported as such instead of being discovered by submitting.

    Whether the regions still agree: there are no per-region policy overrides,
    so any field that differs across regions means a region was deployed from a
    different checkout of cdk.json. That is invisible until a manifest that
    worked yesterday is refused. trusted_registries is compared with ECR
    hostnames stripped, since CDK adds those per deployment.

    Advisory. The cluster is the authoritative gate and this reads a snapshot
    of its policy, so a reject here is a strong signal, not a verdict.

    Args:
        manifest_path: Path to a manifest file or a directory of them.
        regions: Regions to check. Omit for every configured region.
        namespace: Namespace to assume for manifests that don't declare one.
        offline: Read cdk.json instead of calling AWS. Needs no credentials,
            but reports the CONFIGURED policy rather than the deployed one, and
            a deployed region trusts ECR registries cdk.json never mentions --
            so an image rejection may be a false positive.
    """
    args = ["jobs", "check-policy", manifest_path]
    for region in regions or []:
        args += ["-r", region]
    if namespace:
        args += ["-n", namespace]
    if offline:
        args.append("--offline")
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
