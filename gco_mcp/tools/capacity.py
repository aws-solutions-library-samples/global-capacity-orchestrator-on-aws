"""Capacity checking and recommendation MCP tools."""

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_CAPACITY_PURCHASE, FLAG_DESTRUCTIVE_OPERATIONS, is_enabled
from server import mcp


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def check_capacity(instance_type: str, region: str) -> str:
    """Check spot and on-demand capacity for a specific instance type.

    Args:
        instance_type: EC2 instance type (e.g. g4dn.xlarge, g5.2xlarge, p4d.24xlarge).
        region: AWS region to check.
    """
    return cli_runner._run_cli("capacity", "check", "-i", instance_type, "-r", region)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def instance_info(instance_type: str) -> str:
    """Get hardware and pricing metadata for an EC2 instance type.

    Args:
        instance_type: EC2 instance type (for example, g5.2xlarge or p5.48xlarge).
    """
    return cli_runner._run_cli("capacity", "instance-info", instance_type)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def recommend_capacity(
    instance_type: str,
    region: str,
    fault_tolerance: str = "medium",
) -> str:
    """Recommend spot or on-demand capacity for a workload.

    Args:
        instance_type: EC2 instance type to evaluate.
        region: AWS region in which the workload will run.
        fault_tolerance: Interruption tolerance: ``high``, ``medium``, or ``low``.
    """
    return cli_runner._run_cli(
        "capacity",
        "recommend",
        "-i",
        instance_type,
        "-r",
        region,
        "-f",
        fault_tolerance,
    )


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def capacity_status(region: str | None = None) -> str:
    """View capacity status across all deployed regions.

    Args:
        region: Specific region, or omit for all regions.
    """
    args = ["capacity", "status"]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def recommend_region(
    gpu: bool = False, instance_type: str | None = None, gpu_count: int = 0
) -> str:
    """Get optimal region recommendation based on capacity.

    Args:
        gpu: Whether the workload requires GPUs.
        instance_type: Specific instance type to check. When provided, uses weighted
            multi-signal scoring (spot placement scores, pricing, queue depth, etc.).
        gpu_count: Number of GPUs required for the workload.
    """
    args = ["capacity", "recommend-region"]
    if gpu:
        args.append("--gpu")
    if instance_type:
        args += ["-i", instance_type]
    if gpu_count:
        args += ["--gpu-count", str(gpu_count)]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def spot_prices(instance_type: str, region: str) -> str:
    """Get current spot prices for an instance type.

    Args:
        instance_type: EC2 instance type.
        region: AWS region.
    """
    return cli_runner._run_cli("capacity", "spot-prices", "-i", instance_type, "-r", region)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def ai_recommend(
    workload: str,
    instance_type: str | None = None,
    region: str | None = None,
    gpu: bool = False,
    min_gpus: int = 0,
    min_memory_gb: int = 0,
    fault_tolerance: str = "low",
    max_cost: float | None = None,
    model: str | None = None,
) -> str:
    """Get AI-powered capacity recommendation using Amazon Bedrock.

    Gathers comprehensive capacity data (spot scores, pricing, cluster
    utilization, queue depth) and sends it to an LLM for analysis.
    Returns a recommended region, instance type, capacity type, and reasoning.

    Requires AWS credentials with bedrock:InvokeModel permission and the
    specified model enabled in your account.

    Args:
        workload: Description of the workload (e.g. "Fine-tuning a 20B parameter LLM").
        instance_type: Specific instance type(s) to consider (e.g. "p4d.24xlarge").
        region: Specific region(s) to consider (e.g. "us-east-1").
        gpu: Whether the workload requires GPUs.
        min_gpus: Minimum number of GPUs required.
        min_memory_gb: Minimum GPU memory in GB.
        fault_tolerance: Tolerance for interruptions ("low", "medium", "high").
        max_cost: Maximum acceptable cost per hour in USD.
        model: Bedrock model ID to use for analysis. Omit to use the
            server default (Amazon Nova Premier, us.amazon.nova-premier-v1:0 — a
            first-party model with no First-Time-Use form); the CLI and
            capacity advisor resolve that default, so it lives in one
            place rather than being duplicated here.
    """
    args = ["capacity", "ai-recommend", "-w", workload]
    if instance_type:
        args += ["-i", instance_type]
    if region:
        args += ["-r", region]
    if gpu:
        args.append("--gpu")
    if min_gpus > 0:
        args += ["--min-gpus", str(min_gpus)]
    if min_memory_gb > 0:
        args += ["--min-memory-gb", str(min_memory_gb)]
    if fault_tolerance != "low":
        args += ["--fault-tolerance", fault_tolerance]
    if max_cost is not None:
        args += ["--max-cost", str(max_cost)]
    if model:
        args += ["--model", model]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def list_reservations(
    instance_type: str | None = None,
    region: str | None = None,
) -> str:
    """List On-Demand Capacity Reservations (ODCRs) across regions.

    Shows all active capacity reservations with utilization details.

    Args:
        instance_type: Filter by instance type (e.g. p5.48xlarge).
        region: Filter by specific region.
    """
    args = ["capacity", "reservations"]
    if instance_type:
        args += ["-i", instance_type]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def reservation_check(
    instance_type: str,
    regions: list[str] | None = None,
    count: int = 1,
    include_blocks: bool = True,
    block_duration: int = 24,
    block_duration_days: int | None = None,
    earliest_start: str | None = None,
    latest_start: str | None = None,
) -> str:
    """Check ODCR and Capacity Block availability for an instance type.

    Checks existing On-Demand Capacity Reservations and purchasable Capacity
    Blocks for ML. By default it searches a 24h block soonest-available, but you
    can widen the search: pass several regions to fan out in parallel, set a
    start-date window (earliest_start / latest_start) to ask for blocks starting
    near a date, and set the block duration in hours or days. For a full
    duration-range sweep that returns one ranked, de-duplicated report, use
    find_capacity_blocks instead.

    Args:
        instance_type: GPU instance type (e.g. p4d.24xlarge, p5.48xlarge, p6-b200).
        regions: Regions to check in parallel (any regions, not just deployed);
            omit to check all deployed regions.
        count: Minimum number of instances needed.
        include_blocks: Whether to include Capacity Block offerings.
        block_duration: Capacity Block duration in hours (default 24).
        block_duration_days: Capacity Block duration in days (overrides hours).
        earliest_start: Earliest block start date (YYYY-MM-DD or ISO datetime).
        latest_start: Latest block start date (YYYY-MM-DD or ISO datetime).
    """
    args = ["capacity", "reservation-check", "-i", instance_type, "-c", str(count)]
    for r in regions or []:
        args += ["-r", r]
    if not include_blocks:
        args.append("--no-blocks")
    if block_duration != 24:
        args += ["--block-duration", str(block_duration)]
    if block_duration_days is not None:
        args += ["--block-duration-days", str(block_duration_days)]
    if earliest_start:
        args += ["--earliest-start", earliest_start]
    if latest_start:
        args += ["--latest-start", latest_start]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def find_capacity_blocks(
    instance_type: str,
    regions: list[str] | None = None,
    count: int = 1,
    duration_days: int | None = None,
    duration_hours: int | None = None,
    min_duration_days: int | None = None,
    max_duration_days: int | None = None,
    min_duration_hours: int | None = None,
    max_duration_hours: int | None = None,
    earliest_start: str | None = None,
    latest_start: str | None = None,
    find_longest: bool = False,
) -> str:
    """Find EC2 Capacity Blocks across regions x durations x a start-date window.

    This is the one-call sweep for "where and when can I get N of this GPU
    instance for D days?". It searches every requested region and every valid
    Capacity Block duration in the range, in parallel, then returns a single
    consolidated, de-duplicated, ranked report (cheapest per-GPU-hour first) with
    per-hour and per-GPU-hour pricing and the longest available block.

    AWS allows durations in 1-day increments up to 14 days, then 7-day increments
    up to 182 days; a duration range is expanded to those discrete values
    automatically. Friendly names are normalized (p6-b200 -> p6-b200.48xlarge,
    p6-b300 -> p6-b300.48xlarge), and UltraServer-only families (the Grace-
    Blackwell GB200/GB300 superchips / P6e-GB UltraServers) are flagged rather
    than silently returning nothing.

    Args:
        instance_type: GPU instance type or alias (e.g. p6-b200, p5.48xlarge).
        regions: Regions to search (any regions; defaults to deployed regions).
        count: Instances per block.
        duration_days / duration_hours: A single target duration.
        min_duration_days / max_duration_days: Duration range bounds in days.
        min_duration_hours / max_duration_hours: Duration range bounds in hours.
        earliest_start: Earliest block start (YYYY-MM-DD or ISO datetime).
        latest_start: Latest block start (YYYY-MM-DD or ISO datetime).
        find_longest: Sweep the duration ladder and surface the longest block.
    """
    args = ["capacity", "find-blocks", "-i", instance_type]
    for r in regions or []:
        args += ["-r", r]
    if count != 1:
        args += ["-c", str(count)]
    if duration_days is not None:
        args += ["--duration-days", str(duration_days)]
    if duration_hours is not None:
        args += ["--duration-hours", str(duration_hours)]
    if min_duration_days is not None:
        args += ["--min-duration-days", str(min_duration_days)]
    if max_duration_days is not None:
        args += ["--max-duration-days", str(max_duration_days)]
    if min_duration_hours is not None:
        args += ["--min-duration-hours", str(min_duration_hours)]
    if max_duration_hours is not None:
        args += ["--max-duration-hours", str(max_duration_hours)]
    if earliest_start:
        args += ["--earliest-start", earliest_start]
    if latest_start:
        args += ["--latest-start", latest_start]
    if find_longest:
        args.append("--find-longest")
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def capacity_history_show(instance_type: str, region: str, hours: int = 168) -> str:
    """Show the recorded capacity time-series for an instance type in a region.

    Requires the historical capacity surface (an optional add-on to the global
    stack, enabled by default). Returns spot score, spot price, AZ coverage,
    queue depth, and capacity-block availability over the window.

    Args:
        instance_type: EC2 instance type (e.g. g5.xlarge, p5.48xlarge).
        region: AWS region.
        hours: Hours of history to show (default 168 = 7 days).
    """
    return cli_runner._run_cli(
        "capacity", "history", "show", "-i", instance_type, "-r", region, "-H", str(hours)
    )


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def capacity_history_stats(instance_type: str, region: str, hours: int = 168) -> str:
    """Show p25/p50/p75/min/max/stddev per capacity metric over a time window.

    Args:
        instance_type: EC2 instance type.
        region: AWS region.
        hours: Hours of history to summarize (default 168 = 7 days).
    """
    return cli_runner._run_cli(
        "capacity", "history", "stats", "-i", instance_type, "-r", region, "-H", str(hours)
    )


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def capacity_history_patterns(instance_type: str, region: str, hours: int = 168) -> str:
    """Show a day-of-week by hour heatmap of average spot placement scores.

    Args:
        instance_type: EC2 instance type.
        region: AWS region.
        hours: Hours of history to analyze (default 168 = 7 days).
    """
    return cli_runner._run_cli(
        "capacity", "history", "patterns", "-i", instance_type, "-r", region, "-H", str(hours)
    )


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def capacity_predict(
    instance_type: str,
    region: str | None = None,
    hours: int = 168,
    all_regions: bool = False,
) -> str:
    """Predict the best time to acquire capacity from historical patterns (Bedrock).

    Uses the historical capacity surface plus Amazon Bedrock to recommend the
    day/hour windows with the best spot availability and pricing.

    Args:
        instance_type: EC2 instance type.
        region: AWS region. Omit when all_regions is true.
        hours: Hours of history to analyze (default 168 = 7 days).
        all_regions: Predict across every region that has data for the type.
    """
    args = ["capacity", "predict", "-i", instance_type, "-H", str(hours)]
    if all_regions:
        args.append("--all-regions")
    elif region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "capacity"})
@audit_logged
def find_capacity_reservations(
    instance_type: str | None = None,
    regions: list[str] | None = None,
    count: int = 1,
    state: str = "active",
    pricing: bool = True,
) -> str:
    """Find existing ODCRs across regions in one parallel, ranked report.

    The ODCR counterpart to find_capacity_blocks: it searches every requested
    region in parallel, normalizes friendly instance-type aliases (p6-b200 ->
    p6-b200.48xlarge), enriches each reservation with On-Demand pricing, and
    ranks them most-available-first (then cheapest per-GPU-hour). Use this to
    answer "where do I already have free reserved capacity?" rather than
    list_reservations' plain per-region aggregation.

    Args:
        instance_type: Instance type or alias to filter by (e.g. p6-b200); omit
            to return every reservation.
        regions: Regions to search in parallel (any regions; defaults to deployed).
        count: Minimum available instances to consider the search satisfied.
        state: Reservation state filter ("active" default; "all" for any state).
        pricing: Enrich reservations with On-Demand pricing.
    """
    args = ["capacity", "find-reservations"]
    if instance_type:
        args += ["-i", instance_type]
    for r in regions or []:
        args += ["-r", r]
    if count != 1:
        args += ["-c", str(count)]
    if state != "active":
        args += ["--state", state]
    if not pricing:
        args.append("--no-pricing")
    return cli_runner._run_cli(*args)


# Capacity purchasing / creation — disabled by default.
# Set GCO_ENABLE_CAPACITY_PURCHASE=true to enable.
if is_enabled(FLAG_CAPACITY_PURCHASE):

    @mcp.tool(tags={"cost-incurring", "capacity"})
    @audit_logged
    def reserve_capacity(
        offering_id: str,
        region: str,
        dry_run: bool = False,
    ) -> str:
        """Purchase a Capacity Block offering by its ID.

        Use reservation_check first to find available offerings and their IDs,
        then purchase with this tool. Use dry_run=True to validate without purchasing.

        Args:
            offering_id: Capacity Block offering ID (cb-xxx) from reservation_check.
            region: AWS region where the offering exists.
            dry_run: If True, validate the offering without purchasing (no cost).
        """
        args = ["capacity", "reserve", "-o", offering_id, "-r", region]
        if dry_run:
            args.append("--dry-run")
        return cli_runner._run_cli(*args)

    @mcp.tool(tags={"cost-incurring", "capacity"})
    @audit_logged
    def create_reservation(
        instance_type: str,
        region: str,
        availability_zone: str,
        count: int = 1,
        platform: str = "Linux/UNIX",
        tenancy: str = "default",
        match_criteria: str = "open",
        end_date: str | None = None,
        ebs_optimized: bool = False,
        dry_run: bool = False,
    ) -> str:
        """Create a new On-Demand Capacity Reservation (ODCR).

        The ODCR counterpart to reserve_capacity. Reserves On-Demand capacity for
        an instance type in a specific AZ. Charges accrue for the reserved
        capacity until it is cancelled — use dry_run=True to validate first.

        Args:
            instance_type: EC2 instance type or alias (e.g. p5.48xlarge, p6-b200).
            region: AWS region.
            availability_zone: Target AZ (e.g. us-east-1a).
            count: Number of instances to reserve.
            platform: Instance platform/OS (default "Linux/UNIX").
            tenancy: "default" or "dedicated".
            match_criteria: "open" or "targeted".
            end_date: Optional end date (YYYY-MM-DD or ISO); omit for unlimited.
            ebs_optimized: Reserve EBS-optimized capacity.
            dry_run: If True, validate without creating (no cost).
        """
        args = [
            "capacity",
            "create-reservation",
            "-i",
            instance_type,
            "-r",
            region,
            "-z",
            availability_zone,
            "-c",
            str(count),
        ]
        if platform != "Linux/UNIX":
            args += ["--platform", platform]
        if tenancy != "default":
            args += ["--tenancy", tenancy]
        if match_criteria != "open":
            args += ["--match-criteria", match_criteria]
        if end_date:
            args += ["--end-date", end_date]
        if ebs_optimized:
            args.append("--ebs-optimized")
        if dry_run:
            args.append("--dry-run")
        return cli_runner._run_cli(*args)


# Capacity reservation cancellation — destructive, disabled by default.
# Set GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true to enable.
if is_enabled(FLAG_DESTRUCTIVE_OPERATIONS):

    @mcp.tool(tags={"destructive", "capacity"})
    @audit_logged
    def cancel_reservation(
        reservation_id: str,
        region: str,
        dry_run: bool = False,
    ) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        Cancel an On-Demand Capacity Reservation, releasing its capacity.

        Stops On-Demand charges for the reserved capacity. Only ODCRs can be
        cancelled; a Capacity Block runs for its fixed term. Instances already
        running against the reservation are not terminated.

        Args:
            reservation_id: Capacity Reservation ID (cr-xxx) to cancel.
            region: AWS region where the reservation exists.
            dry_run: If True, validate the cancellation without cancelling.
        """
        args = ["capacity", "cancel-reservation", "-o", reservation_id, "-r", region, "-y"]
        if dry_run:
            args.append("--dry-run")
        return cli_runner._run_cli(*args)
