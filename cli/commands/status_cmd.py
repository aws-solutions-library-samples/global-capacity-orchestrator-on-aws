"""Fleet-wide status command for GCO."""

from __future__ import annotations

import sys
import time
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter
from ..status import (
    COST_REFRESH_INTERVAL_SECONDS,
    SECTION_CAPACITY,
    SECTION_COSTS,
    SECTION_INFERENCE,
    SECTION_JOBS,
    SECTION_NODEPOOLS,
    SECTION_ORDER,
    SECTION_QUEUE,
    SECTION_REGIONS,
    SECTION_STACKS,
    SEVERITY_ERROR,
    STATUS_EMPTY,
    STATUS_OK,
    WATCH_INTERVAL_FLOOR_SECONDS,
    FleetStatus,
    Section,
    gather_fleet_status,
)

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)

# The summary is a route into the existing per-domain commands; every block
# names its drill-down so the reader never has to guess the next command.
_DRILL_DOWNS = {
    SECTION_REGIONS: "gco stacks regions list",
    SECTION_STACKS: "gco stacks status <name> --region <region>",
    SECTION_QUEUE: "gco queue stats",
    SECTION_JOBS: "gco jobs list --all-regions",
    SECTION_CAPACITY: "gco capacity status",
    SECTION_INFERENCE: "gco inference list",
    SECTION_COSTS: "gco costs summary",
    SECTION_NODEPOOLS: "gco nodepools list",
}


def _render_regions(data: dict[str, Any]) -> list[str]:
    workload = ", ".join(data.get("workload", [])) or "-"
    return [
        f"global: {data.get('global')}  api-gateway: {data.get('api_gateway')}  "
        f"monitoring: {data.get('monitoring')}",
        f"workload: {workload}  (source: {data.get('source')})",
    ]


def _render_stacks(data: dict[str, Any]) -> list[str]:
    lines = []
    entries = list(data.get("expected", []))
    optional = list(data.get("optional", []))
    width = max((len(str(e.get("name"))) for e in entries + optional), default=0)
    for entry, marker in [(e, "") for e in entries] + [(e, "  (optional)") for e in optional]:
        status_text = entry.get("status") or "-"
        lines.append(
            f"{str(entry.get('name')):<{width}}  {status_text:<25}  "
            f"{str(entry.get('health')):<12}  {entry.get('region')}{marker}"
        )
    return lines


def _render_queue(data: dict[str, Any]) -> list[str]:
    lines = []
    by_region = data.get("by_region", {})
    for region, entry in by_region.items():
        dlq = entry.get("dlq")
        dlq_text = "unknown" if dlq is None else str(dlq)
        lines.append(
            f"{region:<15}  available {entry.get('available', 0):<5} "
            f"in-flight {entry.get('in_flight', 0):<5} delayed {entry.get('delayed', 0):<5} "
            f"dlq {dlq_text}"
        )
    totals = data.get("totals", {})
    if by_region and totals:
        lines.append(
            f"{'totals':<15}  available {totals.get('available', 0):<5} "
            f"in-flight {totals.get('in_flight', 0):<5} delayed {totals.get('delayed', 0):<5} "
            f"dlq {totals.get('dlq', 0)}"
        )
    return lines


def _render_jobs(data: dict[str, Any]) -> list[str]:
    totals = data.get("totals", {})
    complete = data.get("complete", True)
    scan = "complete" if complete else f"TRUNCATED after {data.get('records_evaluated')} records"
    lines = [
        f"total {totals.get('total', 0)}  queued {totals.get('queued', 0)}  "
        f"running {totals.get('running', 0)}  (scan {scan})"
    ]
    for region, statuses in data.get("by_region", {}).items():
        counts = "  ".join(f"{key} {value}" for key, value in statuses.items())
        lines.append(f"{region:<15}  {counts}")
    return lines


def _render_capacity(data: dict[str, Any]) -> list[str]:
    lines = []
    for region, entry in data.get("by_region", {}).items():
        signals = ", ".join(entry.get("unavailable_signals", []))
        missing = f"  (unavailable: {signals})" if signals else ""
        lines.append(
            f"{region:<15}  queue {entry.get('queue_depth', 0):<4} "
            f"running {entry.get('running_jobs', 0):<4} "
            f"gpu {entry.get('gpu_utilization', 0.0):<5.1f} "
            f"cpu {entry.get('cpu_utilization', 0.0):<5.1f} "
            f"telemetry {entry.get('telemetry_status')}{missing}"
        )
    return lines


def _render_inference(data: dict[str, Any]) -> list[str]:
    totals = data.get("totals", {})
    summary = ", ".join(f"{state} {count}" for state, count in totals.items()) or "none"
    lines = [f"endpoints: {data.get('count', 0)}  ({summary})"]
    for endpoint in data.get("endpoints", []):
        regions = ",".join(endpoint.get("target_regions") or [])
        lines.append(
            f"{endpoint.get('endpoint_name')}  {endpoint.get('desired_state')}  "
            f"regions [{regions}]  namespace {endpoint.get('namespace')}"
        )
    return lines


def _render_costs(data: dict[str, Any]) -> list[str]:
    lines = []
    if "total" in data:
        lines.append(f"total ${data.get('total', 0.0):.2f} over {data.get('window_days')} days")
    for item in data.get("by_service", []):
        lines.append(f"{item.get('service', ''):<40}  ${item.get('amount', 0.0):.2f}")
    tags = data.get("allocation_tags")
    if tags is not None:
        rendered_tags = ", ".join(
            f"{item.get('tag_key', '')}={item.get('status', 'unknown')}" for item in tags
        )
        lines.append(f"cost allocation tags: {rendered_tags or 'none'}")
    if data.get("as_of"):
        lines.append(f"as of {data['as_of']}")
    return lines


def _render_nodepools(data: dict[str, Any]) -> list[str]:
    lines = []
    for region, entry in data.get("by_region", {}).items():
        pools = entry.get("nodepools")
        if pools is None:
            lines.append(f"{region:<15}  {entry.get('note', 'endpoint not reachable')}")
        else:
            names = ", ".join(str(p.get("name")) for p in pools) or "none"
            lines.append(f"{region:<15}  {len(pools)} nodepools: {names}")
    return lines


_SECTION_BODIES = {
    SECTION_REGIONS: _render_regions,
    SECTION_STACKS: _render_stacks,
    SECTION_QUEUE: _render_queue,
    SECTION_JOBS: _render_jobs,
    SECTION_CAPACITY: _render_capacity,
    SECTION_INFERENCE: _render_inference,
    SECTION_COSTS: _render_costs,
    SECTION_NODEPOOLS: _render_nodepools,
}


def _render_section(section: Section) -> list[str]:
    """One compact block: heading with status and drill-down, then detail."""
    heading = f"{section.name} [{section.status}]"
    drill = _DRILL_DOWNS.get(section.name)
    if drill:
        heading = f"{heading}  ·  {drill}"
    lines = [heading]

    if section.status in (STATUS_OK, STATUS_EMPTY):
        render_body = _SECTION_BODIES.get(section.name)
        body = render_body(section.data) if render_body else []
        if section.status == STATUS_EMPTY and not body:
            body = ["nothing here — the read succeeded and found no records"]
        lines.extend(f"  {line}" for line in body)
    else:
        # Skipped and unavailable sections show their reason instead of
        # silently vanishing from the summary.
        lines.append(f"  {section.reason or 'no reason recorded'}")
        for error in section.errors[:5]:
            lines.append(f"  error: {error}")
        remaining = len(section.errors) - 5
        if remaining > 0:
            lines.append(f"  ... and {remaining} more error(s)")
    return lines


def _render_table(doc: FleetStatus) -> None:
    """Render the document for a terminal reader.

    The nested document would collapse to ``<dict>`` cells inside the
    generic table formatter, so this renderer is hand-rolled: verdict
    first, findings second, then one block per section.
    """
    print(f"Fleet status: {doc.overall.upper()}  project {doc.project_name}")
    print(f"generated {doc.generated_at}")
    if doc.degraded:
        print(f"degraded sections: {', '.join(doc.degraded)}")

    print()
    if doc.findings:
        print("Findings:")
        for finding in doc.findings:
            print(f"  [{finding.severity}] {finding.section}: {finding.message}")
    else:
        print("Findings: none — nothing looks wrong.")

    for name in SECTION_ORDER:
        section = doc.sections.get(name)
        if section is None:
            continue
        print()
        for line in _render_section(section):
            print(line)


def _has_error_finding(doc: FleetStatus) -> bool:
    return any(finding.severity == SEVERITY_ERROR for finding in doc.findings)


def _watch_loop(
    config: GCOConfig,
    *,
    region: str | None,
    with_costs: bool,
    with_nodepools: bool,
    interval: int,
    fail_on_findings: bool,
    with_policy: bool = False,
) -> None:
    """Re-gather and redraw until interrupted.

    The costs section is re-fetched at most once per
    ``COST_REFRESH_INTERVAL_SECONDS``; ticks in between reuse the previous
    section, whose ``as_of`` timestamp shows when the figure was actually
    retrieved. The reuse is in-process only — no cache file is written.
    """
    cached_costs: Section | None = None
    cached_costs_at = 0.0
    while True:
        now = time.monotonic()
        reuse = None
        if (
            with_costs
            and cached_costs is not None
            and now - cached_costs_at < COST_REFRESH_INTERVAL_SECONDS
        ):
            reuse = cached_costs
        doc = gather_fleet_status(
            config,
            region=region,
            with_costs=with_costs,
            with_nodepools=with_nodepools,
            with_policy=with_policy,
            costs_cache=reuse,
        )
        if with_costs and reuse is None:
            cached_costs = doc.sections.get(SECTION_COSTS)
            cached_costs_at = now

        click.clear()
        _render_table(doc)
        if fail_on_findings and _has_error_finding(doc):
            sys.exit(1)
        print(f"\nrefreshing every {interval}s — Ctrl-C to stop")
        time.sleep(interval)


@click.command("status")
@click.option("--region", "-r", help="Restrict the gather to a single region")
@click.option(
    "--with-costs",
    is_flag=True,
    help="Include the costs section (Cost Explorer bills per request)",
)
@click.option(
    "--with-nodepools",
    is_flag=True,
    help="Include Karpenter nodepools (requires a reachable cluster API endpoint)",
)
@click.option(
    "--watch",
    type=int,
    default=None,
    metavar="SECONDS",
    help=(
        f"Re-gather and redraw every SECONDS (minimum "
        f"{WATCH_INTERVAL_FLOOR_SECONDS}; table output only)"
    ),
)
@click.option(
    "--with-policy",
    is_flag=True,
    help=(
        "Compare the job-validation policy each region enforces and report any "
        "field that differs (one API call per region)"
    ),
)
@click.option(
    "--fail-on-findings",
    is_flag=True,
    help=(
        "Exit 1 when any error-severity finding is present (after rendering); "
        "with --watch, exits on the first tick that carries one"
    ),
)
@pass_config
def status(
    config: GCOConfig,
    region: str | None,
    with_costs: bool,
    with_nodepools: bool,
    with_policy: bool,
    watch: int | None,
    fail_on_findings: bool,
) -> None:
    """Show fleet-wide deployment status across configured regions.

    Aggregates control-plane state — stacks, queue depth, jobs, capacity,
    and inference endpoints — into one document. Every section carries its
    own status, so a failed read degrades that section instead of hiding
    the rest. Reads that bill per request or need cluster reachability are
    opt-in flags.

    Examples:
        gco status
        gco status -r us-east-1
        gco status --output json
        gco status --with-costs --with-nodepools
        gco status --with-policy
        gco status --watch 10
        gco status --fail-on-findings
    """
    formatter = get_output_formatter(config)

    if watch is not None:
        if config.output_format != "table":
            formatter.print_error(
                "--watch requires table output; a repeating stream of documents "
                "is not consumable as JSON or YAML"
            )
            sys.exit(1)
        if watch < WATCH_INTERVAL_FLOOR_SECONDS:
            formatter.print_error(
                f"--watch interval must be at least {WATCH_INTERVAL_FLOOR_SECONDS} seconds"
            )
            sys.exit(1)
        try:
            _watch_loop(
                config,
                region=region,
                with_costs=with_costs,
                with_nodepools=with_nodepools,
                with_policy=with_policy,
                interval=watch,
                fail_on_findings=fail_on_findings,
            )
        except KeyboardInterrupt:
            return
        return

    doc = gather_fleet_status(
        config,
        region=region,
        with_costs=with_costs,
        with_nodepools=with_nodepools,
        with_policy=with_policy,
    )

    if config.output_format == "table":
        _render_table(doc)
    else:
        formatter.print(doc)

    if fail_on_findings and _has_error_finding(doc):
        sys.exit(1)
