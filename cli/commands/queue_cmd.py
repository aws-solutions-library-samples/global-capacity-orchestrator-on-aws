"""Global job queue commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _parse_labels(raw_labels: tuple[str, ...]) -> dict[str, str]:
    """Parse repeated ``--label KEY=VALUE`` options without dropping bad input."""
    labels: dict[str, str] = {}
    for raw_label in raw_labels:
        key, separator, value = raw_label.partition("=")
        if not separator or not key or not value:
            raise click.BadParameter(
                "must be KEY=VALUE with a non-empty key and value",
                param_hint="--label",
            )
        labels[key] = value
    return labels


@click.group()
@pass_config
def queue(config: Any) -> None:
    """Manage the global job queue (DynamoDB-backed).

    The job queue provides centralized job submission and tracking:
    - Submit jobs to any region from anywhere
    - Track job status globally
    - View job history and statistics
    """
    pass


@queue.command("submit")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option("--region", "-r", required=True, help="Target region for job execution")
@click.option("--namespace", "-n", default="gco-jobs", help="Kubernetes namespace")
@click.option("--priority", "-p", default=0, help="Job priority (0-100, higher = more important)")
@click.option("--label", "-l", multiple=True, help="Add labels (key=value)")
@click.option(
    "--max-spot-price",
    type=float,
    help=(
        "Spot price cap in USD/hour. The job is held in the queue until the "
        "current spot price of --spot-instance-type in the target region "
        "drops to or below this value. Requires --spot-instance-type."
    ),
)
@click.option(
    "--spot-instance-type",
    help=(
        "EC2 instance type whose spot price gates dispatch (e.g. g5.xlarge). "
        "Requires --max-spot-price."
    ),
)
@pass_config
def queue_submit(
    config: Any,
    manifest_path: Any,
    region: Any,
    namespace: Any,
    priority: Any,
    label: Any,
    max_spot_price: Any,
    spot_instance_type: Any,
) -> None:
    """Submit a job to the global queue for regional pickup.

    Jobs are stored in DynamoDB and picked up by the target region's
    manifest processor. This enables global job submission with
    centralized tracking.

    With --max-spot-price and --spot-instance-type the job is cost-gated:
    it stays queued until spot pricing for that instance type in the target
    region drops to or below the cap. Cancel with `gco queue cancel` if the
    price never clears.

    Examples:
        gco queue submit job.yaml --region us-east-1
        gco queue submit job.yaml -r us-west-2 --priority 50
        gco queue submit job.yaml -r us-east-1 -l team=ml -l project=training
        gco queue submit job.yaml -r us-east-1 --max-spot-price 0.50 --spot-instance-type g5.xlarge
    """

    from gco.services.manifest_processor import safe_load_yaml
    from gco.services.spot_price_gate import validate_spot_gate_fields

    formatter = get_output_formatter(config)

    gate_error = validate_spot_gate_fields(max_spot_price, spot_instance_type)
    if gate_error:
        formatter.print_error(gate_error)
        sys.exit(1)

    # Parse labels before opening the manifest or constructing an AWS client.
    labels = _parse_labels(tuple(label))

    try:
        # Load manifest
        with open(manifest_path, encoding="utf-8") as f:
            manifest = safe_load_yaml(f, allow_aliases=False)

        # Submit via API
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        body = {
            "manifest": manifest,
            "target_region": region,
            "namespace": namespace,
            "priority": priority,
            "labels": labels if labels else None,
        }
        if max_spot_price is not None:
            body["max_spot_price"] = max_spot_price
            body["spot_instance_type"] = spot_instance_type

        result = aws_client.call_api(
            method="POST",
            path="/api/v1/queue/jobs",
            region=region if config.use_regional_api else None,
            body=body,
        )

        formatter.print_success(f"Job queued for {region}")
        if max_spot_price is not None:
            formatter.print_info(
                f"Spot price gate: dispatches when {spot_instance_type} spot "
                f"price in {region} is <= ${max_spot_price}/hour"
            )
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to queue job: {e}")
        sys.exit(1)


@queue.command("list")
@click.option("--region", "-r", help="Filter by target region")
@click.option(
    "--status",
    "-s",
    type=click.Choice(["queued", "claimed", "running", "succeeded", "failed", "cancelled"]),
    help="Filter by status",
)
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--limit", "-l", default=50, help="Maximum results")
@pass_config
def queue_list(config: Any, region: Any, status: Any, namespace: Any, limit: Any) -> None:
    """List jobs in the global queue.

    Examples:
        gco queue list
        gco queue list --region us-east-1 --status queued
        gco queue list -s running
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        # Build query params
        params = {"limit": limit}
        if region:
            params["target_region"] = region
        if status:
            params["status"] = status
        if namespace:
            params["namespace"] = namespace

        # The region is a DynamoDB filter, not a transport pin. The global API
        # can serve it; forced regional mode uses the configured default bridge.
        query_region = config.default_region if config.use_regional_api else None
        result = aws_client.call_api(
            method="GET",
            path="/api/v1/queue/jobs",
            region=query_region,
            params=params,
        )

        if config.output_format == "table":
            jobs = result.get("jobs", [])
            if not jobs:
                formatter.print_info("No jobs found")
                return

            print(f"\n  Queued Jobs ({result.get('count', 0)} total)")
            print("  " + "-" * 90)
            print(
                "  JOB ID                               NAME                    REGION          STATUS"
            )
            print("  " + "-" * 90)
            for job in jobs:
                job_id = job.get("job_id", "")[:36]
                name = job.get("job_name", "")[:22]
                target = job.get("target_region", "")[:14]
                job_status = job.get("status", "")[:10]
                print(f"  {job_id:<36} {name:<23} {target:<15} {job_status}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to list queued jobs: {e}")
        sys.exit(1)


@queue.command("get")
@click.argument("job_id")
@click.option("--region", "-r", help="Region to query (any region works)")
@pass_config
def queue_get(config: Any, job_id: Any, region: Any) -> None:
    """Get details of a queued job including status history.

    Examples:
        gco queue get abc123-def456
        gco queue get abc123-def456 --region us-east-1
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        result = aws_client.call_api(
            method="GET",
            path=f"/api/v1/queue/jobs/{job_id}",
            region=query_region,
        )

        job = result.get("job", {})

        if config.output_format == "table":
            print(f"\n  Job: {job.get('job_id')}")
            print("  " + "-" * 50)
            print(f"  Name:          {job.get('job_name')}")
            print(f"  Target Region: {job.get('target_region')}")
            print(f"  Namespace:     {job.get('namespace')}")
            print(f"  Status:        {job.get('status')}")
            print(f"  Priority:      {job.get('priority')}")
            print(f"  Submitted:     {job.get('submitted_at')}")
            if job.get("spot_max_price"):
                print(
                    f"  Spot Gate:     {job.get('spot_instance_type')} <= "
                    f"${job.get('spot_max_price')}/hour"
                )
                if job.get("spot_gate_observed_price"):
                    print(
                        f"  Last Price:    ${job.get('spot_gate_observed_price')} "
                        f"(checked {job.get('spot_gate_checked_at')})"
                    )
            if job.get("claimed_by"):
                print(f"  Claimed By:    {job.get('claimed_by')}")
            if job.get("completed_at"):
                print(f"  Completed:     {job.get('completed_at')}")
            if job.get("error_message"):
                print(f"  Error:         {job.get('error_message')}")

            # Show status history
            history = job.get("status_history", [])
            if history:
                print("\n  Status History:")
                for entry in history:
                    ts = entry.get("timestamp", "")[:19]
                    st = entry.get("status", "")
                    msg = entry.get("message", "")[:40]
                    print(f"    [{ts}] {st}: {msg}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get job: {e}")
        sys.exit(1)


@queue.command("cancel")
@click.argument("job_id")
@click.option("--reason", help="Cancellation reason")
@click.option("--region", "-r", help="Region to query (any region works)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def queue_cancel(config: Any, job_id: Any, reason: Any, region: Any, yes: Any) -> None:
    """Cancel a queued job (only works for jobs not yet running).

    Examples:
        gco queue cancel abc123-def456
        gco queue cancel abc123-def456 --reason "No longer needed"
    """
    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Cancel job {job_id}?", abort=True)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        params = {}
        if reason:
            params["reason"] = reason

        result = aws_client.call_api(
            method="DELETE",
            path=f"/api/v1/queue/jobs/{job_id}",
            region=query_region,
            params=params,
        )

        formatter.print_success(f"Job {job_id} cancelled")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to cancel job: {e}")
        sys.exit(1)


@queue.command("stats")
@click.option("--region", "-r", help="Region to query (any region works)")
@pass_config
def queue_stats(config: Any, region: Any) -> None:
    """Get job queue statistics by region and status.

    Examples:
        gco queue stats
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        result = aws_client.call_api(
            method="GET",
            path="/api/v1/queue/stats",
            region=query_region,
        )

        if config.output_format == "table":
            summary = result.get("summary", {})
            by_region = result.get("by_region", {})

            print("\n  Job Queue Statistics")
            print("  " + "-" * 50)
            print(f"  Total Jobs:   {summary.get('total_jobs', 0)}")
            print(f"  Queued:       {summary.get('total_queued', 0)}")
            print(f"  Running:      {summary.get('total_running', 0)}")

            if by_region:
                print("\n  By Region:")
                print("  REGION          QUEUED  RUNNING  SUCCEEDED  FAILED")
                print("  " + "-" * 55)
                for reg, statuses in by_region.items():
                    queued = statuses.get("queued", 0)
                    running = statuses.get("running", 0)
                    succeeded = statuses.get("succeeded", 0)
                    failed = statuses.get("failed", 0)
                    print(f"  {reg:<15} {queued:>6}  {running:>7}  {succeeded:>9}  {failed:>6}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get queue stats: {e}")
        sys.exit(1)
