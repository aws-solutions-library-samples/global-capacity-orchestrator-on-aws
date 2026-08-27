"""Fleet-wide status document assembly for ``gco status``.

Gathers control-plane state across the configured deployment regions and
returns it as a single :class:`FleetStatus` document. Every section is
gathered independently behind a uniform envelope and carries its own status,
so a failure in one section never suppresses the rest, and "there is nothing
there" is always distinguishable from "the read could not be performed".

The document — not any one rendering of it — is the public contract:
``gco status --output json`` emits it unchanged, and the ``fleet_status``
MCP tool returns that JSON to agents.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from cli.config import GCOConfig, _load_cdk_json

if TYPE_CHECKING:
    from cli.capacity.multi_region import RegionCapacity
    from cli.stacks import StackInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------
# Module-level constants rather than bare strings at call sites, so a typo is
# an AttributeError instead of a silently wrong status.

#: The read succeeded and returned data.
STATUS_OK = "ok"
#: The read succeeded and there is genuinely nothing — a success, not a
#: degradation.
STATUS_EMPTY = "empty"
#: The read succeeded for some regions or signals and failed for others.
STATUS_PARTIAL = "partial"
#: The read could not be attempted, for a known and explainable reason.
STATUS_UNAVAILABLE = "unavailable"
#: The read was attempted and failed unexpectedly.
STATUS_ERROR = "error"
#: The section was not requested (an opt-in section without its flag).
STATUS_SKIPPED = "skipped"

#: Finding severities.
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

#: Overall document verdicts.
OVERALL_OK = "ok"
OVERALL_DEGRADED = "degraded"

# Section names. The tuple fixes the rendering and JSON key order.
SECTION_REGIONS = "regions"
SECTION_STACKS = "stacks"
SECTION_QUEUE = "queue"
SECTION_JOBS = "jobs"
SECTION_CAPACITY = "capacity"
SECTION_INFERENCE = "inference"
SECTION_COSTS = "costs"
SECTION_NODEPOOLS = "nodepools"
SECTION_POLICY = "policy"

SECTION_ORDER: tuple[str, ...] = (
    SECTION_REGIONS,
    SECTION_STACKS,
    SECTION_QUEUE,
    SECTION_JOBS,
    SECTION_CAPACITY,
    SECTION_INFERENCE,
    SECTION_COSTS,
    SECTION_NODEPOOLS,
    SECTION_POLICY,
)

# Sections that fan out over the resolved workload region list and therefore
# cannot be gathered at all when region resolution fails.
_PER_REGION_SECTIONS = frozenset({SECTION_STACKS, SECTION_QUEUE, SECTION_CAPACITY})

# Section statuses that degrade the overall document. ``skipped`` is absent
# on purpose: skipping is what the operator asked for.
_DEGRADED_STATUSES = frozenset({STATUS_PARTIAL, STATUS_UNAVAILABLE, STATUS_ERROR})

# Source markers for the ``regions`` section, so a reader can tell a
# configured topology from a flag-narrowed one.
REGION_SOURCE_CDK_JSON = "cdk.json"
REGION_SOURCE_FLAG = "--region flag"

# Stack health classification. Anything ending ``_IN_PROGRESS`` is a deploy
# in flight; ``UPDATE_ROLLBACK_COMPLETE`` and friends mean the last deploy
# did not take, which is unhealthy even though CloudFormation is at rest.
_HEALTHY_STACK_STATUSES = frozenset(
    {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "IMPORT_COMPLETE",
    }
)
_IN_PROGRESS_SUFFIX = "_IN_PROGRESS"

HEALTH_HEALTHY = "healthy"
HEALTH_IN_PROGRESS = "in-progress"
HEALTH_NOT_DEPLOYED = "not-deployed"
HEALTH_UNHEALTHY = "unhealthy"

#: Wall-clock budget for one section's gather; a section that exceeds it
#: reports ``error`` instead of holding the whole document.
SECTION_TIMEOUT_SECONDS = 30

#: Cost Explorer window for the opt-in ``costs`` section.
COST_WINDOW_DAYS = 30

#: Minimum ``--watch`` interval, so watch mode cannot hammer AWS APIs.
WATCH_INTERVAL_FLOOR_SECONDS = 5

#: Minimum spacing between Cost Explorer fetches under ``--watch``.
#: Cost Explorer bills per request and its data does not change minute to
#: minute; the in-between ticks reuse the last section, whose ``as_of``
#: shows when the figure was actually retrieved.
COST_REFRESH_INTERVAL_SECONDS = 15 * 60

#: Ceiling for concurrent per-region (or per-stack) reads within a section.
_MAX_FANOUT_WORKERS = 8


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One independently gathered part of the status document."""

    name: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    """Something that looks wrong, derived from an already-gathered document."""

    severity: str
    section: str
    message: str


@dataclass(frozen=True)
class FleetStatus:
    """The whole fleet status document."""

    generated_at: str
    project_name: str
    overall: str
    degraded: list[str]
    findings: list[Finding]
    sections: dict[str, Section]


# ---------------------------------------------------------------------------
# Region topology resolution
# ---------------------------------------------------------------------------

_REGIONS_UNAVAILABLE_REASON = (
    "deployment regions are not configured; run from a checkout containing "
    "cdk.json (context.deployment_regions.regional) or pass --region to name "
    "one explicitly"
)


def resolve_regions(config: GCOConfig, region: str | None = None) -> Section:
    """Resolve the deployment topology the rest of the document fans out over.

    Reads ``context.deployment_regions`` from ``cdk.json``. An explicit
    ``region`` narrows the workload list to exactly that region and records
    the narrowing in ``source``. This function never falls back to scanning
    AWS regions for stacks — an unresolvable topology is reported as
    ``unavailable`` instead of being guessed at.
    """
    cdk_regions = _load_cdk_json()
    configured = [item for item in cdk_regions.get("regional", []) if isinstance(item, str)]

    if region:
        workload = [region]
        source = REGION_SOURCE_FLAG
    elif configured:
        workload = configured
        source = REGION_SOURCE_CDK_JSON
    else:
        return Section(
            name=SECTION_REGIONS,
            status=STATUS_UNAVAILABLE,
            reason=_REGIONS_UNAVAILABLE_REASON,
        )

    return Section(
        name=SECTION_REGIONS,
        status=STATUS_OK,
        data={
            "global": cdk_regions.get("global", config.global_region),
            "api_gateway": cdk_regions.get("api_gateway", config.api_gateway_region),
            "monitoring": cdk_regions.get("monitoring", config.monitoring_region),
            "workload": workload,
            "source": source,
        },
    )


def _workload_regions(regions_section: Section) -> list[str]:
    """Return the resolved workload region list, or ``[]`` when unresolved."""
    if regions_section.status != STATUS_OK:
        return []
    workload = regions_section.data.get("workload", [])
    return [item for item in workload if isinstance(item, str)]


# ---------------------------------------------------------------------------
# Section gathering
# ---------------------------------------------------------------------------


def _run_section(name: str, gather: Callable[[], Section]) -> Section:
    """Run one section gatherer, absorbing any escaping exception.

    This boundary is what makes "a failure in one section never suppresses
    the rest" structural: no gatherer exception can reach the renderer.
    """
    try:
        return gather()
    except Exception as e:
        logger.debug("Status section %s failed: %s", name, e)
        return Section(
            name=name,
            status=STATUS_ERROR,
            reason="the read failed unexpectedly",
            errors=[f"{type(e).__name__}: {e}"],
        )


def _run_sections_concurrently(gatherers: dict[str, Callable[[], Section]]) -> dict[str, Section]:
    """Run section gatherers in parallel under the shared wall-clock budget.

    Every gatherer gets the full :data:`SECTION_TIMEOUT_SECONDS` of wall
    clock because they run concurrently. A section that has not finished by
    the deadline reports ``error`` naming the timeout. Sections run on
    daemon threads so an abandoned straggler — a hung subprocess probe or
    an unresponsive endpoint — can neither hold the document nor block
    process exit afterwards.
    """
    results: dict[str, Section] = {}
    lock = threading.Lock()

    def run(name: str, gather: Callable[[], Section]) -> None:
        section = _run_section(name, gather)
        with lock:
            results[name] = section

    threads = {
        name: threading.Thread(target=run, args=(name, gather), name=f"status-{name}", daemon=True)
        for name, gather in gatherers.items()
    }
    for thread in threads.values():
        thread.start()
    deadline = time.monotonic() + SECTION_TIMEOUT_SECONDS
    for thread in threads.values():
        thread.join(max(0.0, deadline - time.monotonic()))

    sections: dict[str, Section] = {}
    with lock:
        for name in gatherers:
            sections[name] = results.get(name) or Section(
                name=name,
                status=STATUS_ERROR,
                reason=f"the gather exceeded the {SECTION_TIMEOUT_SECONDS}s section timeout",
            )
    return sections


def _regions_unavailable_section(name: str) -> Section:
    """Section placeholder used when the workload region list is unresolved."""
    return Section(
        name=name,
        status=STATUS_UNAVAILABLE,
        reason="deployment regions could not be resolved; see the regions section",
    )


def _fanout_workers(count: int) -> int:
    """Bounded worker count for a per-region or per-stack fan-out."""
    return max(1, min(_MAX_FANOUT_WORKERS, count))


def _probe_regional_stacks(config: GCOConfig, workload: list[str]) -> dict[str, StackInfo | None]:
    """Describe each workload region's regional stack directly.

    This single probe round feeds the ``stacks`` section and gates the
    ``queue`` and ``capacity`` gathers: both delegate to managers that fall
    back to scanning every AWS region when stack discovery comes up empty,
    and this command must never trigger that scan. ``None`` means the stack
    is absent or not readable — CloudFormation reads here never raise.
    """
    from cli.stacks import get_stack_manager

    manager = get_stack_manager(config)

    def probe(region: str) -> StackInfo | None:
        return manager.get_stack_status(f"{config.regional_stack_prefix}-{region}", region)

    with ThreadPoolExecutor(max_workers=_fanout_workers(len(workload))) as pool:
        return dict(zip(workload, pool.map(probe, workload), strict=True))


# ---------------------------------------------------------------------------
# stacks
# ---------------------------------------------------------------------------


def _classify_stack_health(status: str | None) -> str:
    """Classify a CloudFormation stack status for the document."""
    if status is None:
        return HEALTH_NOT_DEPLOYED
    if status in _HEALTHY_STACK_STATUSES:
        return HEALTH_HEALTHY
    if status.endswith(_IN_PROGRESS_SUFFIX):
        return HEALTH_IN_PROGRESS
    return HEALTH_UNHEALTHY


def _stack_entry(name: str, region: str, info: StackInfo | None) -> dict[str, Any]:
    """One stack's document entry; ``info`` is None when absent or unreadable."""
    status = info.status if info else None
    updated = info.updated_time.isoformat() if info and info.updated_time else None
    return {
        "name": name,
        "region": region,
        "status": status,
        "health": _classify_stack_health(status),
        "updated_time": updated,
    }


def _gather_stacks(
    config: GCOConfig,
    regions_data: dict[str, Any],
    workload: list[str],
) -> Section:
    """Describe every expected and optional stack of the deployment.

    Expected stacks are the global, API-gateway, and monitoring stacks plus
    one regional stack per workload region. The per-region API bridges and
    the analytics stack are optional — not deploying them is a valid
    configuration — so they are listed only when present and their absence
    never produces a finding.
    """
    from cli.stacks import get_stack_manager

    project = config.project_name
    global_region = str(regions_data.get("global", config.global_region))
    api_gateway_region = str(regions_data.get("api_gateway", config.api_gateway_region))
    monitoring_region = str(regions_data.get("monitoring", config.monitoring_region))

    expected: list[tuple[str, str]] = [
        (config.global_stack_name, global_region),
        (config.api_gateway_stack_name, api_gateway_region),
        (f"{project}-monitoring", monitoring_region),
    ]
    expected.extend((f"{config.regional_stack_prefix}-{region}", region) for region in workload)
    optional: list[tuple[str, str]] = [
        (f"{project}-regional-api-{region}", region) for region in workload
    ]
    optional.append((f"{project}-analytics", api_gateway_region))

    manager = get_stack_manager(config)
    everything = expected + optional

    def describe(spec: tuple[str, str]) -> StackInfo | None:
        return manager.get_stack_status(spec[0], spec[1])

    with ThreadPoolExecutor(max_workers=_fanout_workers(len(everything))) as pool:
        described = dict(
            zip([name for name, _ in everything], pool.map(describe, everything), strict=True)
        )

    expected_entries = [
        _stack_entry(name, region, described.get(name)) for name, region in expected
    ]
    optional_entries = [
        _stack_entry(name, region, described[name])
        for name, region in optional
        if described.get(name) is not None
    ]

    return Section(
        name=SECTION_STACKS,
        status=STATUS_OK,
        data={"expected": expected_entries, "optional": optional_entries},
    )


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------


def _gather_queue(
    config: GCOConfig,
    workload: list[str],
    regional_probe: dict[str, StackInfo | None],
    checkout_configured: bool,
) -> Section:
    """Read job-queue and dead-letter-queue depth per workload region.

    Regions whose regional stack is absent are reported ``unavailable``
    without attempting the read: the queue lookup rediscovers stacks
    internally and must only run when the fast discovery path is guaranteed
    to succeed.
    """
    from cli.jobs import get_job_manager

    manager = get_job_manager(config)
    by_region: dict[str, dict[str, int | None]] = {}
    errors: list[str] = []
    unavailable = 0

    def read(region: str) -> dict[str, Any] | None:
        return manager.get_queue_status(region)

    prefix = config.regional_stack_prefix
    readable = [region for region in workload if regional_probe.get(region) is not None]
    for region in workload:
        if region not in readable:
            unavailable += 1
            errors.append(f"{region}: regional stack {prefix}-{region} is absent or not readable")
    if readable and not checkout_configured:
        unavailable += len(readable)
        errors.extend(
            f"{region}: queue reads need the configured region list from cdk.json"
            for region in readable
        )
        readable = []

    results: dict[str, dict[str, Any] | Exception] = {}
    if readable:
        with ThreadPoolExecutor(max_workers=_fanout_workers(len(readable))) as pool:
            futures = {region: pool.submit(read, region) for region in readable}
            for region, future in futures.items():
                try:
                    results[region] = future.result() or {}
                except Exception as e:
                    results[region] = e

    unexpected = 0
    for region in readable:
        outcome = results[region]
        if isinstance(outcome, ValueError):
            unavailable += 1
            errors.append(f"{region}: {outcome}")
        elif isinstance(outcome, Exception):
            unexpected += 1
            errors.append(f"{region}: {type(outcome).__name__}: {outcome}")
        else:
            by_region[region] = {
                "available": int(outcome.get("messages_available", 0)),
                "in_flight": int(outcome.get("messages_in_flight", 0)),
                "delayed": int(outcome.get("messages_delayed", 0)),
                "dlq": outcome.get("dlq_messages"),
            }

    totals = {
        "available": sum(entry["available"] or 0 for entry in by_region.values()),
        "in_flight": sum(entry["in_flight"] or 0 for entry in by_region.values()),
        "delayed": sum(entry["delayed"] or 0 for entry in by_region.values()),
        "dlq": sum(entry["dlq"] or 0 for entry in by_region.values()),
    }
    data = {"by_region": by_region, "totals": totals}

    if by_region and not errors:
        return Section(name=SECTION_QUEUE, status=STATUS_OK, data=data)
    if by_region:
        reason = f"queue depth unavailable for {len(workload) - len(by_region)} of {len(workload)} regions"
        return Section(
            name=SECTION_QUEUE, status=STATUS_PARTIAL, data=data, reason=reason, errors=errors
        )
    if unexpected:
        return Section(
            name=SECTION_QUEUE,
            status=STATUS_ERROR,
            data=data,
            reason="the job queue could not be read in any workload region",
            errors=errors,
        )
    return Section(
        name=SECTION_QUEUE,
        status=STATUS_UNAVAILABLE,
        data=data,
        reason=(
            f"no readable job queue in any workload region; deploy the regional "
            f"stack(s) with `gco stacks deploy {prefix}-<region>`"
        ),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def _gather_jobs(config: GCOConfig, region: str | None) -> Section:
    """Read fleet-wide job counts from the queue-statistics API route."""
    from cli.aws_client import get_aws_client

    aws_client = get_aws_client(config)
    query_region = region or (config.default_region if config.use_regional_api else None)
    try:
        # A status snapshot reports a failing route honestly instead of
        # retrying through it; the next gather re-reads anyway, and retries
        # here can outlive the section timeout.
        result = aws_client.call_api(
            method="GET", path="/api/v1/queue/stats", region=query_region, max_attempts=1
        )
    except RuntimeError as e:
        if "API endpoint" not in str(e):
            raise
        stack = config.api_gateway_stack_name
        return Section(
            name=SECTION_JOBS,
            status=STATUS_UNAVAILABLE,
            reason=(f"the {stack} API is unreachable; deploy it with `gco stacks deploy {stack}`"),
            errors=[str(e)],
        )

    summary = result.get("summary") or {}
    by_region = result.get("by_region") or {}
    totals = {
        "total": int(summary.get("total_jobs", 0)),
        "queued": int(summary.get("total_queued", 0)),
        "running": int(summary.get("total_running", 0)),
    }
    data: dict[str, Any] = {
        "totals": totals,
        "by_region": by_region,
        "complete": bool(summary.get("complete", True)),
        "records_evaluated": summary.get("records_evaluated"),
    }
    status = STATUS_EMPTY if not by_region and totals["total"] == 0 else STATUS_OK
    return Section(name=SECTION_JOBS, status=status, data=data)


# ---------------------------------------------------------------------------
# capacity
# ---------------------------------------------------------------------------


def _capacity_entry_from(cap: RegionCapacity) -> dict[str, Any]:
    """Document entry for one region's capacity sweep result."""
    return {
        "queue_depth": int(cap.queue_depth),
        "running_jobs": int(cap.running_jobs),
        "gpu_utilization": float(cap.gpu_utilization),
        "cpu_utilization": float(cap.cpu_utilization),
        "telemetry_status": str(cap.telemetry_status),
        "unavailable_signals": list(cap.unavailable_signals),
    }


def _capacity_unavailable_entry() -> dict[str, Any]:
    """Entry for a region whose capacity telemetry could not be attempted."""
    return {
        "queue_depth": 0,
        "running_jobs": 0,
        "gpu_utilization": 0.0,
        "cpu_utilization": 0.0,
        "telemetry_status": STATUS_UNAVAILABLE,
        "unavailable_signals": ["queue", "gpu", "cpu"],
    }


def _gather_capacity(
    config: GCOConfig,
    workload: list[str],
    configured: list[str],
    regional_probe: dict[str, StackInfo | None],
) -> Section:
    """Read per-region queue depth and utilization with telemetry provenance.

    The multi-region checker rediscovers stacks internally and falls back to
    scanning every AWS region when discovery finds nothing, so it is invoked
    only when at least one probed regional stack exists and the configured
    region list is non-empty — conditions under which the fast discovery
    path always succeeds. Regions failing that gate get an honest
    ``unavailable`` telemetry entry instead.
    """
    from cli.capacity import get_multi_region_capacity_checker

    prefix = config.regional_stack_prefix
    by_region: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    present = [region for region in workload if regional_probe.get(region) is not None]
    sweep_returned_empty = False
    if present and configured:
        checker = get_multi_region_capacity_checker(config)
        if set(workload) == set(configured):
            capacities = checker.get_all_regions_capacity()
            # Sweep-level failures are what make an empty result mean
            # "checks failed" rather than "no regions".
            errors.extend(checker._last_region_errors)
            sweep_returned_empty = not capacities
            for cap in capacities:
                if cap.region in workload:
                    by_region[cap.region] = _capacity_entry_from(cap)
                    errors.extend(f"{cap.region}: {err}" for err in cap.telemetry_errors)
        else:
            for region in present:
                try:
                    cap = checker.get_region_capacity(region)
                except Exception as e:
                    errors.append(f"{region}: {e}")
                else:
                    by_region[region] = _capacity_entry_from(cap)
                    errors.extend(f"{region}: {err}" for err in cap.telemetry_errors)
    for region in workload:
        if region in by_region:
            continue
        by_region[region] = _capacity_unavailable_entry()
        if region not in present:
            errors.append(f"{region}: regional stack {prefix}-{region} is absent or not readable")
        elif not configured:
            errors.append(
                f"{region}: capacity telemetry needs the configured region list from cdk.json"
            )

    by_region = {region: by_region[region] for region in workload}
    data = {"by_region": by_region}
    statuses = [entry["telemetry_status"] for entry in by_region.values()]
    incomplete = sum(1 for status in statuses if status != "complete")

    if sweep_returned_empty and errors and all(s == STATUS_UNAVAILABLE for s in statuses):
        return Section(
            name=SECTION_CAPACITY,
            status=STATUS_ERROR,
            data=data,
            reason="the capacity sweep failed for every region",
            errors=errors,
        )
    if all(s == STATUS_UNAVAILABLE for s in statuses):
        return Section(
            name=SECTION_CAPACITY,
            status=STATUS_UNAVAILABLE,
            data=data,
            reason="capacity telemetry could not be attempted in any workload region",
            errors=errors,
        )
    if incomplete:
        return Section(
            name=SECTION_CAPACITY,
            status=STATUS_PARTIAL,
            data=data,
            reason=f"{incomplete} of {len(statuses)} regions reported incomplete telemetry",
            errors=errors,
        )
    return Section(name=SECTION_CAPACITY, status=STATUS_OK, data=data, errors=errors)


# ---------------------------------------------------------------------------
# inference
# ---------------------------------------------------------------------------

_ENDPOINT_FIELDS = ("endpoint_name", "desired_state", "target_regions", "namespace", "updated_at")


def _gather_inference(config: GCOConfig) -> Section:
    """Summarize inference endpoint desired state from the global registry."""
    from cli.inference import get_inference_manager

    manager = get_inference_manager(config)
    try:
        endpoints = manager.list_endpoints()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        stack = config.global_stack_name
        return Section(
            name=SECTION_INFERENCE,
            status=STATUS_UNAVAILABLE,
            reason=(
                f"the inference endpoint registry is not deployed; deploy the "
                f"global stack with `gco stacks deploy {stack}`"
            ),
            errors=[str(e)],
        )

    listed = [
        {field_name: endpoint.get(field_name) for field_name in _ENDPOINT_FIELDS}
        for endpoint in endpoints
    ]
    totals = Counter(str(endpoint.get("desired_state")) for endpoint in endpoints)
    # The registry read is a single unpaginated scan; the count makes any
    # truncation attributable.
    data = {"totals": dict(totals), "count": len(listed), "endpoints": listed}
    status = STATUS_OK if listed else STATUS_EMPTY
    return Section(name=SECTION_INFERENCE, status=status, data=data)


def _gather_costs(config: GCOConfig, requested: bool) -> Section:
    """Read the Cost Explorer summary and cost-allocation-tag status.

    Tier 2: Cost Explorer bills per ``GetCostAndUsage`` request, so this
    section is gathered only on explicit request. No by-region breakdown is
    populated — it would need a second billed request.
    """
    if not requested:
        return Section(
            name=SECTION_COSTS,
            status=STATUS_SKIPPED,
            reason="not requested; pass --with-costs (Cost Explorer bills per request)",
        )

    from cli.costs import get_cost_tracker

    tracker = get_cost_tracker(config)
    summary = tracker.get_cost_summary(days=COST_WINDOW_DAYS)

    errors: list[str] = []
    tags: list[dict[str, str]] | None = None
    try:
        tags = [
            {"tag_key": tag.get("tag_key", ""), "status": tag.get("status", "")}
            for tag in tracker.get_cost_allocation_tag_status()
        ]
    except Exception as e:
        errors.append(f"cost allocation tag status: {e}")

    data: dict[str, Any] = {
        "total": round(summary.total, 2),
        "currency": summary.currency,
        "window_days": COST_WINDOW_DAYS,
        "period_start": summary.period_start,
        "period_end": summary.period_end,
        "by_service": [
            {"service": item.service, "amount": round(item.amount, 2)}
            for item in summary.by_service
        ],
        # Whether the tag filters this total depends on are active in
        # Billing, so a near-zero total is not misread as near-zero spend.
        "allocation_tags": tags,
        "as_of": datetime.now(UTC).isoformat(),
    }
    if errors:
        return Section(
            name=SECTION_COSTS,
            status=STATUS_PARTIAL,
            data=data,
            reason="cost total read, but the allocation-tag status could not be",
            errors=errors,
        )
    status = STATUS_EMPTY if not data["by_service"] and data["total"] == 0 else STATUS_OK
    return Section(name=SECTION_COSTS, status=status, data=data)


_NODEPOOL_FIELDS = ("name", "status", "capacity_types", "instance_types")


def _gather_nodepools(config: GCOConfig, requested: bool, workload: list[str]) -> Section:
    """List Karpenter NodePools per region, probing reachability first.

    Tier 3: the NodePool listing talks straight to the EKS API endpoint,
    which is private by default; against a private endpoint it would block
    until timeout. Each region's endpoint posture is probed first and a
    non-public endpoint is reported ``unavailable`` — the Kubernetes call
    is never attempted in that case.
    """
    if not requested:
        return Section(
            name=SECTION_NODEPOOLS,
            status=STATUS_SKIPPED,
            reason="not requested; pass --with-nodepools (requires cluster API reachability)",
        )
    if not workload:
        return _regions_unavailable_section(SECTION_NODEPOOLS)

    from cli import kubectl_helpers
    from cli.nodepools import list_cluster_nodepools

    by_region: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    listed = 0
    private = 0

    for region in workload:
        cluster = f"{config.project_name}-{region}"
        try:
            access = kubectl_helpers.describe_cluster_access(cluster, region)
        except Exception as e:
            errors.append(f"{region}: {e}")
            by_region[region] = {
                "cluster": cluster,
                "reachable": False,
                "note": "cluster endpoint posture could not be determined",
            }
            continue
        if not access.get("public"):
            private += 1
            by_region[region] = {
                "cluster": cluster,
                "reachable": False,
                "note": (
                    f"cluster endpoint is private; open a tunnel with "
                    f"`gco cluster tunnel --region {region}`"
                ),
            }
            continue
        try:
            pools = list_cluster_nodepools(cluster, region)
        except Exception as e:
            errors.append(f"{region}: {e}")
            by_region[region] = {
                "cluster": cluster,
                "reachable": True,
                "note": "nodepool listing failed",
            }
            continue
        listed += 1
        by_region[region] = {
            "cluster": cluster,
            "reachable": True,
            "nodepools": [
                {field_name: pool.get(field_name) for field_name in _NODEPOOL_FIELDS}
                for pool in pools
            ],
        }

    data = {"by_region": by_region}
    if listed == len(workload):
        return Section(name=SECTION_NODEPOOLS, status=STATUS_OK, data=data)
    if listed:
        return Section(
            name=SECTION_NODEPOOLS,
            status=STATUS_PARTIAL,
            data=data,
            reason=f"nodepools listed in {listed} of {len(workload)} regions",
            errors=errors,
        )
    if private:
        return Section(
            name=SECTION_NODEPOOLS,
            status=STATUS_UNAVAILABLE,
            data=data,
            reason=(
                "no cluster endpoint is publicly reachable; open a tunnel with "
                "`gco cluster tunnel` and use `gco nodepools list` through it"
            ),
            errors=errors,
        )
    return Section(
        name=SECTION_NODEPOOLS,
        status=STATUS_ERROR,
        data=data,
        reason="nodepools could not be listed in any region",
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _gather_policy(config: GCOConfig, requested: bool, workload: list[str]) -> Section:
    """Compare the job-validation policy each region actually enforces.

    Opt-in because it costs a CloudFormation describe plus an API call per
    region, and because it needs the regional API bridge deployed.

    Every region is deployed from the same ``cdk.json`` -- there are no
    per-region policy overrides -- so a field that differs across regions means
    at least one region is running a different deployment of that file. Nothing
    else reports this: each region is individually healthy and self-consistent,
    and the divergence only shows up as a manifest that is admitted in one
    region and refused in another.

    ``trusted_registries`` is compared with ECR hostnames stripped, because CDK
    appends the project's own registries at synth time and those encode a region.
    """
    if not requested:
        return Section(
            name=SECTION_POLICY,
            status=STATUS_SKIPPED,
            reason="not requested; pass --with-policy (one API call per region)",
        )
    if not workload:
        return _regions_unavailable_section(SECTION_POLICY)

    from cli.aws_client import get_aws_client
    from cli.job_policy import (
        detect_policy_drift,
        ecr_augmentation,
        fetch_region_policies,
        registry_drift,
    )

    policies = fetch_region_policies(get_aws_client(config), workload)
    readable = [entry for entry in policies if entry.ok]
    unreadable = {entry.region: entry.reason or "unknown" for entry in policies if not entry.ok}

    drift = detect_policy_drift(policies)
    registries = registry_drift(policies)
    if registries is not None:
        drift = [*drift, registries]

    data: dict[str, Any] = {
        "compared": [entry.region for entry in readable],
        "unreadable": unreadable,
        "agree": not drift,
        "drift": [{"field": item.field, "values": item.values} for item in drift],
        "ecr_augmentation": {r: h for r, h in ecr_augmentation(policies).items() if h},
        "enforcement_gaps": {
            entry.region: entry.enforcement_gaps for entry in readable if entry.enforcement_gaps
        },
    }

    if not readable:
        return Section(
            name=SECTION_POLICY,
            status=STATUS_UNAVAILABLE,
            reason="no region's policy could be read",
            data=data,
            errors=[f"{region}: {reason}" for region, reason in sorted(unreadable.items())],
        )
    if unreadable:
        return Section(
            name=SECTION_POLICY,
            status=STATUS_PARTIAL,
            reason=f"{len(unreadable)} of {len(policies)} regions unreadable",
            data=data,
            errors=[f"{region}: {reason}" for region, reason in sorted(unreadable.items())],
        )
    if len(readable) < 2:
        # One region cannot disagree with itself. Report the policy as read
        # rather than implying agreement was verified.
        return Section(name=SECTION_POLICY, status=STATUS_OK, data=data)
    return Section(name=SECTION_POLICY, status=STATUS_OK, data=data)


def derive_findings(sections: dict[str, Section]) -> list[Finding]:
    """Derive the findings list from an already-gathered document.

    A pure function over section data: it issues no AWS calls, so it cannot
    fail in a new way or slow the gather down. The rule set is closed and
    small on purpose — an open-ended heuristic layer becomes a source of
    false alarms. Only expected stacks produce findings; optional stacks may
    legitimately be undeployed. The result is ordered ``error`` before
    ``warn``, each in document order.
    """
    errors: list[Finding] = []
    warns: list[Finding] = []

    stacks = sections.get(SECTION_STACKS)
    if stacks is not None:
        for entry in stacks.data.get("expected", []):
            name = entry.get("name")
            region = entry.get("region")
            health = entry.get("health")
            if health == HEALTH_UNHEALTHY:
                errors.append(
                    Finding(
                        severity=SEVERITY_ERROR,
                        section=SECTION_STACKS,
                        message=f"{name} is {entry.get('status')} in {region}",
                    )
                )
            elif health == HEALTH_NOT_DEPLOYED:
                # get_stack_status cannot distinguish a missing stack from
                # denied access, so the wording must not assert absence.
                warns.append(
                    Finding(
                        severity=SEVERITY_WARN,
                        section=SECTION_STACKS,
                        message=f"{name} is absent or not readable in {region}",
                    )
                )
            elif health == HEALTH_IN_PROGRESS:
                warns.append(
                    Finding(
                        severity=SEVERITY_WARN,
                        section=SECTION_STACKS,
                        message=f"{name} is {entry.get('status')} in {region}",
                    )
                )

    queue = sections.get(SECTION_QUEUE)
    if queue is not None:
        for region, entry in queue.data.get("by_region", {}).items():
            depth = entry.get("dlq")
            if isinstance(depth, int) and depth > 0:
                plural = "" if depth == 1 else "s"
                warns.append(
                    Finding(
                        severity=SEVERITY_WARN,
                        section=SECTION_QUEUE,
                        message=f"{region} dead-letter queue holds {depth} message{plural}",
                    )
                )

    capacity = sections.get(SECTION_CAPACITY)
    if capacity is not None:
        for region, entry in capacity.data.get("by_region", {}).items():
            telemetry = entry.get("telemetry_status")
            if telemetry == STATUS_UNAVAILABLE:
                errors.append(
                    Finding(
                        severity=SEVERITY_ERROR,
                        section=SECTION_CAPACITY,
                        message=f"{region} telemetry is unavailable",
                    )
                )
            elif telemetry == STATUS_PARTIAL:
                signals = ", ".join(entry.get("unavailable_signals", [])) or "unknown"
                warns.append(
                    Finding(
                        severity=SEVERITY_WARN,
                        section=SECTION_CAPACITY,
                        message=f"{region} telemetry is partial (unavailable: {signals})",
                    )
                )

    jobs = sections.get(SECTION_JOBS)
    if jobs is not None and jobs.data and jobs.data.get("complete", True) is False:
        evaluated = jobs.data.get("records_evaluated")
        warns.append(
            Finding(
                severity=SEVERITY_WARN,
                section=SECTION_JOBS,
                message=(
                    f"the job-count scan was truncated after {evaluated} records; "
                    "totals are a floor, not a count"
                ),
            )
        )

    policy = sections.get(SECTION_POLICY)
    if policy is not None and policy.status in {STATUS_OK, STATUS_PARTIAL}:
        for item in policy.data.get("drift", []):
            field_name = item.get("field")
            values = item.get("values", {})
            spread = "; ".join(f"{region}={value}" for region, value in sorted(values.items()))
            warns.append(
                Finding(
                    severity=SEVERITY_WARN,
                    section=SECTION_POLICY,
                    message=(
                        f"{field_name} differs across regions ({spread}) — there are no "
                        f"per-region policy overrides, so a region is running a different "
                        f"deployment of cdk.json"
                    ),
                )
            )
        for region, namespaces in sorted(policy.data.get("enforcement_gaps", {}).items()):
            warns.append(
                Finding(
                    severity=SEVERITY_WARN,
                    section=SECTION_POLICY,
                    message=(
                        f"{region} cannot read the live ResourceQuota/LimitRange for "
                        f"{', '.join(namespaces)}, so only its front-door caps are "
                        f"reportable (check the manifest-processor Role)"
                    ),
                )
            )

    return errors + warns


# ---------------------------------------------------------------------------
# Verdict derivation
# ---------------------------------------------------------------------------


def _derive_overall(sections: dict[str, Section], findings: list[Finding]) -> tuple[str, list[str]]:
    """Derive the ``overall`` verdict and the responsible-section list.

    The document is degraded when any section is partial, unavailable, or in
    error, or when any finding is present. The ``degraded`` list names the
    sections with a degraded status or an error-severity finding, in section
    order; a warn finding flips the verdict without adding its section.
    """
    responsible = {
        name for name, section in sections.items() if section.status in _DEGRADED_STATUSES
    }
    responsible.update(
        finding.section for finding in findings if finding.severity == SEVERITY_ERROR
    )
    if responsible or findings:
        return OVERALL_DEGRADED, [name for name in SECTION_ORDER if name in responsible]
    return OVERALL_OK, []


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def gather_fleet_status(
    config: GCOConfig,
    *,
    region: str | None = None,
    with_costs: bool = False,
    with_nodepools: bool = False,
    with_policy: bool = False,
    costs_cache: Section | None = None,
) -> FleetStatus:
    """Gather every section and assemble the fleet status document.

    Always returns a document: sections that cannot be gathered degrade
    individually and the rest are unaffected.

    ``costs_cache`` reuses a previously gathered ``costs`` section instead
    of issuing a new Cost Explorer request. It exists for watch mode's
    in-process rate limit only; nothing is ever written to disk.
    """
    regions_section = _run_section(SECTION_REGIONS, lambda: resolve_regions(config, region))
    workload = _workload_regions(regions_section)
    configured = [item for item in _load_cdk_json().get("regional", []) if isinstance(item, str)]

    # One direct describe per regional stack, shared by the queue and
    # capacity gates (and accepted as a duplicate of the stacks section's
    # own read). An empty result keeps the discovery-based managers — whose
    # empty-discovery fallback scans every AWS region — from being invoked.
    regional_probe: dict[str, StackInfo | None] = {}
    if workload:
        try:
            regional_probe = _probe_regional_stacks(config, workload)
        except Exception as e:
            logger.debug("Regional stack probe failed: %s", e)
            regional_probe = dict.fromkeys(workload)

    gatherers: dict[str, Callable[[], Section]] = {}
    if workload:
        gatherers[SECTION_STACKS] = lambda: _gather_stacks(config, regions_section.data, workload)
        gatherers[SECTION_QUEUE] = lambda: _gather_queue(
            config, workload, regional_probe, bool(configured)
        )
        gatherers[SECTION_CAPACITY] = lambda: _gather_capacity(
            config, workload, configured, regional_probe
        )
    gatherers[SECTION_JOBS] = lambda: _gather_jobs(config, region)
    gatherers[SECTION_INFERENCE] = lambda: _gather_inference(config)
    if with_costs and costs_cache is not None:
        reused_costs = costs_cache
        gatherers[SECTION_COSTS] = lambda: reused_costs
    else:
        gatherers[SECTION_COSTS] = lambda: _gather_costs(config, with_costs)
    gatherers[SECTION_NODEPOOLS] = lambda: _gather_nodepools(config, with_nodepools, workload)
    gatherers[SECTION_POLICY] = lambda: _gather_policy(config, with_policy, workload)

    sections: dict[str, Section] = {SECTION_REGIONS: regions_section}
    if not workload:
        for name in sorted(_PER_REGION_SECTIONS):
            sections[name] = _regions_unavailable_section(name)
    sections.update(_run_sections_concurrently(gatherers))

    findings = derive_findings(sections)
    overall, degraded = _derive_overall(sections, findings)

    return FleetStatus(
        generated_at=datetime.now(UTC).isoformat(),
        project_name=config.project_name,
        overall=overall,
        degraded=degraded,
        findings=findings,
        sections={name: sections[name] for name in SECTION_ORDER},
    )
