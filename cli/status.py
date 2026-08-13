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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cli.config import GCOConfig, _load_cdk_json

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

    sections: dict[str, Section] = {SECTION_REGIONS: regions_section}
    for name in (SECTION_STACKS, SECTION_QUEUE, SECTION_JOBS, SECTION_CAPACITY, SECTION_INFERENCE):
        if name in _PER_REGION_SECTIONS and not workload:
            sections[name] = _regions_unavailable_section(name)
        else:
            sections[name] = _pending_section(name)
    sections[SECTION_COSTS] = _run_section(SECTION_COSTS, lambda: _gather_costs(config, with_costs))
    sections[SECTION_NODEPOOLS] = _run_section(
        SECTION_NODEPOOLS, lambda: _gather_nodepools(config, with_nodepools, workload)
    )

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
