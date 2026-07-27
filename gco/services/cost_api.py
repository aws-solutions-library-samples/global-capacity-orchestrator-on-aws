"""Cost Monitor HTTP service.

Runs inside the ``cost-monitor`` Deployment (gco-system) and serves:

- ``/healthz`` / ``/readyz`` — Kubernetes probes.
- ``/metrics`` — Prometheus metrics for the in-cluster scrape.
- ``/internal/status`` — service + OpenCost health, including the live
  "returning data" probe release validation gates on.
- ``GET /internal/reports`` — recent report objects for this region.
- ``POST /internal/reports`` — ad-hoc report generation.

The service is cluster-internal (ClusterIP, default-deny ingress except the
manifest processor): the *authenticated* public surface is the manifest API's
``/api/v1/cost/*`` router, which proxies here. A background task writes the
scheduled interval reports.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from gco.services.cost_monitor import (
    CostMonitor,
    OpenCostUnavailableError,
    ReportWriteError,
    create_cost_monitor_from_env,
)
from gco.services.structured_logging import configure_structured_logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

#: Populated by the lifespan handler; read by the route handlers.
cost_monitor: CostMonitor | None = None

_SCHEDULER_TICK_SECONDS = 60.0


class AdhocReportRequest(BaseModel):
    """Request body for POST /internal/reports."""

    window_hours: int = Field(
        24,
        ge=1,
        le=168,
        description="Trailing window the report covers, in hours",
    )
    include_rows: bool = Field(
        False,
        description="Include the normalized allocation rows in the response",
    )


def _check_monitor() -> CostMonitor:
    if cost_monitor is None:
        raise HTTPException(status_code=503, detail="Cost monitor not initialized")
    return cost_monitor


async def _scheduled_report_loop(monitor: CostMonitor, stop: asyncio.Event) -> None:
    """Write the aligned interval report; failures retry on the next tick."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(monitor.run_scheduled_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive any pass failure
            logger.warning("Scheduled cost report pass failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SCHEDULER_TICK_SECONDS)
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize the monitor and run the scheduled reporter until shutdown."""
    global cost_monitor

    logger.info("Starting Cost Monitor Service")
    cost_monitor = create_cost_monitor_from_env()
    configure_structured_logging(
        service_name="cost-monitor",
        cluster_id=cost_monitor.cluster,
        region=cost_monitor.region,
    )
    stop = asyncio.Event()
    loop_task = asyncio.create_task(
        _scheduled_report_loop(cost_monitor, stop),
        name="cost-monitor-scheduled-reports",
    )
    app.state.scheduled_report_task = loop_task
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(loop_task, timeout=30)
        except TimeoutError:
            loop_task.cancel()
        logger.info("Shutting down Cost Monitor Service")


app = FastAPI(
    title="GCO Cost Monitor",
    description="Scheduled and on-demand OpenCost allocation reporting",
    version="1.0.0",
    lifespan=lifespan,
)

from gco.services.service_metrics import mount_metrics  # noqa: E402

mount_metrics(app, "cost-monitor")


@app.get("/healthz", tags=["Health"])
async def kubernetes_health_check() -> dict[str, str]:
    """Kubernetes-style liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", tags=["Health"])
async def kubernetes_readiness_check() -> dict[str, str]:
    """Readiness requires the monitor plus a live scheduled-report task."""
    if cost_monitor is None:
        raise HTTPException(status_code=503, detail="Cost monitor not ready")
    task = getattr(app.state, "scheduled_report_task", None)
    if task is not None and task.done():
        raise HTTPException(status_code=503, detail="Scheduled reporter stopped unexpectedly")
    return {"status": "ready"}


@app.get("/internal/status", tags=["Cost"])
async def get_status() -> dict[str, Any]:
    """Service status including OpenCost health and the data-returning probe."""
    monitor = _check_monitor()
    return await asyncio.to_thread(monitor.status)


@app.get("/internal/reports", tags=["Cost"])
async def list_reports(
    adhoc: bool = Query(False, description="List ad-hoc instead of scheduled reports"),
    limit: int = Query(50, ge=1, le=1000, description="Maximum objects returned"),
) -> dict[str, Any]:
    """List this region's most recent report objects, newest first."""
    monitor = _check_monitor()
    try:
        reports = await asyncio.to_thread(monitor.list_reports, adhoc=adhoc, limit=limit)
    except Exception as exc:  # noqa: BLE001 - surface S3 failures as 502
        raise HTTPException(status_code=502, detail=f"Failed to list reports: {exc}") from exc
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "region": monitor.region,
        "bucket": monitor.bucket,
        "count": len(reports),
        "reports": reports,
    }


@app.post("/internal/reports", tags=["Cost"], status_code=201)
async def generate_adhoc_report(request: AdhocReportRequest) -> dict[str, Any]:
    """Generate one ad-hoc allocation report for the trailing window."""
    monitor = _check_monitor()
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=request.window_hours)
    try:
        result = await asyncio.to_thread(
            monitor.generate_report,
            window_start,
            window_end,
            adhoc=True,
            include_rows=request.include_rows,
        )
    except OpenCostUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReportWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "region": monitor.region,
        "bucket": monitor.bucket,
        "report": result.summary(),
    }
    if request.include_rows:
        body["rows"] = result.rows
    return body


def create_app() -> FastAPI:
    """Factory function to create the FastAPI app."""
    return app


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")  # nosec B104 — must bind all interfaces inside K8s pod
    port = int(os.getenv("PORT", "8080"))
    logger.info("Starting Cost Monitor API on %s:%d", host, port)
    uvicorn.run(
        "gco.services.cost_api:app",
        host=host,
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        reload=False,
    )
