"""Cost monitoring (OpenCost) health, data, and report-pipeline checks.

Validates through the same authenticated API surface operators use: each
Region's ``/api/v1/cost/status`` must report a healthy OpenCost that is
returning allocation data, and an ad-hoc ``/api/v1/cost/reports`` request
must produce a Parquet object that is then confirmed present in the central
cost report bucket. Data readiness is polled with a bounded deadline because
a freshly-deployed Prometheus needs a few scrape cycles before OpenCost can
answer with non-empty allocations.
"""

from __future__ import annotations

import time
from typing import Any

from ..checks.jobs import _response_json
from ..context import _job_transport_region
from ..models import RunContext

#: Ceiling for the OpenCost data-readiness poll. A fresh deploy needs
#: Prometheus up, OpenCost scraped, and at least one allocation window
#: resolvable; measured cold-start readiness sits well inside this bound.
_OPENCOST_READY_TIMEOUT_SECONDS = 1_200

#: Trailing window requested for the validation ad-hoc report.
_VALIDATION_REPORT_WINDOW_HOURS = 1


def _cost_monitoring_configured(ctx: RunContext) -> bool:
    """Return whether the checked-in cdk.json enables the cost pipeline."""
    cost_block = ctx.cdk_context.get("cost_monitoring")
    cost_enabled = True
    if isinstance(cost_block, dict) and "enabled" in cost_block:
        cost_enabled = bool(cost_block["enabled"])
    observability_block = ctx.cdk_context.get("cluster_observability")
    observability_enabled = True
    if isinstance(observability_block, dict) and "enabled" in observability_block:
        observability_enabled = bool(observability_block["enabled"])
    return cost_enabled and observability_enabled


def _monitoring_region(ctx: RunContext) -> str:
    regions = ctx.cdk_context.get("deployment_regions") or {}
    monitoring = regions.get("monitoring") if isinstance(regions, dict) else None
    return str(monitoring or ctx.config.global_region)


def _get_cost_status(ctx: RunContext, region: str) -> dict[str, Any]:
    """Fetch one Region's /api/v1/cost/status through its authorized transport."""
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path="/api/v1/cost/status",
        target_region=_job_transport_region(ctx, region),
    )
    if not response.ok:
        raise RuntimeError(
            f"Cost status for {region} failed: {response.status_code} {response.text}"
        )
    status = _response_json(response, f"Cost status for {region}")
    observed_region = str(status.get("region") or "")
    if observed_region and observed_region != region:
        raise RuntimeError(
            f"Cost status transport returned Region {observed_region!r}; expected {region!r}"
        )
    return status


def _wait_for_opencost_data(ctx: RunContext, region: str) -> dict[str, Any]:
    """Poll until OpenCost is healthy and returning allocation data.

    Fails the action when the bounded deadline passes with OpenCost either
    unhealthy or answering with empty allocations — both mean the deployed
    cost pipeline cannot produce trustworthy reports.
    """
    deadline = time.monotonic() + _OPENCOST_READY_TIMEOUT_SECONDS
    last_status: dict[str, Any] = {}
    while True:
        last_status = _get_cost_status(ctx, region)
        if bool(last_status.get("opencost_healthy")) and bool(
            last_status.get("opencost_returning_data")
        ):
            return last_status
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"OpenCost in {region} did not become healthy with allocation data "
                f"within {_OPENCOST_READY_TIMEOUT_SECONDS}s: "
                f"healthy={last_status.get('opencost_healthy')} "
                f"returning_data={last_status.get('opencost_returning_data')} "
                f"last_error={last_status.get('last_error')}"
            )
        time.sleep(ctx.settings.poll_interval_seconds)


def _generate_validation_report(ctx: RunContext, region: str) -> dict[str, Any]:
    """Request one ad-hoc report and require row-bearing Parquet evidence."""
    response = ctx.aws_client.make_authenticated_request(
        method="POST",
        path="/api/v1/cost/reports",
        body={"window_hours": _VALIDATION_REPORT_WINDOW_HOURS, "include_rows": False},
        target_region=_job_transport_region(ctx, region),
    )
    if response.status_code not in {200, 201}:
        raise RuntimeError(
            f"Ad-hoc cost report for {region} failed: {response.status_code} {response.text}"
        )
    payload = _response_json(response, f"Ad-hoc cost report for {region}")
    report = payload.get("report")
    if not isinstance(report, dict) or not report.get("s3_key"):
        raise RuntimeError(f"Ad-hoc cost report for {region} omitted its S3 key")
    if int(report.get("row_count") or 0) <= 0:
        raise RuntimeError(f"Ad-hoc cost report for {region} contained zero allocation rows")
    bucket = str(payload.get("bucket") or "")
    if not bucket:
        raise RuntimeError(f"Ad-hoc cost report for {region} omitted its bucket")
    return {"bucket": bucket, **report}


def _verify_report_object(ctx: RunContext, report: dict[str, Any]) -> dict[str, Any]:
    """Confirm the reported Parquet object actually exists in S3."""
    s3 = ctx.session.client("s3", region_name=_monitoring_region(ctx))
    key = str(report["s3_key"])
    bucket = str(report["bucket"])
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - absence and access failures both fail validation
        raise RuntimeError(
            f"Cost report object s3://{bucket}/{key} is not readable: {exc}"
        ) from exc
    size = int(head.get("ContentLength") or 0)
    if size <= 0:
        raise RuntimeError(f"Cost report object s3://{bucket}/{key} is empty")
    return {"bucket": bucket, "key": key, "size_bytes": size}
