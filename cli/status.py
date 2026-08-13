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
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
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

SECTION_ORDER: tuple[str, ...] = (
    SECTION_REGIONS,
    SECTION_STACKS,
    SECTION_QUEUE,
    SECTION_JOBS,
    SECTION_CAPACITY,
    SECTION_INFERENCE,
    SECTION_COSTS,
    SECTION_NODEPOOLS,
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
    the deadline reports ``error`` naming the timeout; its thread is
    abandoned rather than allowed to hold the document.
    """
    pool = ThreadPoolExecutor(max_workers=max(1, len(gatherers)))
    futures = {name: pool.submit(_run_section, name, gather) for name, gather in gatherers.items()}
    done, _ = wait(futures.values(), timeout=SECTION_TIMEOUT_SECONDS)
    sections: dict[str, Section] = {}
    for name, future in futures.items():
        if future in done:
            sections[name] = future.result()
        else:
            future.cancel()
            sections[name] = Section(
                name=name,
                status=STATUS_ERROR,
                reason=f"the gather exceeded the {SECTION_TIMEOUT_SECONDS}s section timeout",
            )
    # Let stragglers finish in the background instead of blocking the
    # document on them here.
    pool.shutdown(wait=False, cancel_futures=True)
    return sections


def _regions_unavailable_section(name: str) -> Section:
    """Section placeholder used when the workload region list is unresolved."""
    return Section(
        name=name,
        status=STATUS_UNAVAILABLE,
        reason="deployment regions could not be resolved; see the regions section",
    )


def _pending_section(name: str) -> Section:
    """Placeholder for a section whose gatherer does not exist yet."""
    return Section(name=name, status=STATUS_SKIPPED, reason="gathering is not implemented yet")


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
        result = aws_client.call_api(method="GET", path="/api/v1/queue/stats", region=query_region)
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
    """Gather the ``costs`` section, or report it skipped when not requested."""
    del config
    if not requested:
        return Section(
            name=SECTION_COSTS,
            status=STATUS_SKIPPED,
            reason="not requested; pass --with-costs (Cost Explorer bills per request)",
        )
    return _pending_section(SECTION_COSTS)


def _gather_nodepools(config: GCOConfig, requested: bool, workload: list[str]) -> Section:
    """Gather the ``nodepools`` section, or report it skipped when not requested."""
    del config, workload
    if not requested:
        return Section(
            name=SECTION_NODEPOOLS,
            status=STATUS_SKIPPED,
            reason="not requested; pass --with-nodepools (requires cluster API reachability)",
        )
    return _pending_section(SECTION_NODEPOOLS)


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
) -> FleetStatus:
    """Gather every section and assemble the fleet status document.

    Always returns a document: sections that cannot be gathered degrade
    individually and the rest are unaffected.
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
    gatherers[SECTION_COSTS] = lambda: _gather_costs(config, with_costs)
    gatherers[SECTION_NODEPOOLS] = lambda: _gather_nodepools(config, with_nodepools, workload)

    sections: dict[str, Section] = {SECTION_REGIONS: regions_section}
    if not workload:
        for name in sorted(_PER_REGION_SECTIONS):
            sections[name] = _regions_unavailable_section(name)
    sections.update(_run_sections_concurrently(gatherers))

    findings: list[Finding] = []
    overall, degraded = _derive_overall(sections, findings)

    return FleetStatus(
        generated_at=datetime.now(UTC).isoformat(),
        project_name=config.project_name,
        overall=overall,
        degraded=degraded,
        findings=findings,
        sections={name: sections[name] for name in SECTION_ORDER},
    )
