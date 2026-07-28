"""Cost tracking MCP tools."""

import cli_runner
from audit import audit_logged
from server import mcp


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_summary(days: int = 30) -> str:
    """Get total GCO spend broken down by AWS service.

    Args:
        days: Number of days to look back.
    """
    return cli_runner._run_cli("costs", "summary", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_by_region(days: int = 30) -> str:
    """Get cost breakdown by AWS region.

    Args:
        days: Number of days to look back.
    """
    return cli_runner._run_cli("costs", "regions", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_trend(days: int = 14) -> str:
    """Get daily cost trend.

    Args:
        days: Number of days to show.
    """
    return cli_runner._run_cli("costs", "trend", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_workloads(region: str | None = None) -> str:
    """Estimate accumulated and hourly cost for running workloads.

    Args:
        region: Region to inspect, or omit to inspect every deployment region.
    """
    args = ["costs", "workloads"]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_forecast(days_ahead: int = 30) -> str:
    """Forecast GCO costs for the next N days.

    Args:
        days_ahead: Days to forecast ahead.
    """
    return cli_runner._run_cli("costs", "forecast", "--days", str(days_ahead))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_allocation_status(extra_tags: list[str] | None = None) -> str:
    """Show cost allocation tag activation status for GCO's billing keys.

    Reports whether the Project tag (which every cost query filters on) and
    the AWS-generated aws:eks:cluster-name tag (per-cluster attribution for
    EKS Auto Mode compute) are Active, Inactive, or not yet discovered by
    Billing, plus backfill history and split-cost-allocation-data guidance.

    Args:
        extra_tags: Additional tag keys to check.
    """
    args = ["costs", "allocation", "status"]
    for tag in extra_tags or []:
        args += ["-t", tag]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"low-risk", "costs"})
@audit_logged
def cost_allocation_activate(
    extra_tags: list[str] | None = None,
    backfill_from: str | None = None,
) -> str:
    """Activate GCO's cost allocation tag keys in the billing account.

    Activates the Project and aws:eks:cluster-name tag keys (plus any
    extras) so Cost Explorer can attribute spend to them. Reversible in
    the Billing console; in an AWS Organization this requires the
    management (payer) account. Only affects billing data from activation
    onward unless a backfill date is given.

    Args:
        extra_tags: Additional tag keys to activate.
        backfill_from: Optionally re-tag historical usage from this date
            (YYYY-MM-DD, up to 12 months back).
    """
    args = ["costs", "allocation", "activate", "-y"]
    for tag in extra_tags or []:
        args += ["-t", tag]
    if backfill_from:
        args += ["--backfill-from", backfill_from]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_k8s_namespaces(days: int = 7, region: str | None = None) -> str:
    """Show Kubernetes cost by namespace (Athena over OpenCost reports).

    Args:
        days: Days to look back.
        region: Restrict to one deployment region, or omit for all.
    """
    args = ["costs", "k8s", "namespaces", "--days", str(days)]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_k8s_regions(days: int = 7) -> str:
    """Show Kubernetes allocation cost by deployment region.

    Args:
        days: Days to look back.
    """
    return cli_runner._run_cli("costs", "k8s", "regions", "--days", str(days))


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_k8s_trend(
    days: int = 14,
    granularity: str = "daily",
    namespace: str | None = None,
) -> str:
    """Show Kubernetes cost over time.

    Args:
        days: Days to look back.
        granularity: Trend bucket size ("daily" or "hourly").
        namespace: Restrict to one namespace, or omit for all.
    """
    args = ["costs", "k8s", "trend", "--days", str(days), "--granularity", granularity]
    if namespace:
        args += ["-n", namespace]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_k8s_top(limit: int = 10, by: str = "namespace", days: int = 7) -> str:
    """Show the top-N Kubernetes spenders by namespace, region, or cluster.

    Args:
        limit: Number of results.
        by: Grouping dimension ("namespace", "region", or "cluster").
        days: Days to look back.
    """
    return cli_runner._run_cli(
        "costs", "k8s", "top", "-n", str(limit), "--by", by, "--days", str(days)
    )


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_report_status(region: str | None = None) -> str:
    """Show cost monitoring health, including OpenCost status.

    Args:
        region: Region whose cost monitor to check, or omit for the
            nearest healthy region via the global API.
    """
    args = ["costs", "report", "status"]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "costs"})
@audit_logged
def cost_report_list(region: str | None = None, adhoc: bool = False, limit: int = 20) -> str:
    """List recent OpenCost allocation report objects in S3.

    Args:
        region: Region whose reports to list.
        adhoc: List ad-hoc instead of scheduled reports.
        limit: Maximum results.
    """
    args = ["costs", "report", "list", "-l", str(limit)]
    if region:
        args += ["-r", region]
    if adhoc:
        args += ["--adhoc"]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"low-risk", "costs"})
@audit_logged
def cost_report_generate(region: str | None = None, window_hours: int = 24) -> str:
    """Generate an ad-hoc OpenCost allocation report now (written to S3).

    Args:
        region: Region whose cost monitor generates the report.
        window_hours: Trailing window the report covers (1-168).
    """
    args = ["costs", "report", "generate", "--window-hours", str(window_hours)]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)
