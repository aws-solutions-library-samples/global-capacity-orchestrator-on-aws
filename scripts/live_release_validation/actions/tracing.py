"""tracing: the deployed API services' OpenTelemetry spans reach Transaction Search."""

from __future__ import annotations

from typing import Any

from ..checks.tracing import effective_tracing_config, verify_region_tracing
from ..models import RunContext


def action_tracing(ctx: RunContext) -> dict[str, Any]:
    """Require every traced GCO service's spans in each Region's Transaction Search.

    For every deployed Region: the X-Ray trace segment destination must be
    ``CloudWatchLogs`` and ``ACTIVE``; authenticated API traffic then reaches
    the health monitor, the manifest processor, and (with cost monitoring on)
    ``/api/v1/cost/status``, and a bounded CloudWatch Logs Insights poll of
    the ``aws/spans`` log group must find spans for each expected
    ``service.name`` plus at least one trace in which a cost-monitor span
    joins the manifest-processor trace that called it. The inference proxy is
    expected where this run's ``inference`` action sent requests through it and
    is skipped with the reason elsewhere. Evidence is counts only.

    With ``tracing.enabled: false`` in cdk.json nothing exports spans, and the
    action passes with a note.
    """
    config = effective_tracing_config(ctx)
    if not config["enabled"]:
        return {
            "tracing": config,
            "note": "tracing.enabled is false in cdk.json; no service exports spans",
            "regions": {},
        }
    return {
        "tracing": config,
        "regions": {
            region: verify_region_tracing(ctx, region, config) for region in ctx.deployment_regions
        },
    }
