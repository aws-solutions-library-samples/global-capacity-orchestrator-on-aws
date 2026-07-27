"""Cost tracking commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig, _load_cdk_json
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _get_deployment_regions(config: GCOConfig) -> list[str]:
    """Get the list of regional deployment regions from cdk.json or fallback to default."""
    cdk_regions = _load_cdk_json()
    if cdk_regions and "regional" in cdk_regions:
        regional = cdk_regions["regional"]
        if isinstance(regional, list) and all(isinstance(r, str) for r in regional):
            return regional
    return [config.default_region]


@click.group()
@pass_config
def costs(config: Any) -> None:
    """View cost breakdowns and estimates for GCO resources."""
    pass


def _print_query_result(config: Any, result: Any, title: str) -> None:
    """Render an Athena QueryResult as a table or structured output."""
    formatter = get_output_formatter(config)
    if config.output_format != "table":
        formatter.print({"columns": result.columns, "rows": result.rows})
        return
    if not result.rows:
        formatter.print_info("No cost data found for the requested window")
        formatter.print_info(
            "Scheduled reports accrue once cost monitoring is deployed; see docs/COST_MONITORING.md"
        )
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column) or "")) for row in result.rows))
        for column in result.columns
    }
    print(f"\n  {title}")
    header = "  " + "  ".join(column.upper().ljust(widths[column]) for column in result.columns)
    print("  " + "-" * (len(header) - 2))
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in result.rows:
        print(
            "  "
            + "  ".join(
                str(row.get(column) or "").ljust(widths[column]) for column in result.columns
            )
        )
    print()


@costs.command("summary")
@click.option(
    "--days", "-d", default=30, type=int, help="Number of days to look back (default: 30)"
)
@click.option(
    "--all", "show_all", is_flag=True, help="Show all account costs (not filtered by GCO tag)"
)
@pass_config
def costs_summary(config: Any, days: Any, show_all: Any) -> None:
    """Show total GCO spend by service.

    Examples:
        gco costs summary
        gco costs summary --days 7
        gco costs summary --all    # All account costs (useful before tags propagate)
    """
    from ..costs import get_cost_tracker

    formatter = get_output_formatter(config)

    try:
        tracker = get_cost_tracker(config)
        summary = tracker.get_cost_summary(days=days, unfiltered=show_all)
        label = "Account" if show_all else "GCO"

        if config.output_format != "table":
            formatter.print(
                {
                    "total": summary.total,
                    "currency": summary.currency,
                    "period_start": summary.period_start,
                    "period_end": summary.period_end,
                    "by_service": [
                        {"service": s.service, "amount": s.amount} for s in summary.by_service
                    ],
                }
            )
            return

        print(f"\n  {label} Cost Summary ({summary.period_start} to {summary.period_end})")
        print("  " + "-" * 75)
        print(f"  {'SERVICE':<50} {'COST':>12}")
        print("  " + "-" * 75)

        for svc in summary.by_service:
            print(f"  {svc.service:<50} ${svc.amount:>10.2f}")

        print("  " + "-" * 75)
        print(f"  {'TOTAL':<50} ${summary.total:>10.2f}")
        print()

    except Exception as e:
        formatter.print_error(f"Failed to get cost summary: {e}")
        sys.exit(1)


@costs.command("regions")
@click.option(
    "--days", "-d", default=30, type=int, help="Number of days to look back (default: 30)"
)
@pass_config
def costs_regions(config: Any, days: Any) -> None:
    """Show cost breakdown by region.

    Examples:
        gco costs regions
        gco costs regions --days 7
    """
    from ..costs import get_cost_tracker

    formatter = get_output_formatter(config)

    try:
        tracker = get_cost_tracker(config)
        by_region = tracker.get_cost_by_region(days=days)

        if config.output_format != "table":
            formatter.print(by_region)
            return

        total = sum(by_region.values())
        print(f"\n  GCO Cost by Region (last {days} days)")
        print("  " + "-" * 50)
        print(f"  {'REGION':<30} {'COST':>12}")
        print("  " + "-" * 50)

        for region, amount in by_region.items():
            pct = (amount / total * 100) if total > 0 else 0
            print(f"  {region:<30} ${amount:>10.2f}  ({pct:.0f}%)")

        print("  " + "-" * 50)
        print(f"  {'TOTAL':<30} ${total:>10.2f}")
        print()

    except Exception as e:
        formatter.print_error(f"Failed to get regional costs: {e}")
        sys.exit(1)


@costs.command("trend")
@click.option("--days", "-d", default=14, type=int, help="Number of days (default: 14)")
@click.option(
    "--all", "show_all", is_flag=True, help="Show all account costs (not filtered by GCO tag)"
)
@pass_config
def costs_trend(config: Any, days: Any, show_all: Any) -> None:
    """Show daily cost trend.

    Examples:
        gco costs trend
        gco costs trend --days 7
        gco costs trend --all
    """
    from ..costs import get_cost_tracker

    formatter = get_output_formatter(config)

    try:
        tracker = get_cost_tracker(config)
        trend = tracker.get_daily_trend(days=days, unfiltered=show_all)
        label = "Account" if show_all else "GCO"

        if config.output_format != "table":
            formatter.print(trend)
            return

        print(f"\n  Daily Cost Trend — {label} (last {days} days)")
        print("  " + "-" * 45)
        print(f"  {'DATE':<15} {'COST':>10}  {'CHART'}")
        print("  " + "-" * 45)

        max_amount = max((d["amount"] for d in trend), default=1) or 1
        for day in trend:
            bar_len = int(day["amount"] / max_amount * 25)
            bar = "█" * bar_len
            print(f"  {day['date']:<15} ${day['amount']:>8.2f}  {bar}")

        total = sum(d["amount"] for d in trend)
        avg = total / len(trend) if trend else 0
        print("  " + "-" * 45)
        print(f"  Total: ${total:.2f}  |  Avg/day: ${avg:.2f}")
        print()

    except Exception as e:
        formatter.print_error(f"Failed to get cost trend: {e}")
        sys.exit(1)


@costs.command("workloads")
@click.option("--region", "-r", help="Region to check (default: all deployment regions)")
@pass_config
def costs_workloads(config: Any, region: Any) -> None:
    """Estimate costs for running workloads (jobs and inference endpoints).

    Examples:
        gco costs workloads
        gco costs workloads -r us-east-1
    """
    from ..costs import get_cost_tracker

    formatter = get_output_formatter(config)

    try:
        tracker = get_cost_tracker(config)

        regions = [region] if region else _get_deployment_regions(config)
        all_workloads = []

        for r in regions:
            workloads = tracker.estimate_running_workloads(r)
            all_workloads.extend(workloads)

        if config.output_format != "table":
            formatter.print(
                [
                    {
                        "name": w.name,
                        "type": w.workload_type,
                        "instance_type": w.instance_type,
                        "gpu_count": w.gpu_count,
                        "hourly_rate": w.hourly_rate,
                        "runtime_hours": w.runtime_hours,
                        "estimated_cost": w.estimated_cost,
                        "region": w.region,
                    }
                    for w in all_workloads
                ]
            )
            return

        if not all_workloads:
            formatter.print_info("No running workloads found")
            return

        print(f"\n  Running Workload Costs ({len(all_workloads)} workloads)")
        print("  " + "-" * 95)
        print(
            f"  {'NAME':<30} {'TYPE':<10} {'INSTANCE':<15} {'GPU':>3} {'$/HR':>8} {'HOURS':>7} {'COST':>10}"
        )
        print("  " + "-" * 95)

        total = 0.0
        for w in sorted(all_workloads, key=lambda x: x.estimated_cost, reverse=True):
            name = w.name[:29]
            print(
                f"  {name:<30} {w.workload_type:<10} {w.instance_type:<15} "
                f"{w.gpu_count:>3} ${w.hourly_rate:>7.3f} {w.runtime_hours:>7.1f} ${w.estimated_cost:>9.4f}"
            )
            total += w.estimated_cost

        total_hourly = sum(w.hourly_rate for w in all_workloads)
        print("  " + "-" * 95)
        print(
            f"  {'TOTAL':<30} {'':10} {'':15} {'':>3} ${total_hourly:>7.3f} {'':>7} ${total:>9.4f}"
        )
        print()

    except Exception as e:
        formatter.print_error(f"Failed to estimate workload costs: {e}")
        sys.exit(1)


@costs.command("forecast")
@click.option("--days", "-d", default=30, type=int, help="Days to forecast (default: 30)")
@pass_config
def costs_forecast(config: Any, days: Any) -> None:
    """Forecast GCO costs for the next N days.

    Examples:
        gco costs forecast
        gco costs forecast --days 60
    """
    from ..costs import get_cost_tracker

    formatter = get_output_formatter(config)

    try:
        tracker = get_cost_tracker(config)
        forecast = tracker.get_forecast(days_ahead=days)

        if "error" in forecast:
            formatter.print_error(f"Forecast unavailable: {forecast['error']}")
            formatter.print_info("Cost Explorer needs 14+ days of data to generate forecasts")
            return

        if config.output_format != "table":
            formatter.print(forecast)
            return

        total = forecast.get("forecast_total", 0)
        print(f"\n  Cost Forecast ({forecast['period_start']} to {forecast['period_end']})")
        print("  " + "-" * 40)
        print(f"  Projected spend:  ${total:>10.2f}")
        print(f"  Daily average:    ${total / days:>10.2f}")
        print()

    except Exception as e:
        formatter.print_error(f"Failed to get forecast: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# k8s subgroup — Athena queries over the OpenCost allocation reports
# ---------------------------------------------------------------------------


@costs.group("k8s")
@pass_config
def costs_k8s(config: Any) -> None:
    """Query Kubernetes allocation costs across regions (Athena-backed).

    These commands aggregate the Parquet allocation reports the per-region
    cost-monitor services write to the central cost report bucket. Requires
    cost_monitoring.enabled in cdk.json and a deployed monitoring stack.
    """
    pass


@costs_k8s.command("namespaces")
@click.option("--days", "-d", default=7, type=int, help="Days to look back (default: 7)")
@click.option("--region", "-r", help="Restrict to one deployment region")
@pass_config
def costs_k8s_namespaces(config: Any, days: Any, region: Any) -> None:
    """Show Kubernetes cost by namespace across all regions.

    Examples:
        gco costs k8s namespaces
        gco costs k8s namespaces --days 30
        gco costs k8s namespaces -r us-east-1
    """
    from ..cost_analytics import get_cost_analytics

    formatter = get_output_formatter(config)
    try:
        analytics = get_cost_analytics(config)
        result = analytics.cost_by_namespace(days=days, region=region)
        scope = f"region {region}" if region else "all regions"
        _print_query_result(config, result, f"Kubernetes cost by namespace — {scope}, last {days}d")
    except Exception as e:
        formatter.print_error(f"Failed to query namespace costs: {e}")
        sys.exit(1)


@costs_k8s.command("regions")
@click.option("--days", "-d", default=7, type=int, help="Days to look back (default: 7)")
@pass_config
def costs_k8s_regions(config: Any, days: Any) -> None:
    """Show Kubernetes allocation cost by deployment region.

    Examples:
        gco costs k8s regions
        gco costs k8s regions --days 30
    """
    from ..cost_analytics import get_cost_analytics

    formatter = get_output_formatter(config)
    try:
        analytics = get_cost_analytics(config)
        result = analytics.cost_by_region(days=days)
        _print_query_result(config, result, f"Kubernetes cost by region — last {days}d")
    except Exception as e:
        formatter.print_error(f"Failed to query regional costs: {e}")
        sys.exit(1)


@costs_k8s.command("trend")
@click.option("--days", "-d", default=14, type=int, help="Days to look back (default: 14)")
@click.option(
    "--granularity",
    type=click.Choice(["daily", "hourly"]),
    default="daily",
    show_default=True,
    help="Trend bucket size",
)
@click.option("--namespace", "-n", help="Restrict to one namespace")
@pass_config
def costs_k8s_trend(config: Any, days: Any, granularity: Any, namespace: Any) -> None:
    """Show Kubernetes cost over time.

    Examples:
        gco costs k8s trend
        gco costs k8s trend --days 30 --granularity daily
        gco costs k8s trend -n gco-jobs --granularity hourly --days 2
    """
    from ..cost_analytics import get_cost_analytics

    formatter = get_output_formatter(config)
    try:
        analytics = get_cost_analytics(config)
        result = analytics.cost_over_time(days=days, granularity=granularity, namespace=namespace)
        scope = f"namespace {namespace}" if namespace else "all namespaces"
        _print_query_result(config, result, f"Kubernetes cost trend — {scope}, last {days}d")
    except Exception as e:
        formatter.print_error(f"Failed to query cost trend: {e}")
        sys.exit(1)


@costs_k8s.command("top")
@click.option(
    "--limit", "-n", "top_n", default=10, type=int, help="Number of results (default: 10)"
)
@click.option(
    "--by",
    type=click.Choice(["namespace", "region", "cluster"]),
    default="namespace",
    show_default=True,
    help="Grouping dimension",
)
@click.option("--days", "-d", default=7, type=int, help="Days to look back (default: 7)")
@pass_config
def costs_k8s_top(config: Any, top_n: Any, by: Any, days: Any) -> None:
    """Show the top-N spenders by namespace, region, or cluster.

    Examples:
        gco costs k8s top
        gco costs k8s top -n 5 --by region
        gco costs k8s top --by cluster --days 30
    """
    from ..cost_analytics import get_cost_analytics

    formatter = get_output_formatter(config)
    try:
        analytics = get_cost_analytics(config)
        result = analytics.top_spenders(n=top_n, by=by, days=days)
        _print_query_result(config, result, f"Top {top_n} spenders by {by} — last {days}d")
    except Exception as e:
        formatter.print_error(f"Failed to query top spenders: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# report subgroup — ad-hoc reports + report listing via the GCO API
# ---------------------------------------------------------------------------


def _cost_api_region(config: Any, region: Any) -> Any:
    """Resolve the transport region for /api/v1/cost/* calls.

    An explicit ``--region`` pins the request to that region's API bridge
    (each region's cost monitor owns its own OpenCost data). Without it the
    request rides the global API and is served by the nearest healthy region;
    the response payload names the region that answered.
    """
    if region:
        return region
    return config.default_region if config.use_regional_api else None


@costs.group("report")
@pass_config
def costs_report(config: Any) -> None:
    """Generate and list OpenCost allocation reports via the GCO API."""
    pass


@costs_report.command("generate")
@click.option("--region", "-r", help="Region whose cost monitor generates the report")
@click.option(
    "--window-hours",
    default=24,
    type=click.IntRange(1, 168),
    show_default=True,
    help="Trailing window the report covers",
)
@click.option("--show-rows", is_flag=True, help="Print the allocation rows in the response")
@pass_config
def costs_report_generate(config: Any, region: Any, window_hours: Any, show_rows: Any) -> None:
    """Generate an ad-hoc cost report now (written under adhoc/ in S3).

    Examples:
        gco costs report generate
        gco costs report generate -r us-east-1 --window-hours 48
        gco costs report generate --show-rows
    """
    from ..aws_client import get_aws_client

    formatter = get_output_formatter(config)
    try:
        aws_client = get_aws_client(config)
        result = aws_client.call_api(
            method="POST",
            path="/api/v1/cost/reports",
            region=_cost_api_region(config, region),
            body={"window_hours": window_hours, "include_rows": bool(show_rows)},
        )
        report = result.get("report", {})
        formatter.print_success(
            f"Report written to s3://{result.get('bucket')}/{report.get('s3_key')}"
        )
        formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to generate cost report: {e}")
        sys.exit(1)


@costs_report.command("list")
@click.option("--region", "-r", help="Region whose reports to list")
@click.option("--adhoc", is_flag=True, help="List ad-hoc instead of scheduled reports")
@click.option("--limit", "-l", default=20, type=click.IntRange(1, 1000), help="Maximum results")
@pass_config
def costs_report_list(config: Any, region: Any, adhoc: Any, limit: Any) -> None:
    """List recent cost report objects in the cost report bucket.

    Examples:
        gco costs report list
        gco costs report list -r us-east-1 --limit 50
        gco costs report list --adhoc
    """
    from ..aws_client import get_aws_client

    formatter = get_output_formatter(config)
    try:
        aws_client = get_aws_client(config)
        result = aws_client.call_api(
            method="GET",
            path="/api/v1/cost/reports",
            region=_cost_api_region(config, region),
            params={"adhoc": str(bool(adhoc)).lower(), "limit": str(limit)},
        )
        if config.output_format != "table":
            formatter.print(result)
            return
        reports = result.get("reports", [])
        if not reports:
            formatter.print_info("No reports found yet")
            return
        print(f"\n  Cost Reports — {result.get('region')} ({result.get('count', 0)} shown)")
        print("  " + "-" * 100)
        print(f"  {'KEY':<75} {'SIZE':>9} {'MODIFIED':<20}")
        print("  " + "-" * 100)
        for report in reports:
            key = str(report.get("key", ""))[:74]
            size = report.get("size_bytes", 0)
            modified = str(report.get("last_modified", ""))[:19]
            print(f"  {key:<75} {size:>9} {modified:<20}")
        print()
    except Exception as e:
        formatter.print_error(f"Failed to list cost reports: {e}")
        sys.exit(1)


@costs_report.command("status")
@click.option("--region", "-r", help="Region whose cost monitor to check")
@pass_config
def costs_report_status(config: Any, region: Any) -> None:
    """Show cost monitoring health, including OpenCost status.

    Examples:
        gco costs report status
        gco costs report status -r us-east-1
    """
    from ..aws_client import get_aws_client

    formatter = get_output_formatter(config)
    try:
        aws_client = get_aws_client(config)
        result = aws_client.call_api(
            method="GET",
            path="/api/v1/cost/status",
            region=_cost_api_region(config, region),
        )
        if config.output_format != "table":
            formatter.print(result)
            return
        print(f"\n  Cost Monitoring Status — {result.get('region')}")
        print("  " + "-" * 55)
        print(f"  OpenCost healthy:        {result.get('opencost_healthy')}")
        print(f"  OpenCost returning data: {result.get('opencost_returning_data')}")
        print(f"  Report bucket:           {result.get('bucket')}")
        print(f"  Report interval:         {result.get('report_interval_minutes')} minutes")
        last = result.get("last_scheduled_report")
        if last:
            print(f"  Last scheduled report:   {last.get('s3_key')}")
            print(f"    rows={last.get('row_count')} total=${last.get('total_cost')}")
        if result.get("last_error"):
            print(f"  Last error:              {result.get('last_error')}")
        print()
    except Exception as e:
        formatter.print_error(f"Failed to get cost monitoring status: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# dashboard — port-forward to the regional cost dashboards
# ---------------------------------------------------------------------------


@costs.command("dashboard")
@click.option(
    "--service",
    type=click.Choice(["grafana", "opencost"]),
    default="grafana",
    show_default=True,
    help="grafana opens the GCO Cost dashboard; opencost opens the native OpenCost UI",
)
@click.option("--region", help="Cluster region (defaults to the first cdk.json regional entry)")
@click.option("--local-port", type=int, help="Local port to bind (defaults per-service)")
@click.option(
    "--via-ssm",
    "via_ssm",
    metavar="INSTANCE_ID|auto",
    help=(
        "Tunnel to the private API endpoint through an SSM-managed instance. "
        "Pass an instance id to use an existing one, or 'auto' to provision a "
        "self-terminating ephemeral bastion and tear it down when the forward stops."
    ),
)
@click.option(
    "--bastion-ttl-minutes",
    type=int,
    default=120,
    show_default=True,
    help="Self-terminate backstop (minutes) for an `--via-ssm auto` bastion",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt when provisioning an `--via-ssm auto` bastion",
)
@pass_config
def costs_dashboard(
    config: Any,
    service: str,
    region: Any,
    local_port: Any,
    via_ssm: Any,
    bastion_ttl_minutes: int,
    assume_yes: bool,
) -> None:
    """Open a regional cost dashboard over the private EKS endpoint.

    Port-forwards to the in-cluster Grafana (GCO Cost dashboard) or the
    native OpenCost UI. Runs in the foreground; press Ctrl-C to stop. On a
    private-endpoint cluster (the default) pass ``--via-ssm <instance-id>``
    or ``--via-ssm auto`` exactly like ``gco monitoring open``.

    Examples:
        gco costs dashboard
        gco costs dashboard --service opencost --region us-east-1
        gco costs dashboard --via-ssm auto -y
    """
    import subprocess

    from ..cluster_tunnel import open_api_server_tunnel, resolve_region
    from ..kubectl_helpers import build_port_forward_command, update_kubeconfig
    from .monitoring_cmd import _MONITORING_NAMESPACE, _SERVICES

    formatter = get_output_formatter(config)
    svc = _SERVICES[service]
    target_region = resolve_region(config, region)
    cluster = f"{config.project_name}-{target_region}"
    bind_port = local_port or svc["default_local_port"]

    try:
        update_kubeconfig(cluster, target_region)
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)

    try:
        with open_api_server_tunnel(
            formatter,
            cluster=cluster,
            region=target_region,
            via_ssm=via_ssm,
            bastion_ttl_minutes=bastion_ttl_minutes,
            assume_yes=assume_yes,
        ) as session:
            cmd = build_port_forward_command(
                _MONITORING_NAMESPACE,
                svc["target"],
                bind_port,
                svc["remote_port"],
                server=session.server,
                tls_server_name=session.tls_server_name,
            )
            if service == "grafana":
                url = f"http://localhost:{bind_port}/d/gco-cost/gco-cost-opencost"
                formatter.print_success(f"GCO Cost dashboard → {url} (Ctrl-C to stop)")
                formatter.print_info(
                    "Log in with the Grafana admin credential from the "
                    "kube-prometheus-stack-grafana Secret (monitoring namespace)."
                )
            else:
                url = f"http://localhost:{bind_port}"
                formatter.print_success(f"OpenCost UI → {url} (Ctrl-C to stop)")
            try:
                subprocess.run(
                    cmd, check=False
                )  # nosemgrep: dangerous-subprocess-use-audit - argv built by build_port_forward_command; list form, no shell=True
            except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
                return
    except (RuntimeError, ValueError) as exc:
        formatter.print_error(str(exc))
        sys.exit(1)
