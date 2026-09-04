"""
Health API Service for GCO (Global Capacity Orchestrator on AWS).

This FastAPI service exposes health status endpoints for:
- ALB health checks (/healthz, /readyz)
- Detailed health status (/api/v1/health)
- Resource utilization metrics (/api/v1/metrics)
- Service status information (/api/v1/status)

The service runs a background task that continuously monitors cluster health
and caches the results for fast response times on health check endpoints.

Endpoints:
    GET /healthz          - Kubernetes liveness probe (always 200 if running)
    GET /readyz           - Kubernetes readiness probe (200 if health monitor ready)
    GET /api/v1/health    - Detailed health status (200 if healthy, 503 if not)
    GET /api/v1/metrics   - Resource utilization metrics
    GET /api/v1/status    - Service operational status

Environment Variables:
    HOST: Bind address (default: 0.0.0.0)
    PORT: Listen port (default: 8080)
    LOG_LEVEL: Logging level (default: info)
    CLUSTER_NAME, REGION, *_THRESHOLD: See health_monitor.py
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from gco.models import HealthStatus
from gco.services.auth_middleware import AuthenticationMiddleware
from gco.services.health_monitor import HealthMonitor, create_health_monitor_from_env
from gco.services.metrics_publisher import HealthMonitorMetrics
from gco.services.structured_logging import configure_structured_logging
from gco.services.webhook_dispatcher import (
    WebhookDispatcher,
    create_webhook_dispatcher_from_env,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global health monitor instance
health_monitor: HealthMonitor | None = None
health_metrics: HealthMonitorMetrics | None = None
webhook_dispatcher: WebhookDispatcher | None = None
current_health_status: HealthStatus | None = None
health_check_task = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan manager - starts and stops background health monitoring
    and webhook dispatcher.
    """
    global health_monitor, health_metrics, health_check_task, webhook_dispatcher

    # Startup
    logger.info("Starting Health API Service")
    try:
        health_monitor = create_health_monitor_from_env()

        # Enable structured JSON logging now that we know cluster_id and region
        configure_structured_logging(
            service_name="health-api",
            cluster_id=health_monitor.cluster_id,
            region=health_monitor.region,
        )
        # Initialize metrics publisher for CloudWatch custom metrics
        # Non-fatal: if credentials aren't available yet (e.g., Pod Identity agent
        # still starting), we skip metrics but keep serving health checks.
        try:
            health_metrics = HealthMonitorMetrics(
                cluster_name=health_monitor.cluster_id,
                region=health_monitor.region,
            )
        except Exception as e:
            logger.warning(f"Failed to initialize CloudWatch metrics publisher: {e}")
            health_metrics = None
        health_task = asyncio.create_task(background_health_monitor())
        health_check_task = health_task
        logger.info("Health monitoring started")

        # Start webhook dispatcher for job event notifications
        try:
            webhook_dispatcher = create_webhook_dispatcher_from_env()
            await webhook_dispatcher.start()
            logger.info("Webhook dispatcher started")
        except Exception as e:
            logger.warning(f"Failed to start webhook dispatcher: {e}")
            # Don't fail startup if webhook dispatcher fails - it's not critical
            webhook_dispatcher = None

    except Exception as e:
        logger.error(f"Failed to start health monitoring: {e}")
        raise

    try:
        yield
    finally:
        # Shutdown must also run when a lifespan consumer raises.
        logger.info("Shutting down Health API Service")
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        if webhook_dispatcher:
            await webhook_dispatcher.stop()
            logger.info("Webhook dispatcher stopped")
        logger.info("Health monitoring stopped")


# Create FastAPI app with lifespan management
app = FastAPI(
    title="GCO Health Monitor API",
    description="Health monitoring service for GCO (Global Capacity Orchestrator on AWS) EKS clusters",
    version="1.0.0",
    lifespan=lifespan,
)

# Add authentication middleware
app.add_middleware(AuthenticationMiddleware)

# Expose Prometheus /metrics for the in-cluster observability scrape. The auth
# middleware exempts /metrics, so the cluster Prometheus reaches it over the
# existing service port without credentials.
from gco.services.service_metrics import mount_metrics  # noqa: E402

mount_metrics(app, "health-monitor")


async def background_health_monitor() -> None:
    """
    Background task that continuously monitors cluster health
    and publishes metrics to CloudWatch
    """
    global current_health_status

    while True:
        try:
            if health_monitor is None:
                logger.warning("Health monitor not initialized, waiting...")
                await asyncio.sleep(10)
                continue
            current_health_status = await health_monitor.get_health_status()
            logger.debug(f"Health status updated: {current_health_status.status}")

            # Periodically sync ALB hostname in SSM (self-healing)
            await health_monitor.sync_alb_registration()

            # Publish metrics to CloudWatch for dashboard visibility
            if health_metrics and current_health_status:
                try:
                    health_metrics.publish_resource_utilization(
                        cpu_percent=current_health_status.resource_utilization.cpu,
                        memory_percent=current_health_status.resource_utilization.memory,
                        gpu_percent=current_health_status.resource_utilization.gpu,
                        active_jobs=current_health_status.active_jobs,
                    )
                    # Also publish health status
                    threshold_violations = (
                        current_health_status.get_threshold_violations()
                        if hasattr(current_health_status, "get_threshold_violations")
                        else []
                    )
                    health_metrics.publish_health_status(
                        is_healthy=(current_health_status.status == "healthy"),
                        threshold_violations=threshold_violations,
                    )
                    logger.debug("Published health metrics to CloudWatch")
                except Exception as e:
                    logger.warning(f"Failed to publish health metrics to CloudWatch: {e}")

            # Sleep for 30 seconds before next check
            await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Background health monitoring cancelled")
            break
        except Exception as e:
            logger.error(f"Error in background health monitoring: {e}")
            await asyncio.sleep(10)  # Shorter sleep on error


@app.get("/")
async def root() -> dict[str, Any]:
    """Root endpoint with basic service information"""
    return {
        "service": "GCO Health Monitor API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "health": "/api/v1/health",
            "metrics": "/api/v1/metrics",
            "status": "/api/v1/status",
        },
    }


@app.get("/api/v1/health")
async def health_check() -> JSONResponse:
    """
    Primary health check endpoint for ALB health checks
    Returns 200 if cluster is healthy, 503 if unhealthy
    """
    global current_health_status

    try:
        status = current_health_status
        # If we don't have a current status, get one immediately
        if status is None:
            if health_monitor is None:
                raise HTTPException(status_code=503, detail="Health monitor not initialized")
            status = await health_monitor.get_health_status()
            current_health_status = status

        # Check if status is too old (more than 2 minutes)
        age_seconds = (datetime.now() - status.timestamp).total_seconds()
        if age_seconds > 120 and health_monitor is not None:  # 2 minutes
            logger.warning(f"Health status is {age_seconds:.0f} seconds old, refreshing")
            status = await health_monitor.get_health_status()
            current_health_status = status

        # Return appropriate HTTP status based on health
        if status.status == "healthy":
            return JSONResponse(
                status_code=200,
                content={
                    "status": "healthy",
                    "timestamp": status.timestamp.isoformat(),
                    "cluster_id": status.cluster_id,
                    "region": status.region,
                },
            )
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": status.timestamp.isoformat(),
                "cluster_id": status.cluster_id,
                "region": status.region,
                "message": status.message,
            },
        )

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": datetime.now().isoformat(),
                "error": "health monitor unavailable",
            },
        )


@app.get("/api/v1/metrics")
async def get_metrics() -> dict[str, Any]:
    """
    Detailed metrics endpoint with resource utilization information
    """
    global current_health_status

    try:
        # Get fresh metrics if needed
        if current_health_status is None:
            if health_monitor is None:
                raise HTTPException(status_code=503, detail="Health monitor not initialized")
            current_health_status = await health_monitor.get_health_status()

        return {
            "cluster_id": current_health_status.cluster_id,
            "region": current_health_status.region,
            "timestamp": current_health_status.timestamp.isoformat(),
            "status": current_health_status.status,
            "resource_utilization": {
                "cpu_percent": round(current_health_status.resource_utilization.cpu, 2),
                "memory_percent": round(current_health_status.resource_utilization.memory, 2),
                "gpu_percent": round(current_health_status.resource_utilization.gpu, 2),
            },
            "thresholds": {
                "cpu_threshold": current_health_status.thresholds.cpu_threshold,
                "memory_threshold": current_health_status.thresholds.memory_threshold,
                "gpu_threshold": current_health_status.thresholds.gpu_threshold,
            },
            "active_jobs": current_health_status.active_jobs,
            "message": current_health_status.message,
            "threshold_violations": (
                current_health_status.get_threshold_violations()
                if hasattr(current_health_status, "get_threshold_violations")
                else []
            ),
        }

    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get metrics: {e!s}") from e


@app.get("/api/v1/status")
async def get_status() -> dict[str, Any]:
    """
    Service status endpoint with operational information
    """

    # Get webhook dispatcher metrics if available
    webhook_metrics = None
    if webhook_dispatcher:
        webhook_metrics = webhook_dispatcher.get_metrics()

    service_status = {
        "service": "GCO Health Monitor API",
        "version": "1.0.0",
        "uptime_seconds": None,  # Could be implemented with start time tracking
        "health_monitor_initialized": health_monitor is not None,
        "background_task_running": health_check_task is not None and not health_check_task.done(),
        "last_health_check": (
            current_health_status.timestamp.isoformat() if current_health_status else None
        ),
        "webhook_dispatcher": {
            "enabled": webhook_dispatcher is not None,
            "running": webhook_metrics.get("running", False) if webhook_metrics else False,
            "deliveries_total": (
                webhook_metrics.get("deliveries_total", 0) if webhook_metrics else 0
            ),
            "deliveries_success": (
                webhook_metrics.get("deliveries_success", 0) if webhook_metrics else 0
            ),
            "deliveries_failed": (
                webhook_metrics.get("deliveries_failed", 0) if webhook_metrics else 0
            ),
            "cached_jobs": webhook_metrics.get("cached_jobs", 0) if webhook_metrics else 0,
        },
        "environment": {
            "cluster_name": os.getenv("CLUSTER_NAME", "unknown"),
            "region": os.getenv("REGION", "unknown"),
            "cpu_threshold": os.getenv("CPU_THRESHOLD", "80"),
            "memory_threshold": os.getenv("MEMORY_THRESHOLD", "85"),
            "gpu_threshold": os.getenv("GPU_THRESHOLD", "90"),
        },
    }

    return service_status


@app.get("/healthz")
async def kubernetes_health_check() -> dict[str, str]:
    """
    Kubernetes-style health check endpoint
    Simple endpoint that returns 200 if the service is running
    """
    return {"status": "ok"}


@app.get("/readyz")
async def kubernetes_readiness_check() -> dict[str, str]:
    """
    Kubernetes-style readiness check endpoint
    Returns 200 if the service is ready to serve traffic
    """

    if health_monitor is None:
        raise HTTPException(status_code=503, detail="Health monitor not ready")

    return {"status": "ready"}


# Error handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for unhandled errors"""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else "An unexpected error occurred",
        },
    )


def create_app() -> FastAPI:
    """Factory function to create the FastAPI app"""
    return app


if __name__ == "__main__":
    import uvicorn

    # Configuration from environment variables
    host = os.getenv("HOST", "0.0.0.0")  # nosec B104 — must bind all interfaces inside K8s pod
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info(f"Starting Health API on {host}:{port}")

    uvicorn.run(
        "gco.services.health_api:app", host=host, port=port, log_level=log_level, reload=False
    )
