"""Gather slow-moving live environment signals for the sampling prompt.

The Mission Strategy_Revision prompt has an optional
``=== Environment context ===`` block (see
:class:`mcp.mission.sampling.SamplingPrompt.environment_context`) that
carries cluster + queue snapshots the model would otherwise have to
spend tool calls to discover. This module produces the dict that fills
it.

What lands in the block:

* ``regions``: sorted list of deployed regions.
* ``cluster_metrics``: per-region snapshot keyed by region. Each entry
  carries ``queue_depth``, ``running_jobs``, ``pending_jobs``,
  ``gpu_utilization``, ``cpu_utilization``, ``recommendation_score``.
  The keys mirror the fields :class:`cli.capacity.multi_region.RegionCapacity`
  exposes — the reuse keeps the gather path fast (one CloudWatch +
  one SQS roundtrip per region) and the data shape consistent with
  what ``ai_recommend`` already feeds into its own model prompts.
* ``reservations``: ``{"active_count": int}`` summary (counts only,
  not the full reservation list — the full table is out of scope here
  because the model can fetch it via the ``list_reservations`` tool
  if it needs the detail).

What deliberately does not land in the block:

* Spot prices and on-demand prices — large, AZ-fanout, and cheap to
  fetch on demand from the ``spot_prices`` tool when the model has
  picked an instance shape. Putting them in the prompt up front
  inflates the byte budget for every iteration even when the model
  never touches that signal.
* Capacity-block offerings — same reason; the ``reservation_check``
  tool surfaces them when needed.
* Anything timestamp-stamped to second precision. The byte-identical
  determinism property in :func:`tests.test_mission_sampling.test_assemble_is_deterministic`
  would start flapping if the prompt embedded a wall clock.

Failure semantics: every AWS call is wrapped. A total credential failure
or a missing capacity checker returns ``None`` so the sampling prompt
omits the section entirely. Per-region partial failures land as the
default zeroed :class:`RegionCapacity` shape — the model sees the region
in the list with all-zero metrics rather than not at all, which is the
honest representation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from collections.abc import Mapping

    from cli.capacity.multi_region import MultiRegionCapacityChecker

logger = logging.getLogger(__name__)


__all__ = ["gather_session_environment"]


def _safe_get_checker() -> MultiRegionCapacityChecker | None:
    """Return a multi-region checker, or ``None`` when AWS isn't reachable.

    Lazy-imports ``boto3``/``cli.capacity.multi_region`` so a session
    that opted out of sampling never pays the import cost. Any
    construction failure (no boto3 installed, no resolved credentials,
    config-loader bombs out) is logged at debug level and reported as
    ``None`` — the caller treats that as "skip the env section".
    """
    try:
        from cli.capacity.multi_region import (  # noqa: PLC0415
            get_multi_region_capacity_checker,
        )

        return get_multi_region_capacity_checker()
    except Exception as exc:  # noqa: BLE001
        logger.debug("environment context gather: checker init failed: %s", exc)
        return None


def _summarise_reservations(
    checker: MultiRegionCapacityChecker,
    regions: list[str],
) -> dict[str, Any]:
    """Return a counts-only reservation summary across ``regions``.

    The CapacityChecker exposes a ``list_capacity_reservations(region)``
    API that returns the full reservation list for a single region.
    The model rarely needs every field — what shifts strategy is "are
    there active CRs the operator can target?". So we return
    ``{"active_count": int, "by_region": {region: int}}`` only, with
    the full surface accessible through the ``list_reservations``
    tool if the model wants to drill in.

    Per-region failures land as ``0`` in ``by_region`` so the output
    shape is stable regardless of which probe succeeded.
    """
    try:
        from cli.capacity.checker import CapacityChecker  # noqa: PLC0415

        capacity_checker = CapacityChecker(checker.config)
    except Exception as exc:  # noqa: BLE001
        logger.debug("environment context gather: reservation checker init failed: %s", exc)
        return {"active_count": 0, "by_region": {}, "_error": "reservation_probe_failed"}

    by_region: dict[str, int] = {}
    total_active = 0
    for region in regions:
        try:
            reservations = capacity_checker.list_capacity_reservations(region, state="active")
            count = len(reservations)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "environment context gather: reservation list failed for %s: %s", region, exc
            )
            count = 0
        by_region[region] = count
        total_active += count

    return {"active_count": int(total_active), "by_region": by_region}


def gather_session_environment(
    session: Mapping[str, Any] | None = None,
    *,
    multi_region_checker: MultiRegionCapacityChecker | None = None,
) -> dict[str, Any] | None:
    """Build the environment-context dict for a Mission session.

    Args:
        session: Optional Mission session state. Reserved for future
            use (today the gather logic is session-independent — the
            argument exists so call sites can pin their interest
            without churn when, e.g., per-session region scoping
            lands).
        multi_region_checker: Optional pre-built checker. When ``None``,
            the gather function builds a fresh one. Tests pass a stub
            here to bypass boto3 entirely.

    Returns:
        A JSON-safe dict suitable to hand to
        :class:`mcp.mission.sampling.SamplingPrompt.environment_context`.
        Returns ``None`` when no checker is available (offline mode,
        no AWS creds, boto3 not installed) — the prompt then omits the
        section entirely. Sorted-key emission and zero timestamps keep
        the output byte-identical across two calls with the same
        underlying state.
    """
    del session  # reserved; see docstring

    checker = multi_region_checker or _safe_get_checker()
    if checker is None:
        return None

    try:
        capacities = checker.get_all_regions_capacity()
    except Exception as exc:  # noqa: BLE001
        logger.debug("environment context gather: get_all_regions_capacity failed: %s", exc)
        return None

    if not capacities:
        # No deployed regions discovered — return an empty shape rather
        # than ``None`` so the operator can see the gather did run.
        return {
            "regions": [],
            "cluster_metrics": {},
            "reservations": _summarise_reservations(checker, []),
        }

    cluster_metrics: dict[str, dict[str, Any]] = {}
    for cap in capacities:
        cluster_metrics[cap.region] = {
            "queue_depth": int(cap.queue_depth),
            "running_jobs": int(cap.running_jobs),
            "pending_jobs": int(cap.pending_jobs),
            "gpu_utilization": float(cap.gpu_utilization),
            "cpu_utilization": float(cap.cpu_utilization),
            "recommendation_score": float(cap.recommendation_score),
        }

    sorted_regions = sorted(cluster_metrics.keys())
    return {
        "regions": sorted_regions,
        "cluster_metrics": cluster_metrics,
        "reservations": _summarise_reservations(checker, sorted_regions),
    }
