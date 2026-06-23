"""Capacity checking commands."""

import sys
from typing import Any

import click
from botocore.exceptions import ClientError

from ..capacity import get_capacity_checker
from ..config import GCOConfig
from ..output import format_capacity_table, get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def capacity(config: Any) -> None:
    """Check EC2 capacity availability."""
    pass


@capacity.command("check")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option(
    "--type",
    "-t",
    "capacity_type",
    type=click.Choice(["spot", "on-demand", "both"]),
    default="both",
    help="Capacity type to check",
)
@click.option(
    "--enrich-historical",
    is_flag=True,
    help="Append historical capacity context (requires historical.enabled)",
)
@pass_config
def check_capacity(
    config: Any,
    instance_type: Any,
    region: Any,
    capacity_type: Any,
    enrich_historical: Any,
) -> None:
    """Check capacity availability for an instance type.

    Provides estimates based on spot price history and availability patterns.
    """
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        estimates = checker.estimate_capacity(instance_type, region, capacity_type)

        if config.output_format == "table":
            print(format_capacity_table(estimates))
        else:
            formatter.print(estimates)

        if enrich_historical:
            _print_historical_enrichment(formatter, instance_type, region)

    except Exception as e:
        formatter.print_error(f"Failed to check capacity: {e}")
        sys.exit(1)


@capacity.command("recommend")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option(
    "--fault-tolerance",
    "-f",
    type=click.Choice(["high", "medium", "low"]),
    default="medium",
    help="Fault tolerance level",
)
@pass_config
def recommend_capacity(config: Any, instance_type: Any, region: Any, fault_tolerance: Any) -> None:
    """Get capacity type recommendation for a workload."""
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        capacity_type, explanation = checker.recommend_capacity_type(
            instance_type, region, fault_tolerance
        )

        formatter.print_info(f"Recommended: {capacity_type.upper()}")
        formatter.print_info(f"Reason: {explanation}")

    except Exception as e:
        formatter.print_error(f"Failed to get recommendation: {e}")
        sys.exit(1)


@capacity.command("spot-prices")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--days", "-d", default=7, help="Days of history")
@pass_config
def spot_prices(config: Any, instance_type: Any, region: Any, days: Any) -> None:
    """Get spot price history for an instance type."""
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        prices = checker.get_spot_price_history(instance_type, region, days)

        if not prices:
            formatter.print_warning(f"No spot price data for {instance_type} in {region}")
            return

        formatter.print(
            prices,
            columns=[
                "availability_zone",
                "current_price",
                "avg_price_7d",
                "min_price_7d",
                "max_price_7d",
                "price_stability",
            ],
        )

    except Exception as e:
        formatter.print_error(f"Failed to get spot prices: {e}")
        sys.exit(1)


@capacity.command("instance-info")
@click.argument("instance_type")
@pass_config
def instance_info(config: Any, instance_type: Any) -> None:
    """Get information about an instance type."""
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        info = checker.get_instance_info(instance_type)
        if info:
            formatter.print(info)
        else:
            formatter.print_error(f"Instance type {instance_type} not found")
            sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to get instance info: {e}")
        sys.exit(1)


@capacity.command("status")
@click.option("--region", "-r", help="Specific region to check")
@click.option("--all-regions", "-a", is_flag=True, default=True, help="Check all regions (default)")
@pass_config
def capacity_status(config: Any, region: Any, all_regions: Any) -> None:
    """Show comprehensive resource utilization across regions.

    Displays pending/running workloads, GPU/CPU utilization, queue depth,
    and active job counts for one or all GCO clusters.

    Examples:
        gco capacity status
        gco capacity status --region us-east-1
        gco capacity status --all-regions
    """
    from ..capacity import get_multi_region_capacity_checker

    formatter = get_output_formatter(config)

    try:
        checker = get_multi_region_capacity_checker(config)

        if region:
            capacity = checker.get_region_capacity(region)
            formatter.print(capacity)
        else:
            capacities = checker.get_all_regions_capacity()

            if not capacities:
                formatter.print_warning("No GCO stacks found")
                return

            # Format as table
            print("\n  REGION          QUEUE  RUNNING  GPU%   CPU%   SCORE")
            print("  " + "-" * 55)
            for c in sorted(capacities, key=lambda x: x.recommendation_score):
                print(
                    f"  {c.region:<15} {c.queue_depth:>5}  {c.running_jobs:>7}  "
                    f"{c.gpu_utilization:>4.0f}%  {c.cpu_utilization:>4.0f}%  {c.recommendation_score:>5.0f}"
                )

            # Show recommendation
            print()
            best = min(capacities, key=lambda x: x.recommendation_score)
            formatter.print_info(f"Recommended region: {best.region} (lowest score = best)")

    except Exception as e:
        formatter.print_error(f"Failed to get capacity status: {e}")
        sys.exit(1)


@capacity.command("recommend-region")
@click.option("--gpu", is_flag=True, help="Job requires GPUs")
@click.option("--min-gpus", default=0, help="Minimum GPUs required")
@click.option(
    "--instance-type", "-i", default=None, help="Specific instance type for workload-aware scoring"
)
@click.option("--gpu-count", default=0, help="Number of GPUs required")
@pass_config
def recommend_region(
    config: Any, gpu: Any, min_gpus: Any, instance_type: Any, gpu_count: Any
) -> None:
    """Recommend optimal region for job placement.

    Analyzes capacity across all deployed EKS regions and recommends
    the best region. When --instance-type is provided, uses weighted
    multi-signal scoring that factors in spot placement scores, pricing,
    queue depth, GPU utilization, and running job counts.

    Without --instance-type, uses a simpler composite score based on
    queue depth, GPU utilization, and running jobs.

    Examples:
        gco capacity recommend-region
        gco capacity recommend-region --gpu
        gco capacity recommend-region -i g5.xlarge
        gco capacity recommend-region -i p4d.24xlarge --gpu-count 8
    """
    from ..capacity import get_multi_region_capacity_checker

    formatter = get_output_formatter(config)

    try:
        checker = get_multi_region_capacity_checker(config)
        recommendation = checker.recommend_region_for_job(
            gpu_required=gpu,
            min_gpus=min_gpus,
            instance_type=instance_type,
            gpu_count=gpu_count,
        )

        formatter.print_success(f"Recommended region: {recommendation['region']}")
        formatter.print_info(f"Reason: {recommendation['reason']}")

        if config.verbose:
            print("\nAll regions ranked:")
            for r in recommendation.get("all_regions", []):
                print(
                    f"  {r['region']}: score={r['score']:.4f}, "
                    f"queue={r['queue_depth']}, gpu={r['gpu_utilization']:.0f}%"
                )

    except Exception as e:
        formatter.print_error(f"Failed to get recommendation: {e}")
        sys.exit(1)


@capacity.command("ai-recommend")
@click.option("--workload", "-w", help="Description of your workload")
@click.option(
    "--instance-type",
    "-i",
    multiple=True,
    help="Instance types to consider (can specify multiple)",
)
@click.option("--region", "-r", multiple=True, help="Regions to consider (can specify multiple)")
@click.option("--gpu", is_flag=True, help="Workload requires GPUs")
@click.option("--min-gpus", default=0, help="Minimum GPUs required")
@click.option("--min-memory-gb", default=0, help="Minimum memory in GB")
@click.option(
    "--fault-tolerance",
    "-f",
    type=click.Choice(["high", "medium", "low"]),
    default="medium",
    help="Fault tolerance level",
)
@click.option("--max-cost", type=float, help="Maximum cost per hour in USD")
@click.option(
    "--model",
    "-m",
    default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    help="Bedrock model ID to use",
)
@click.option("--raw", is_flag=True, help="Show raw AI response")
@pass_config
def ai_recommend(
    config: Any,
    workload: Any,
    instance_type: Any,
    region: Any,
    gpu: Any,
    min_gpus: Any,
    min_memory_gb: Any,
    fault_tolerance: Any,
    max_cost: Any,
    model: Any,
    raw: Any,
) -> None:
    """Get AI-powered capacity recommendation using Amazon Bedrock.

    This command gathers comprehensive capacity data including:
    - Spot placement scores and pricing across regions
    - On-demand availability and pricing
    - Current cluster utilization (queue depth, GPU/CPU usage)
    - Running and pending job counts

    The data is analyzed by an LLM to provide intelligent recommendations
    for where to place your workload.

    ⚠️  DISCLAIMER: Recommendations are AI-generated and should be validated
    before making production decisions. Capacity availability and pricing
    can change rapidly.

    REQUIREMENTS:
    - AWS credentials with bedrock:InvokeModel permission
    - The specified Bedrock model must be enabled in your account
    - Default model: Claude Sonnet 4.5 (anthropic.claude-sonnet-4-5-20250929-v1:0)

    Examples:
        gco capacity ai-recommend --workload "Training a large language model"

        gco capacity ai-recommend -w "Inference workload" --gpu --min-gpus 4

        gco capacity ai-recommend -i g5.xlarge -i g5.2xlarge -r us-east-1 -r us-west-2

        gco capacity ai-recommend --fault-tolerance high --max-cost 5.00
    """
    from ..capacity import get_bedrock_capacity_advisor

    formatter = get_output_formatter(config)

    # Print disclaimer
    print()
    print("  " + "=" * 70)
    print("  ⚠️  AI-POWERED RECOMMENDATION DISCLAIMER")
    print("  " + "-" * 70)
    print("  This recommendation is generated by an AI model and should be")
    print("  validated before making production decisions.")
    print("  ")
    print("  • Capacity availability can change rapidly")
    print("  • Spot instances may be interrupted at any time")
    print("  • Pricing data may not reflect real-time prices")
    print("  • AI recommendations are not guaranteed to be optimal")
    print("  " + "=" * 70)
    print()

    try:
        formatter.print_info("Gathering capacity data across regions...")

        advisor = get_bedrock_capacity_advisor(config, model_id=model)

        # Build requirements dict
        requirements = {
            "gpu_required": gpu,
            "min_gpus": min_gpus if min_gpus > 0 else None,
            "min_memory_gb": min_memory_gb if min_memory_gb > 0 else None,
            "fault_tolerance": fault_tolerance,
            "max_cost_per_hour": max_cost,
        }
        # Remove None values
        requirements = {k: v for k, v in requirements.items() if v is not None}

        formatter.print_info(f"Analyzing with {model}...")

        recommendation = advisor.get_recommendation(
            workload_description=workload,
            instance_types=list(instance_type) if instance_type else None,
            regions=list(region) if region else None,
            requirements=requirements if requirements else None,
        )

        # Display recommendation
        print()
        print("  " + "=" * 70)
        print("  🤖 AI RECOMMENDATION")
        print("  " + "=" * 70)
        print()
        print(f"  Region:        {recommendation.recommended_region}")
        print(f"  Instance Type: {recommendation.recommended_instance_type}")
        print(f"  Capacity Type: {recommendation.recommended_capacity_type.upper()}")
        print(f"  Confidence:    {recommendation.confidence.upper()}")
        if recommendation.cost_estimate:
            print(f"  Est. Cost:     {recommendation.cost_estimate}")
        print()
        print("  REASONING:")
        print("  " + "-" * 68)
        # Word wrap the reasoning
        reasoning_lines = recommendation.reasoning.split(". ")
        for line in reasoning_lines:
            if line.strip():
                print(f"  {line.strip()}.")
        print()

        # Show alternatives
        if recommendation.alternative_options:
            print("  ALTERNATIVE OPTIONS:")
            print("  " + "-" * 68)
            for i, alt in enumerate(recommendation.alternative_options[:3], 1):
                print(
                    f"  {i}. {alt.get('region', 'N/A')} / "
                    f"{alt.get('instance_type', 'N/A')} / "
                    f"{alt.get('capacity_type', 'N/A').upper()}"
                )
                if alt.get("reason"):
                    print(f"     {alt['reason']}")
            print()

        # Show warnings
        if recommendation.warnings:
            print("  ⚠️  WARNINGS:")
            print("  " + "-" * 68)
            for warning in recommendation.warnings:
                print(f"  • {warning}")
            print()

        # Show raw response if requested
        if raw:
            print("  RAW AI RESPONSE:")
            print("  " + "-" * 68)
            print(recommendation.raw_response)
            print()

        print("  " + "=" * 70)
        print()

    except Exception as e:
        formatter.print_error(f"Failed to get AI recommendation: {e}")
        sys.exit(1)


@capacity.command("reservations")
@click.option("--instance-type", "-i", help="Filter by instance type")
@click.option("--region", "-r", help="Specific region (default: all deployed regions)")
@pass_config
def list_reservations(config: Any, instance_type: Any, region: Any) -> None:
    """List On-Demand Capacity Reservations (ODCRs) across regions.

    Shows all active capacity reservations with utilization details.

    Examples:
        gco capacity reservations
        gco capacity reservations -i p5.48xlarge
        gco capacity reservations -r us-east-1
    """
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        if region:
            reservations = checker.list_capacity_reservations(region, instance_type=instance_type)
            result = {
                "regions_checked": [region],
                "total_reservations": len(reservations),
                "total_reserved_instances": sum(r["total_instances"] for r in reservations),
                "total_available_instances": sum(r["available_instances"] for r in reservations),
                "reservations": reservations,
            }
        else:
            result = checker.list_all_reservations(instance_type=instance_type)

        if config.output_format != "table":
            formatter.print(result)
            return

        reservations = result["reservations"]
        if not reservations:
            formatter.print_info("No active capacity reservations found")
            return

        print(f"\n  Capacity Reservations ({len(reservations)} found)")
        print("  " + "-" * 90)
        print(
            f"  {'INSTANCE TYPE':<18} {'REGION':<15} {'AZ':<18} "
            f"{'TOTAL':>5} {'AVAIL':>5} {'USED%':>6} {'MATCH CRITERIA'}"
        )
        print("  " + "-" * 90)
        for r in reservations:
            print(
                f"  {r['instance_type']:<18} {r['region']:<15} "
                f"{r['availability_zone']:<18} {r['total_instances']:>5} "
                f"{r['available_instances']:>5} {r['utilization_pct']:>5.1f}% "
                f"{r.get('instance_match_criteria', 'open')}"
            )

        print()
        print(
            f"  Total: {result['total_reserved_instances']} reserved, "
            f"{result['total_available_instances']} available"
        )
        print()

    except Exception as e:
        formatter.print_error(f"Failed to list reservations: {e}")
        sys.exit(1)


@capacity.command("reservation-check")
@click.option("--instance-type", "-i", required=True, help="Instance type to check")
@click.option("--region", "-r", help="Specific region (default: all deployed regions)")
@click.option("--count", "-c", default=1, help="Minimum instances needed")
@click.option(
    "--include-blocks/--no-blocks",
    default=True,
    help="Include Capacity Block offerings (default: yes)",
)
@click.option(
    "--block-duration",
    default=24,
    type=int,
    help="Capacity Block duration in hours (default: 24)",
)
@pass_config
def reservation_check(
    config: Any,
    instance_type: Any,
    region: Any,
    count: Any,
    include_blocks: Any,
    block_duration: Any,
) -> None:
    """Check reservation availability and Capacity Block offerings.

    Checks both existing ODCRs and purchasable Capacity Blocks for ML
    workloads. Capacity Blocks provide guaranteed GPU capacity for a
    fixed duration at a known price.

    Examples:
        gco capacity reservation-check -i p5.48xlarge
        gco capacity reservation-check -i p4d.24xlarge -c 2 --block-duration 48
        gco capacity reservation-check -i g5.48xlarge -r us-east-1 --no-blocks
    """
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        formatter.print_info(
            f"Checking reservations for {instance_type} "
            f"(min {count} instance{'s' if count > 1 else ''})..."
        )

        result = checker.check_reservation_availability(
            instance_type=instance_type,
            region=region,
            min_count=count,
            include_capacity_blocks=include_blocks,
            block_duration_hours=block_duration,
        )

        if config.output_format != "table":
            formatter.print(result)
            return

        # ODCR section
        odcr = result["odcr"]
        print(f"\n  On-Demand Capacity Reservations for {instance_type}")
        print("  " + "-" * 60)
        if odcr["reservations"]:
            for r in odcr["reservations"]:
                print(
                    f"  ✓ {r['availability_zone']}: "
                    f"{r['available_instances']}/{r['total_instances']} available "
                    f"({r['reservation_id']})"
                )
            print(
                f"\n  Total: {odcr['total_available_instances']} available "
                f"of {odcr['total_reserved_instances']} reserved"
            )
        else:
            print("  No active ODCRs found for this instance type")

        # Capacity Blocks section
        if include_blocks:
            blocks = result["capacity_blocks"]
            print(f"\n  Capacity Block Offerings ({block_duration}h)")
            print("  " + "-" * 60)
            if blocks["offerings"]:
                for b in blocks["offerings"]:
                    print(
                        f"  ✓ {b['availability_zone']}: "
                        f"{b['instance_count']}x {b['duration_hours']}h "
                        f"starting {b['start_date'][:16]} — ${b['upfront_fee']}"
                    )
            else:
                print("  No Capacity Block offerings available")

        # Recommendation
        print()
        print(f"  💡 {result['recommendation']}")
        print()

    except Exception as e:
        formatter.print_error(f"Failed to check reservations: {e}")
        sys.exit(1)


@capacity.command("reserve")
@click.option(
    "--offering-id",
    "-o",
    required=True,
    help="Capacity Block offering ID (cb-xxx) from reservation-check",
)
@click.option("--region", "-r", required=True, help="AWS region where the offering exists")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate the offering without purchasing (no cost incurred)",
)
@pass_config
def reserve_capacity(config: Any, offering_id: Any, region: Any, dry_run: Any) -> None:
    """Purchase a Capacity Block offering by its ID.

    Use 'gco capacity reservation-check' first to find available offerings
    and their IDs, then purchase with this command.

    ⚠️  WARNING: This command purchases capacity and incurs charges.
    Use --dry-run to validate first.

    Examples:
        # First, find offerings:
        gco capacity reservation-check -i p4d.24xlarge -r us-east-1

        # Validate without purchasing:
        gco capacity reserve -o cb-0123456789abcdef0 -r us-east-1 --dry-run

        # Purchase:
        gco capacity reserve -o cb-0123456789abcdef0 -r us-east-1
    """
    formatter = get_output_formatter(config)
    checker = get_capacity_checker(config)

    try:
        if dry_run:
            formatter.print_info(f"Dry run: validating offering {offering_id} in {region}...")
        else:
            formatter.print_info(f"Purchasing Capacity Block {offering_id} in {region}...")

        result = checker.purchase_capacity_block(
            offering_id=offering_id,
            region=region,
            dry_run=dry_run,
        )

        if config.output_format != "table":
            formatter.print(result)
            return

        if result["success"]:
            if dry_run:
                print()
                print(f"  ✓ Dry run passed — offering {offering_id} is valid and purchasable")
                print(f"  Region: {region}")
                print()
                print("  To purchase, run without --dry-run:")
                print(f"    gco capacity reserve -o {offering_id} -r {region}")
                print()
            else:
                print()
                print("  ✓ Capacity Block purchased successfully")
                print(f"  Reservation ID: {result['reservation_id']}")
                print(f"  Instance Type:  {result['instance_type']}")
                print(f"  AZ:             {result['availability_zone']}")
                print(f"  Instances:      {result['total_instances']}")
                print(f"  Start:          {result.get('start_date', 'N/A')}")
                print(f"  End:            {result.get('end_date', 'N/A')}")
                print()
                print("  To create a NodePool for this reservation:")
                print(
                    f"    gco nodepools create-odcr -n my-pool -r {region} "
                    f"-c {result['reservation_id']} -i {result['instance_type']}"
                )
                print()
        else:
            formatter.print_error(
                f"Failed: {result.get('error_code', 'Unknown')}: {result.get('error', '')}"
            )
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to reserve capacity: {e}")
        sys.exit(1)


_HISTORY_DISABLED_HINT = (
    "The historical capacity surface is not enabled. It is an optional add-on to "
    "the global stack: set historical.enabled to true in cdk.json and run "
    "'gco stacks deploy gco-global'. See lambda/capacity-poller/README.md."
)


def _history_disabled(exc: Exception) -> bool:
    """True if exc is a 'table does not exist' error (feature not deployed)."""
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException"
    )


def _print_historical_enrichment(formatter: Any, instance_type: str, region: str) -> None:
    """Append a historical capacity summary to ``gco capacity check`` output."""
    from ..capacity.history import get_capacity_history_store

    try:
        stats = get_capacity_history_store().get_statistics(instance_type, region)
    except Exception as e:  # supplementary to check; never fail the command
        if _history_disabled(e):
            formatter.print_warning(_HISTORY_DISABLED_HINT)
        else:
            formatter.print_warning(f"Historical enrichment unavailable: {e}")
        return
    if stats["sample_count"] == 0:
        formatter.print_warning(
            f"No historical samples for {instance_type} in {region} yet "
            "(the poller records one about every 15 minutes)."
        )
        return
    formatter.print_info(f"Historical context (last 7 days, {stats['sample_count']} samples):")
    spot_stats = stats["metrics"].get("spot_score")
    if spot_stats:
        print(
            f"  spot_score p25/p50/p75: "
            f"{spot_stats['p25']}/{spot_stats['p50']}/{spot_stats['p75']} "
            f"(min {spot_stats['min']}, max {spot_stats['max']})"
        )
    price_stats = stats["metrics"].get("spot_price")
    if price_stats:
        print(
            f"  spot_price p25/p50/p75: "
            f"{price_stats['p25']}/{price_stats['p50']}/{price_stats['p75']}"
        )


def _format_patterns_grid(patterns: dict[str, Any]) -> str:
    """Render a day-of-week x hour heatmap of average scores."""
    from ..capacity.history import DAY_NAMES

    grid = patterns.get("patterns", {})
    metric = patterns.get("metric", "spot_score")
    lines = [f"Average {metric} by day-of-week and hour (UTC)"]
    header = "Day".ljust(10) + "".join(f"{hour:>5}" for hour in range(24))
    lines.append(header)
    lines.append("-" * len(header))
    for day in DAY_NAMES:
        hours = grid.get(day, {})
        row = day[:9].ljust(10)
        for hour in range(24):
            cell = hours.get(hour) or hours.get(str(hour))
            row += f"{cell['avg']:>5.1f}" if cell else f"{'.':>5}"
        lines.append(row)
    best = patterns.get("best_windows", [])[:3]
    if best:
        lines.append("")
        lines.append("Best windows:")
        for window in best:
            lines.append(
                f"- {window['day']} {window['hour']:02d}:00 UTC "
                f"avg={window['avg']} (n={window['count']})"
            )
    return "\n".join(lines)


@capacity.group("history")
def history() -> None:
    """Query the historical capacity surface (requires historical.enabled)."""


@history.command("show")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--hours", "-H", default=168, help="Hours of history (default 168 = 7 days)")
@pass_config
def history_show(config: Any, instance_type: Any, region: Any, hours: Any) -> None:
    """Show the capacity time-series for an instance type in a region."""
    from ..capacity.history import get_capacity_history_store

    formatter = get_output_formatter(config)
    try:
        trend = get_capacity_history_store().get_trend(instance_type, region, hours)
        if not trend:
            formatter.print_warning(
                f"No historical samples for {instance_type} in {region} in the last {hours}h yet (the poller records one about every 15 minutes)."
            )
            return
        formatter.print(
            trend,
            columns=[
                "timestamp",
                "spot_score",
                "spot_price",
                "az_count",
                "queue_depth",
                "capacity_blocks_available",
                "capacity_blocks_total",
            ],
        )
    except Exception as e:
        if _history_disabled(e):
            formatter.print_warning(_HISTORY_DISABLED_HINT)
            return
        formatter.print_error(f"Failed to load capacity history: {e}")
        sys.exit(1)


@history.command("stats")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--hours", "-H", default=168, help="Hours of history (default 168 = 7 days)")
@pass_config
def history_stats(config: Any, instance_type: Any, region: Any, hours: Any) -> None:
    """Show a statistical summary (p25/p50/p75/min/max/stddev) per metric."""
    from ..capacity.history import get_capacity_history_store

    formatter = get_output_formatter(config)
    try:
        stats = get_capacity_history_store().get_statistics(instance_type, region, hours)
        if stats["sample_count"] == 0:
            formatter.print_warning(
                f"No historical samples for {instance_type} in {region} in the last {hours}h yet (the poller records one about every 15 minutes)."
            )
            return
        if config.output_format == "table":
            rows = [{"metric": name, **values} for name, values in stats["metrics"].items()]
            print(
                formatter.format(
                    rows,
                    columns=[
                        "metric",
                        "count",
                        "min",
                        "p25",
                        "p50",
                        "p75",
                        "max",
                        "mean",
                        "stddev",
                    ],
                )
            )
        else:
            formatter.print(stats)
    except Exception as e:
        if _history_disabled(e):
            formatter.print_warning(_HISTORY_DISABLED_HINT)
            return
        formatter.print_error(f"Failed to compute capacity statistics: {e}")
        sys.exit(1)


@history.command("patterns")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--hours", "-H", default=168, help="Hours of history (default 168 = 7 days)")
@pass_config
def history_patterns(config: Any, instance_type: Any, region: Any, hours: Any) -> None:
    """Show a day/hour heatmap grid of average spot scores."""
    from ..capacity.history import get_capacity_history_store

    formatter = get_output_formatter(config)
    try:
        patterns = get_capacity_history_store().get_temporal_patterns(instance_type, region, hours)
        if not patterns["patterns"]:
            formatter.print_warning(
                f"No historical samples for {instance_type} in {region} in the last {hours}h yet (the poller records one about every 15 minutes)."
            )
            return
        if config.output_format == "table":
            print(_format_patterns_grid(patterns))
        else:
            formatter.print(patterns)
    except Exception as e:
        if _history_disabled(e):
            formatter.print_warning(_HISTORY_DISABLED_HINT)
            return
        formatter.print_error(f"Failed to compute capacity patterns: {e}")
        sys.exit(1)


@capacity.command("predict")
@click.option("--instance-type", "-i", required=True, help="EC2 instance type")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option(
    "--hours", "-H", default=168, help="Hours of history to analyze (default 168 = 7 days)"
)
@click.option(
    "--model",
    "-m",
    default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    help="Bedrock model ID to use",
)
@click.option("--raw", is_flag=True, help="Show the raw AI response")
@pass_config
def predict_capacity(
    config: Any,
    instance_type: Any,
    region: Any,
    hours: Any,
    model: Any,
    raw: Any,
) -> None:
    """Predict the best time to acquire capacity from historical patterns (Bedrock).

    Combines the historical capacity surface (an optional add-on to the global
    stack) with Amazon Bedrock to recommend the day/hour windows with the best
    spot availability and pricing. Requires historical.enabled and collected
    samples.
    """
    from ..capacity import get_bedrock_capacity_advisor

    formatter = get_output_formatter(config)
    try:
        advisor = get_bedrock_capacity_advisor(config, model_id=model)
        prediction = advisor.predict_capacity_window(instance_type, region, hours_back=hours)
    except ValueError as e:
        formatter.print_warning(str(e))
        return
    except Exception as e:
        if _history_disabled(e):
            formatter.print_warning(_HISTORY_DISABLED_HINT)
            return
        formatter.print_error(f"Failed to predict capacity window: {e}")
        sys.exit(1)

    if config.output_format != "table":
        formatter.print(
            {
                "instance_type": prediction.instance_type,
                "region": prediction.region,
                "confidence": prediction.confidence,
                "best_windows": prediction.best_windows,
                "avoid_windows": prediction.avoid_windows,
                "reasoning": prediction.reasoning,
            }
        )
        return

    print()
    print(
        f"  Best time to acquire {instance_type} in {region} "
        f"(confidence: {prediction.confidence.upper()})"
    )
    print("  " + "-" * 68)
    if prediction.best_windows:
        for window in prediction.best_windows[:5]:
            print(
                f"  + {window.get('day', '?')} {window.get('hour_range', '?')}: "
                f"{window.get('why', '')}"
            )
    else:
        print("  (no clear best window identified)")
    if prediction.avoid_windows:
        print()
        print("  Windows to avoid:")
        for window in prediction.avoid_windows[:5]:
            print(
                f"  - {window.get('day', '?')} {window.get('hour_range', '?')}: "
                f"{window.get('why', '')}"
            )
    if prediction.reasoning:
        print()
        print("  Reasoning:")
        for line in prediction.reasoning.split(". "):
            if line.strip():
                print(f"    {line.strip()}")
    if raw:
        print()
        print(prediction.raw_response)
