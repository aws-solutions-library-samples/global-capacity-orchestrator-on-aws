"""Job management commands."""

import logging
import sys
from collections.abc import Mapping
from typing import Any

import click

from ..config import GCOConfig
from ..jobs import get_job_manager, resolve_submission_identity
from ..output import format_job_table, get_output_formatter

logger = logging.getLogger(__name__)

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _resolve_result_namespace(result: Any, fallback: str) -> str:
    """Pick the submitted Job namespace without assuming mapping resources."""
    _job_name, namespace = resolve_submission_identity(result, fallback_namespace=fallback)
    return namespace or fallback


def _resolve_result_job_name(result: Any) -> str | None:
    """Pick the generated/submitted Job name from a submission response."""
    job_name, _namespace = resolve_submission_identity(result)
    return job_name


@click.group()
@pass_config
def jobs(config: Any) -> None:
    """Manage jobs across GCO clusters."""
    pass


@jobs.command("submit")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option(
    "--namespace",
    "-n",
    help="Fallback namespace for manifests that don't declare their own",
)
@click.option("--region", "-r", "target_region", help="Target specific region")
@click.option("--dry-run", is_flag=True, help="Validate without applying")
@click.option("--label", "-l", multiple=True, help="Add labels (key=value)")
@click.option("--wait", "-w", is_flag=True, help="Wait for job completion")
@click.option("--timeout", default=3600, help="Wait timeout in seconds")
@pass_config
def submit_job(
    config: Any,
    manifest_path: Any,
    namespace: Any,
    target_region: Any,
    dry_run: Any,
    label: Any,
    wait: Any,
    timeout: Any,
) -> None:
    """Submit a job to GCO.

    MANIFEST_PATH can be a YAML file or directory containing YAML files.
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    # Parse labels
    labels = {}
    for lbl in label:
        if "=" in lbl:
            k, v = lbl.split("=", 1)
            labels[k] = v

    try:
        result = job_manager.submit_job(
            manifests=manifest_path,
            namespace=namespace,
            target_region=target_region,
            dry_run=dry_run,
            labels=labels if labels else None,
        )

        if dry_run:
            formatter.print_success("Dry run successful - manifests are valid")
        else:
            formatter.print_success("Job submitted successfully")

        # Surface any rename warnings from mapping-shaped API resources.
        # Direct kubectl responses contain strings in ``resources``.
        resources = result.get("resources", []) if isinstance(result, Mapping) else []
        for resource in resources:
            if not isinstance(resource, Mapping):
                continue
            msg = str(resource.get("message", ""))
            if "renamed" in msg.lower() or "still running" in msg.lower():
                formatter.print_warning(msg)

        formatter.print(result)

        # Wait for completion if requested
        if wait and not dry_run:
            job_name = _resolve_result_job_name(result)
            if job_name:
                # The API response tells us exactly where the resource landed
                # (may differ from --namespace since the manifest's own value
                # takes precedence). Fall back to the CLI flag or the config
                # default only if the response didn't include a namespace.
                resolved_ns = _resolve_result_namespace(
                    result, fallback=namespace or config.default_namespace
                )
                formatter.print_info(f"Waiting for job {job_name} to complete...")
                final_job = job_manager.wait_for_job(
                    job_name=job_name,
                    namespace=resolved_ns,
                    region=target_region,
                    timeout_seconds=timeout,
                )
                formatter.print_success(f"Job completed with status: {final_job.status}")

    except Exception as e:
        formatter.print_error(f"Failed to submit job: {e}")
        sys.exit(1)


@jobs.command("submit-direct")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option("--region", "-r", required=True, help="Target region for direct submission")
@click.option(
    "--namespace",
    "-n",
    help="Fallback namespace for manifests that don't declare their own",
)
@click.option("--dry-run", is_flag=True, help="Validate without applying")
@click.option("--label", "-l", multiple=True, help="Add labels (key=value)")
@click.option("--wait", "-w", is_flag=True, help="Wait for job completion")
@click.option("--timeout", default=3600, help="Wait timeout in seconds")
@pass_config
def submit_job_direct(
    config: Any,
    manifest_path: Any,
    region: Any,
    namespace: Any,
    dry_run: Any,
    label: Any,
    wait: Any,
    timeout: Any,
) -> None:
    """Submit a job directly to a regional cluster using kubectl.

    This bypasses the API Gateway and submits directly to the EKS cluster.

    REQUIREMENTS:
    - kubectl installed and in PATH
    - EKS access entry configured for your IAM principal
    - AWS credentials with eks:DescribeCluster permission

    To configure EKS access, run:

        aws eks create-access-entry --cluster-name gco-REGION --principal-arn YOUR_ARN

        aws eks associate-access-policy --cluster-name gco-REGION \\
            --principal-arn YOUR_ARN \\
            --policy-arn arn:<partition>:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \\
            --access-scope type=cluster

    Examples:
        gco jobs submit-direct job.yaml --region us-east-1
        gco jobs submit-direct job.yaml -r us-west-2 -n gco-jobs --wait
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    # Parse labels
    labels = {}
    for lbl in label:
        if "=" in lbl:
            k, v = lbl.split("=", 1)
            labels[k] = v

    try:
        formatter.print_info(f"Submitting directly to cluster in {region} via kubectl...")

        result = job_manager.submit_job_direct(
            manifests=manifest_path,
            region=region,
            namespace=namespace,
            dry_run=dry_run,
            labels=labels if labels else None,
        )

        if dry_run:
            formatter.print_success("Dry run successful - manifests are valid")
        else:
            formatter.print_success(f"Job submitted directly to {region}")

        # Surface any warnings (e.g. job was renamed due to name collision)
        # without mutating or assuming the shape of the direct result.
        warnings = result.get("warnings", []) if isinstance(result, Mapping) else []
        for warning in warnings:
            formatter.print_warning(str(warning))

        formatter.print(result)

        # Wait for completion if requested
        if wait and not dry_run:
            job_name = _resolve_result_job_name(result)
            if job_name:
                resolved_ns = _resolve_result_namespace(
                    result, fallback=namespace or config.default_namespace
                )
                formatter.print_info(f"Waiting for job {job_name} to complete...")
                final_job = job_manager.wait_for_job(
                    job_name=job_name,
                    namespace=resolved_ns,
                    region=region,
                    timeout_seconds=timeout,
                )
                formatter.print_success(f"Job completed with status: {final_job.status}")

    except Exception as e:
        formatter.print_error(f"Failed to submit job directly: {e}")
        sys.exit(1)


@jobs.command("submit-sqs")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option("--region", "-r", help="Target region (auto-selects optimal if not specified)")
@click.option(
    "--namespace",
    "-n",
    help="Fallback namespace for manifests that don't declare their own",
)
@click.option("--label", "-l", multiple=True, help="Add labels (key=value)")
@click.option("--priority", "-p", default=0, help="Job priority (higher = more important)")
@click.option("--auto-region", is_flag=True, help="Auto-select optimal region based on capacity")
@pass_config
def submit_job_sqs(
    config: Any,
    manifest_path: Any,
    region: Any,
    namespace: Any,
    label: Any,
    priority: Any,
    auto_region: Any,
) -> None:
    """Submit a job to a regional SQS queue for processing.

    This is the recommended way to submit jobs as it:
    - Decouples submission from processing
    - Enables KEDA-based autoscaling
    - Provides better fault tolerance

    If --auto-region is specified, the CLI will analyze capacity across all
    regions and submit to the optimal one.

    Examples:
        gco jobs submit-sqs job.yaml --region us-east-1
        gco jobs submit-sqs job.yaml --auto-region
        gco jobs submit-sqs job.yaml -r us-west-2 --priority 10
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    # Parse labels
    labels = {}
    for lbl in label:
        if "=" in lbl:
            k, v = lbl.split("=", 1)
            labels[k] = v

    try:
        # Auto-select region if requested
        if auto_region and not region:
            formatter.print_info("Analyzing capacity across regions...")
            from ..capacity import get_capacity_checker

            checker = get_capacity_checker(config)
            recommendation = checker.recommend_region_for_job()
            region = recommendation["region"]
            formatter.print_info(f"Selected region: {region} ({recommendation['reason']})")
        elif not region:
            region = config.default_region

        formatter.print_info(f"Submitting job to SQS queue in {region}...")

        result = job_manager.submit_job_sqs(
            manifests=manifest_path,
            region=region,
            namespace=namespace,
            labels=labels if labels else None,
            priority=priority,
        )

        formatter.print_success(f"Job queued successfully in {region}")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to submit job to SQS: {e}")
        sys.exit(1)


@jobs.command("queue-status")
@click.option("--region", "-r", help="Specific region to check")
@click.option("--all-regions", "-a", is_flag=True, help="Check all regions")
@pass_config
def queue_status(config: Any, region: Any, all_regions: Any) -> None:
    """Show job queue status across regions.

    Displays the number of pending, in-flight, and failed messages
    in the job queues.

    Examples:
        gco jobs queue-status --region us-east-1
        gco jobs queue-status --all-regions
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        if all_regions:
            from ..aws_client import get_aws_client

            aws_client = get_aws_client(config)
            stacks = aws_client.discover_regional_stacks()

            results = []
            for stack_region in stacks:
                try:
                    status = job_manager.get_queue_status(stack_region)
                    results.append(status)
                except Exception as e:
                    logger.debug("Failed to get queue status for %s: %s", stack_region, e)
                    continue

            if not results:
                formatter.print_warning("No queue status available")
                return

            # Format as table
            print("\n  REGION          PENDING  IN-FLIGHT  DELAYED  DLQ")
            print("  " + "-" * 55)
            for r in results:
                dlq = r.get("dlq_messages", 0)
                print(
                    f"  {r['region']:<15} {r['messages_available']:>7}  "
                    f"{r['messages_in_flight']:>9}  {r['messages_delayed']:>7}  {dlq:>3}"
                )
        else:
            target_region = region or config.default_region
            status = job_manager.get_queue_status(target_region)
            formatter.print(status)

    except Exception as e:
        formatter.print_error(f"Failed to get queue status: {e}")
        sys.exit(1)


@jobs.command("list")
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--region", "-r", help="Target region (required unless --all-regions)")
@click.option("--status", "-s", type=click.Choice(["pending", "running", "succeeded", "failed"]))
@click.option("--all-regions", "-a", is_flag=True, help="Query all regions via global API")
@click.option("--limit", "-l", default=50, help="Maximum jobs to return")
@pass_config
def list_jobs(
    config: Any, namespace: Any, region: Any, status: Any, all_regions: Any, limit: Any
) -> None:
    """List jobs in GCO clusters.

    You must specify either --region for a specific cluster or --all-regions
    to query all clusters via the global aggregation API.

    Examples:
        gco jobs list --region us-east-1
        gco jobs list --all-regions
        gco jobs list -r us-west-2 -n gco-jobs --status running
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    # Require explicit region or --all-regions
    if not region and not all_regions:
        formatter.print_error("You must specify --region or --all-regions")
        formatter.print_info("  Use --region/-r to query a specific cluster")
        formatter.print_info("  Use --all-regions/-a to query all clusters")
        sys.exit(1)

    try:
        if all_regions:
            # Use global aggregation API
            result = job_manager.list_jobs_global(
                namespace=namespace,
                status=status,
                limit=limit,
            )

            if config.output_format == "table":
                # Print summary
                print("\n  Global Jobs Summary")
                print("  " + "-" * 50)
                print(f"  Total jobs: {result.get('total', 0)}")
                print(f"  Regions queried: {result.get('regions_queried', 0)}")
                print(f"  Regions successful: {result.get('regions_successful', 0)}")

                # Print region summaries
                if result.get("region_summaries"):
                    print("\n  REGION          COUNT  TOTAL")
                    print("  " + "-" * 35)
                    for r in result["region_summaries"]:
                        print(f"  {r['region']:<15} {r['count']:>5}  {r['total']:>5}")

                # Print jobs
                jobs_data = result.get("jobs", [])
                if jobs_data:
                    print(
                        "\n  NAME                           NAMESPACE       REGION          STATUS"
                    )
                    print("  " + "-" * 75)
                    for job in jobs_data[:limit]:
                        name = job.get("metadata", {}).get("name", "")[:30]
                        ns = job.get("metadata", {}).get("namespace", "")[:14]
                        job_region = job.get("_source_region", "")[:14]
                        job_status = job.get("computed_status", "unknown")[:10]
                        print(f"  {name:<30} {ns:<15} {job_region:<15} {job_status}")

                # Print errors if any
                if result.get("errors"):
                    print("\n  Errors:")
                    for err in result["errors"]:
                        formatter.print_warning(f"  {err['region']}: {err['error']}")
            else:
                formatter.print(result)
        else:
            # Query specific region
            jobs_list = job_manager.list_jobs(
                region=region, namespace=namespace, status=status, all_regions=False
            )

            if config.output_format == "table":
                print(format_job_table(jobs_list))
            else:
                formatter.print(jobs_list)

    except Exception as e:
        formatter.print_error(f"Failed to list jobs: {e}")
        sys.exit(1)


@jobs.command("get")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@pass_config
def get_job(config: Any, job_name: Any, namespace: Any, region: Any) -> None:
    """Get details of a specific job.

    Examples:
        gco jobs get my-job --region us-east-1
        gco jobs get training-job -r us-west-2 -n ml-jobs
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        job = job_manager.get_job(job_name, namespace, region)
        if job:
            formatter.print(job)
        else:
            formatter.print_error(f"Job {job_name} not found")
            sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to get job: {e}")
        sys.exit(1)


@jobs.command("logs")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@click.option("--tail", "-t", default=100, help="Number of lines to show")
@click.option(
    "--since", "-s", default=24, type=int, help="Hours to look back in CloudWatch (default: 24)"
)
@click.option("--container", "-c", help="Container name (for multi-container pods)")
@pass_config
def get_logs(
    config: Any, job_name: Any, namespace: Any, region: Any, tail: Any, since: Any, container: Any
) -> None:
    """Get logs from a job.

    Fetches logs from the Kubernetes API if the pod is still running.
    If the pod is gone, falls back to CloudWatch Logs automatically.
    Use --since to control how far back CloudWatch searches.

    Examples:
        gco jobs logs my-job --region us-east-1
        gco jobs logs training-job -r us-west-2 -n ml-jobs --tail 500
        gco jobs logs old-job -r us-east-1 --since 72
        gco jobs logs multi-container-job -r us-east-1 --container sidecar
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        logs = job_manager.get_job_logs(
            job_name, namespace, region, tail_lines=tail, since_hours=since
        )
        print(logs)
    except Exception as e:
        formatter.print_error(f"Failed to get logs: {e}")
        sys.exit(1)


@jobs.command("delete")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def delete_job(config: Any, job_name: Any, namespace: Any, region: Any, yes: Any) -> None:
    """Delete a job.

    Examples:
        gco jobs delete my-job --region us-east-1
        gco jobs delete old-job -r us-west-2 -n ml-jobs -y
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    if not yes:
        click.confirm(f"Delete job {job_name} in namespace {namespace} ({region})?", abort=True)

    try:
        job_manager.delete_job(job_name, namespace, region)
        formatter.print_success(f"Job {job_name} deleted")
    except Exception as e:
        formatter.print_error(f"Failed to delete job: {e}")
        sys.exit(1)


@jobs.command("events")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@pass_config
def get_job_events(config: Any, job_name: Any, namespace: Any, region: Any) -> None:
    """Get Kubernetes events for a job.

    Shows events related to the job and its pods, useful for debugging
    scheduling issues, resource problems, or startup failures.

    Examples:
        gco jobs events my-job --region us-east-1
        gco jobs events training-job -n ml-jobs -r us-west-2
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        result = job_manager.get_job_events(job_name, namespace, region)

        if config.output_format == "table":
            events = result.get("events", [])
            if not events:
                formatter.print_info("No events found for this job")
                return

            print(f"\n  Events for {job_name} ({result.get('count', 0)} total)")
            print("  " + "-" * 70)
            for event in events:
                event_type = event.get("type") or "Normal"
                reason = (event.get("reason") or "")[:20]
                message = (event.get("message") or "")[:50]
                timestamp = (event.get("lastTimestamp") or event.get("firstTimestamp") or "")[:19]
                marker = "⚠" if event_type == "Warning" else "✓"
                print(f"  {marker} [{timestamp}] {reason:<20} {message}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get job events: {e}")
        sys.exit(1)


@jobs.command("pods")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@pass_config
def get_job_pods(config: Any, job_name: Any, namespace: Any, region: Any) -> None:
    """Get pod details for a job.

    Shows all pods created by the job with their status, node placement,
    and container information.

    Examples:
        gco jobs pods my-job -r us-east-1
        gco jobs pods training-job -n ml-jobs -r us-west-2
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        result = job_manager.get_job_pods(job_name, namespace, region)

        if config.output_format == "table":
            pods = result.get("pods", [])
            if not pods:
                formatter.print_info("No pods found for this job")
                return

            print(f"\n  Pods for {job_name} ({result.get('count', 0)} total)")
            print("  " + "-" * 80)
            print(
                "  NAME                                    NODE                    STATUS     RESTARTS"
            )
            print("  " + "-" * 80)
            for pod in pods:
                name = (pod.get("metadata", {}).get("name") or "")[:40]
                node = (pod.get("spec", {}).get("nodeName") or "")[:22]
                phase = (pod.get("status", {}).get("phase") or "Unknown")[:10]
                restarts = sum(
                    c.get("restartCount", 0)
                    for c in (pod.get("status", {}).get("containerStatuses") or [])
                )
                print(f"  {name:<40} {node:<23} {phase:<10} {restarts}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get job pods: {e}")
        sys.exit(1)


@jobs.command("pod-logs")
@click.argument("job_name")
@click.argument("pod_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@click.option("--tail", "-t", default=100, help="Number of lines to show")
@click.option("--container", "-c", help="Container name (for multi-container pods)")
@pass_config
def get_pod_logs_cmd(
    config: Any,
    job_name: Any,
    pod_name: Any,
    namespace: Any,
    region: Any,
    tail: Any,
    container: Any,
) -> None:
    """Get logs from a specific pod of a job.

    Use 'gco jobs pods' first to list available pods, then use this
    command to get logs from a specific pod.

    Examples:
        gco jobs pod-logs my-job my-job-abc123 -r us-east-1
        gco jobs pod-logs training-job training-job-xyz789 -r us-west-2 --tail 500
        gco jobs pod-logs multi-job multi-job-pod1 -r us-east-1 --container sidecar
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        result = job_manager.get_pod_logs(
            job_name=job_name,
            pod_name=pod_name,
            namespace=namespace,
            region=region,
            tail_lines=tail,
            container=container,
        )

        # Print logs directly
        logs = result.get("logs", "")
        if logs:
            print(logs)
        else:
            formatter.print_info("No logs available")

    except Exception as e:
        formatter.print_error(f"Failed to get pod logs: {e}")
        sys.exit(1)


@jobs.command("metrics")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@pass_config
def get_job_metrics(config: Any, job_name: Any, namespace: Any, region: Any) -> None:
    """Get resource usage metrics for a job.

    Shows CPU and memory usage for all pods in the job. Requires
    metrics-server to be installed in the cluster.

    Examples:
        gco jobs metrics my-job --region us-east-1
        gco jobs metrics training-job -n ml-jobs -r us-west-2
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    try:
        result = job_manager.get_job_metrics(job_name, namespace, region)

        if config.output_format == "table":
            summary = result.get("summary", {})
            pods = result.get("pods", [])

            print(f"\n  Resource Metrics for {job_name}")
            print("  " + "-" * 50)
            print(f"  Total CPU: {summary.get('total_cpu_millicores', 0)}m")
            print(f"  Total Memory: {summary.get('total_memory_mib', 0):.1f} MiB")
            print(f"  Pod Count: {summary.get('pod_count', 0)}")

            if pods:
                print("\n  POD                                     CPU(m)    MEMORY(MiB)")
                print("  " + "-" * 65)
                for pod in pods:
                    pod_name = pod.get("pod_name", "")[:40]
                    cpu = sum(c.get("cpu_millicores", 0) for c in pod.get("containers", []))
                    mem = sum(c.get("memory_mib", 0) for c in pod.get("containers", []))
                    print(f"  {pod_name:<40} {cpu:>6}    {mem:>10.1f}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get job metrics: {e}")
        sys.exit(1)


@jobs.command("retry")
@click.argument("job_name")
@click.option("--namespace", "-n", default="gco-jobs", help="Job namespace")
@click.option("--region", "-r", required=True, help="Job region (required)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def retry_job(config: Any, job_name: Any, namespace: Any, region: Any, yes: Any) -> None:
    """Retry a failed job.

    Creates a new job from the failed job's spec with a new name.
    The original job is preserved for debugging.

    Examples:
        gco jobs retry failed-job --region us-east-1
        gco jobs retry training-job -n ml-jobs -r us-west-2 -y
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    if not yes:
        click.confirm(f"Retry job {job_name} in namespace {namespace} ({region})?", abort=True)

    try:
        result = job_manager.retry_job(job_name, namespace, region)

        if result.get("success"):
            formatter.print_success(f"Job retry created: {result.get('new_job')}")
        else:
            formatter.print_error(f"Failed to retry job: {result.get('message')}")
            sys.exit(1)

        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to retry job: {e}")
        sys.exit(1)


@jobs.command("bulk-delete")
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--status", "-s", type=click.Choice(["completed", "succeeded", "failed"]))
@click.option("--older-than-days", "-d", type=int, help="Delete jobs older than N days")
@click.option("--label-selector", "-l", help="Kubernetes label selector")
@click.option("--region", "-r", help="Target region (required unless --all-regions)")
@click.option("--all-regions", "-a", is_flag=True, help="Delete across all regions")
@click.option("--dry-run", is_flag=True, default=True, help="Only show what would be deleted")
@click.option("--execute", is_flag=True, help="Actually delete (disables dry-run)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def bulk_delete_jobs(
    config: Any,
    namespace: Any,
    status: Any,
    older_than_days: Any,
    label_selector: Any,
    region: Any,
    all_regions: Any,
    dry_run: Any,
    execute: Any,
    yes: Any,
) -> None:
    """Bulk delete jobs based on filters.

    You must specify either --region for a specific cluster or --all-regions
    to delete across all clusters.

    By default runs in dry-run mode. Use --execute to actually delete.

    Examples:
        gco jobs bulk-delete --region us-east-1 --status completed --older-than-days 7
        gco jobs bulk-delete -r us-west-2 -n gco-jobs -s failed --execute -y
        gco jobs bulk-delete --all-regions --status failed --older-than-days 30 --execute
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    # Require explicit region or --all-regions
    if not region and not all_regions:
        formatter.print_error("You must specify --region or --all-regions")
        formatter.print_info("  Use --region/-r to delete from a specific cluster")
        formatter.print_info("  Use --all-regions/-a to delete across all clusters")
        sys.exit(1)

    # --execute disables dry-run
    if execute:
        dry_run = False

    if not dry_run and not yes:
        scope = f"region {region}" if region else "ALL regions"
        click.confirm(
            f"This will permanently delete matching jobs in {scope}. Continue?", abort=True
        )

    try:
        if region:
            # Single region delete
            result = job_manager.bulk_delete_jobs(
                namespace=namespace,
                status=status,
                older_than_days=older_than_days,
                label_selector=label_selector,
                region=region,
                dry_run=dry_run,
            )
        else:
            # Global delete across all regions
            result = job_manager.bulk_delete_global(
                namespace=namespace,
                status=status,
                older_than_days=older_than_days,
                label_selector=label_selector,
                dry_run=dry_run,
            )

        if dry_run:
            formatter.print_info("DRY RUN - No jobs were deleted")
            formatter.print_info(f"Would delete {result.get('total_matched', 0)} jobs")
        else:
            formatter.print_success(
                f"Deleted {result.get('deleted_count', result.get('total_deleted', 0))} jobs"
            )

        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to bulk delete jobs: {e}")
        sys.exit(1)


@jobs.command("health")
@click.option("--region", "-r", help="Target region (required unless --all-regions)")
@click.option("--all-regions", "-a", is_flag=True, help="Get health across all regions")
@pass_config
def job_health(config: Any, region: Any, all_regions: Any) -> None:
    """Get health status of GCO clusters.

    You must specify either --region for a specific cluster or --all-regions
    to get health status across all clusters.

    Examples:
        gco jobs health --region us-east-1
        gco jobs health --all-regions
    """
    formatter = get_output_formatter(config)
    job_manager = get_job_manager(config)

    # Require explicit region or --all-regions
    if not region and not all_regions:
        formatter.print_error("You must specify --region or --all-regions")
        formatter.print_info("  Use --region/-r to check a specific cluster")
        formatter.print_info("  Use --all-regions/-a to check all clusters")
        sys.exit(1)

    try:
        if all_regions:
            result = job_manager.get_global_health()

            if config.output_format == "table":
                print(
                    f"\n  Global Health Status: {result.get('overall_status', 'unknown').upper()}"
                )
                print("  " + "-" * 50)
                print(
                    f"  Healthy regions: {result.get('healthy_regions', 0)}/{result.get('total_regions', 0)}"
                )

                regions = result.get("regions", [])
                if regions:
                    print("\n  REGION          STATUS       CLUSTER")
                    print("  " + "-" * 50)
                    for r in regions:
                        status_icon = "✓" if r.get("status") == "healthy" else "✗"
                        print(
                            f"  {status_icon} {r.get('region', ''):<13} {r.get('status', ''):<12} {r.get('cluster_id', '')}"
                        )
            else:
                formatter.print(result)
        else:
            # Single region health check via API
            result = job_manager._aws_client.get_health(region=region)
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get health status: {e}")
        sys.exit(1)


@jobs.command("submit-queue")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option("--region", "-r", required=True, help="Target region for job execution")
@click.option("--namespace", "-n", default="gco-jobs", help="Kubernetes namespace")
@click.option("--priority", "-p", default=0, help="Job priority (0-100, higher = more important)")
@click.option("--label", "-l", multiple=True, help="Add labels (key=value)")
@pass_config
def submit_job_queue(
    config: Any, manifest_path: Any, region: Any, namespace: Any, priority: Any, label: Any
) -> None:
    """Submit a job to the global DynamoDB queue for regional pickup.

    Jobs are stored in DynamoDB and picked up by the target region's
    manifest processor. This enables global job submission with
    centralized tracking and status history.

    This is different from submit-sqs which uses regional SQS queues.
    The DynamoDB queue provides:
    - Global visibility of all queued jobs
    - Status tracking and history
    - Priority-based scheduling
    - Cross-region job management

    Use 'gco queue list' to view queued jobs and their status.

    Examples:
        gco jobs submit-queue job.yaml --region us-east-1
        gco jobs submit-queue job.yaml -r us-west-2 --priority 50
        gco jobs submit-queue job.yaml -r us-east-1 -l team=ml -l project=training
    """

    from gco.services.manifest_processor import safe_load_yaml

    formatter = get_output_formatter(config)

    # Parse labels
    labels = {}
    for lbl in label:
        if "=" in lbl:
            k, v = lbl.split("=", 1)
            labels[k] = v

    try:
        # Load manifest
        with open(manifest_path, encoding="utf-8") as f:
            manifest = safe_load_yaml(f, allow_aliases=False)

        # Submit via API
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        result = aws_client.call_api(
            method="POST",
            path="/api/v1/queue/jobs",
            region=region,
            body={
                "manifest": manifest,
                "target_region": region,
                "namespace": namespace,
                "priority": priority,
                "labels": labels if labels else None,
            },
        )

        formatter.print_success(f"Job queued for {region}")
        formatter.print_info("Use 'gco queue list' or 'gco queue get <job_id>' to track status")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to queue job: {e}")
        sys.exit(1)
