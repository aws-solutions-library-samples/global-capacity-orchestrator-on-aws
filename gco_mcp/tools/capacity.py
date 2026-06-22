"""Capacity checking and recommendation MCP tools."""

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_CAPACITY_PURCHASE, is_enabled
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
    model: str = "anthropic.claude-sonnet-4-5-20250929-v1:0",
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
        model: Bedrock model ID to use for analysis.
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
    if model != "anthropic.claude-sonnet-4-5-20250929-v1:0":
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
    region: str | None = None,
    count: int = 1,
    include_blocks: bool = True,
    block_duration: int = 24,
) -> str:
    """Check reservation availability and Capacity Block offerings.

    Checks both existing ODCRs and purchasable Capacity Blocks for ML
    workloads. Capacity Blocks provide guaranteed GPU capacity for a
    fixed duration at a known price.

    Args:
        instance_type: GPU instance type (e.g. p4d.24xlarge, p5.48xlarge).
        region: Specific region to check (omit for all deployed regions).
        count: Minimum number of instances needed.
        include_blocks: Whether to include Capacity Block offerings.
        block_duration: Capacity Block duration in hours.
    """
    args = ["capacity", "reservation-check", "-i", instance_type, "-c", str(count)]
    if region:
        args += ["-r", region]
    if not include_blocks:
        args.append("--no-blocks")
    if block_duration != 24:
        args += ["--block-duration", str(block_duration)]
    return cli_runner._run_cli(*args)


# Capacity Block purchasing — disabled by default.
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
