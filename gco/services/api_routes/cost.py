"""Cost reporting endpoints — the authenticated /api/v1/cost/* surface.

The manifest API is the cluster's authenticated ingress (HMAC middleware +
IAM-authorized API Gateway in front), so cost reporting is exposed here and
proxied to the internal ``cost-monitor`` ClusterIP service, which owns the
OpenCost queries and the S3 report pipeline. Keeping the cost-monitor
unexposed preserves its single-writer isolation while giving operators one
API host for every control-plane call:

- ``GET  /api/v1/cost/status``  — service + OpenCost health for this region.
- ``GET  /api/v1/cost/reports`` — recent scheduled/ad-hoc report objects.
- ``POST /api/v1/cost/reports`` — generate an ad-hoc report now.

When cost monitoring is disabled the cost-monitor Deployment does not exist,
so the proxy maps connection failures to a clear 503.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/cost", tags=["Cost"])
logger = logging.getLogger(__name__)

_DEFAULT_COST_MONITOR_URL = "http://cost-monitor.gco-system.svc.cluster.local"
_PROXY_TIMEOUT_SECONDS = 30.0
_REPORT_TIMEOUT_SECONDS = 120.0

_DISABLED_DETAIL = (
    "Cost monitoring is unavailable in this region. Enable cost_monitoring "
    "(and cluster_observability) in cdk.json and redeploy, or check the "
    "cost-monitor Deployment in gco-system."
)


class CostReportRequest(BaseModel):
    """Request body for POST /api/v1/cost/reports."""

    window_hours: int = Field(
        24,
        ge=1,
        le=168,
        description="Trailing window the ad-hoc report covers, in hours",
    )
    include_rows: bool = Field(
        False,
        description="Include the normalized allocation rows in the response",
    )


def _cost_monitor_base_url() -> str:
    return os.getenv("COST_MONITOR_URL", _DEFAULT_COST_MONITOR_URL).rstrip("/")


async def _proxy_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{_cost_monitor_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        logger.warning("Cost monitor unreachable at %s: %s", url, exc)
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL) from exc
    return _relay_json(response)


def _relay_json(response: httpx.Response) -> dict[str, Any]:
    """Return the cost-monitor JSON body, propagating its error statuses."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="Cost monitor returned a non-JSON body"
        ) from exc
    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=response.status_code,
            detail=str(detail or "Cost monitor request failed"),
        )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Cost monitor returned a non-object body")
    return payload


@router.get("/status")
async def get_cost_status() -> Response:
    """Cost monitoring status for this region, including OpenCost health."""
    payload = await _proxy_get("/internal/status", {})
    return JSONResponse(status_code=200, content=payload)


@router.get("/reports")
async def list_cost_reports(
    adhoc: bool = Query(False, description="List ad-hoc instead of scheduled reports"),
    limit: int = Query(50, ge=1, le=1000, description="Maximum objects returned"),
) -> Response:
    """List this region's most recent cost report objects in S3."""
    payload = await _proxy_get("/internal/reports", {"adhoc": str(adhoc).lower(), "limit": limit})
    return JSONResponse(status_code=200, content=payload)


@router.post("/reports")
async def generate_cost_report(request: CostReportRequest) -> Response:
    """Generate an ad-hoc cost report for the trailing window."""
    url = f"{_cost_monitor_base_url()}/internal/reports"
    try:
        async with httpx.AsyncClient(timeout=_REPORT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json={
                    "window_hours": request.window_hours,
                    "include_rows": request.include_rows,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Cost monitor unreachable at %s: %s", url, exc)
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL) from exc
    payload = _relay_json(response)
    payload.setdefault("timestamp", datetime.now(UTC).isoformat())
    return JSONResponse(status_code=201, content=payload)
