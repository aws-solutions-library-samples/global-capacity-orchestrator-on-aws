"""opencost: cost monitoring health and report-pipeline validation."""

from __future__ import annotations

from typing import Any

from ..checks.opencost import (
    _cost_monitoring_configured,
    _generate_validation_report,
    _verify_report_object,
    _wait_for_opencost_data,
)
from ..models import RunContext


def action_opencost(ctx: RunContext) -> dict[str, Any]:
    """Require healthy, data-returning OpenCost and a working report pipeline.

    For every deployed Region: poll ``/api/v1/cost/status`` until OpenCost is
    healthy *and* returning allocation data (bounded), then generate an
    ad-hoc report through ``/api/v1/cost/reports`` and confirm the Parquet
    object exists in the central cost report bucket. Any Region with an
    unhealthy or empty OpenCost fails validation.

    When the checked-in ``cdk.json`` disables cost monitoring (or cluster
    observability, its data source), the action records that configuration
    and passes — validation proves the configured topology, and an opted-out
    pipeline has nothing to prove.
    """
    if not _cost_monitoring_configured(ctx):
        return {
            "cost_monitoring_enabled": False,
            "detail": (
                "cost_monitoring (or cluster_observability) is disabled in "
                "cdk.json; no OpenCost surface is deployed to validate"
            ),
        }

    regions: dict[str, Any] = {}
    for region in ctx.deployment_regions:
        status = _wait_for_opencost_data(ctx, region)
        report = _generate_validation_report(ctx, region)
        object_evidence = _verify_report_object(ctx, report)
        regions[region] = {
            "opencost_healthy": bool(status.get("opencost_healthy")),
            "opencost_returning_data": bool(status.get("opencost_returning_data")),
            "allocation_names": status.get("allocation_names"),
            "report": report,
            "s3_object": object_evidence,
        }

    evidence = {
        "cost_monitoring_enabled": True,
        "regions": regions,
    }
    with ctx.state_lock:
        ctx.checkpoint.state["opencost"] = evidence
        ctx.persist_callback(ctx.checkpoint)
    return evidence
