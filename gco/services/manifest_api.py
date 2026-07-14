"""
Manifest API Service for GCO (Global Capacity Orchestrator on AWS).

This FastAPI service provides REST endpoints for Kubernetes manifest
submission, validation, and management. Endpoint implementations live
in the ``api_routes`` sub-package; this module wires them together and
owns the application lifecycle, Pydantic request/response models, and
health probes.

See ``api_routes/`` for the individual routers:
    - manifests.py  — manifest submit / validate / resource CRUD
    - jobs.py       — job list / get / logs / events / metrics / delete / retry
    - templates.py  — job template CRUD + create-from-template
    - webhooks.py   — webhook registration
    - queue.py      — DynamoDB-backed global job queue
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from gco.services.auth_middleware import AuthenticationMiddleware
from gco.services.manifest_processor import (
    ManifestProcessor,
    create_manifest_processor_from_env,
)
from gco.services.metrics_publisher import ManifestProcessorMetrics
from gco.services.structured_logging import configure_structured_logging
from gco.services.template_store import (
    JobStore,
    TemplateStore,
    WebhookStore,
    get_job_store,
    get_template_store,
    get_webhook_store,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request Size Limit Middleware
# ---------------------------------------------------------------------------

# Default max request body size: 1MB
DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce request body size limits.

    Rejects requests that exceed the configured maximum body size with HTTP 413
    (Payload Too Large). Checks the Content-Length header first for an early
    rejection without reading the body. For requests without Content-Length,
    reads up to limit + 1 byte and rejects if exceeded.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES) -> None:
        super().__init__(app)
        self.max_body_bytes = max_body_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Skip size checks for GET, HEAD, OPTIONS, DELETE (typically no body)
        if request.method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return await call_next(request)

        # Check Content-Length header for early rejection
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request body exceeds maximum size of {self.max_body_bytes} bytes"
                        },
                    )
            except ValueError, TypeError:
                # Invalid Content-Length header — let the request proceed
                # and let downstream validation handle it
                pass

        # For requests without Content-Length (chunked transfer), read up to
        # limit + 1 byte to detect oversized bodies
        if not content_length:
            body = await request.body()
            if len(body) > self.max_body_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": f"Request body exceeds maximum size of {self.max_body_bytes} bytes"
                    },
                )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Global state — populated by lifespan, read by routers via this module.
# ---------------------------------------------------------------------------
manifest_processor: ManifestProcessor | None = None
manifest_metrics: ManifestProcessorMetrics | None = None
template_store: TemplateStore | None = None
webhook_store: WebhookStore | None = None
job_store: JobStore | None = None


# =============================================================================
# Pydantic Models for API
# =============================================================================


# =============================================================================
# Application Lifecycle
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager — initializes manifest processor and stores."""
    global manifest_processor, manifest_metrics, template_store, webhook_store, job_store

    logger.info("Starting Manifest API Service")
    try:
        manifest_processor = create_manifest_processor_from_env()

        configure_structured_logging(
            service_name="manifest-api",
            cluster_id=manifest_processor.cluster_id,
            region=manifest_processor.region,
        )

        manifest_metrics = ManifestProcessorMetrics(
            cluster_name=manifest_processor.cluster_id,
            region=manifest_processor.region,
        )
        logger.info("Manifest processor initialized")

        template_store = get_template_store()
        webhook_store = get_webhook_store()
        job_store = get_job_store()
        logger.info("DynamoDB stores initialized")
    except Exception as e:
        logger.error(f"Failed to initialize manifest processor: {e}")
        raise

    yield

    logger.info("Shutting down Manifest API Service")


# =============================================================================
# Create FastAPI app and include routers
# =============================================================================

app = FastAPI(
    title="GCO Manifest Processor API",
    description="Kubernetes manifest submission and management service for GCO (Global Capacity Orchestrator on AWS)",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(AuthenticationMiddleware)

# Request size limit middleware — added after auth middleware so it executes
# first in the request pipeline (Starlette processes middleware in LIFO order).
_max_body_bytes = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES)))
app.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=_max_body_bytes)

# Expose Prometheus /metrics for the in-cluster observability scrape. The auth
# middleware exempts /metrics, so the cluster Prometheus reaches it over the
# existing service port without credentials.
from gco.services.service_metrics import mount_metrics  # noqa: E402

mount_metrics(app, "manifest-processor")

# Include domain routers
from gco.services.api_routes.jobs import router as jobs_router  # noqa: E402
from gco.services.api_routes.manifests import router as manifests_router  # noqa: E402
from gco.services.api_routes.queue import router as queue_router  # noqa: E402
from gco.services.api_routes.templates import router as templates_router  # noqa: E402
from gco.services.api_routes.webhooks import router as webhooks_router  # noqa: E402

app.include_router(manifests_router)
app.include_router(jobs_router)
app.include_router(templates_router)
app.include_router(webhooks_router)
app.include_router(queue_router)


# =============================================================================
# Root & Health Endpoints (kept here — they're thin and tightly coupled to state)
# =============================================================================


@app.get("/", tags=["Info"])
async def root() -> dict[str, Any]:
    """Root endpoint with basic service information and API overview."""
    return {
        "service": "GCO Manifest Processor API",
        "version": "2.0.0",
        "status": "running",
        "cluster_id": (manifest_processor.cluster_id if manifest_processor else "unknown"),
        "region": (manifest_processor.region if manifest_processor else "unknown"),
        "endpoints": {
            "manifests": {
                "submit": "POST /api/v1/manifests",
                "validate": "POST /api/v1/manifests/validate",
                "get": "GET /api/v1/manifests/{namespace}/{name}",
                "delete": "DELETE /api/v1/manifests/{namespace}/{name}",
            },
            "jobs": {
                "list": "GET /api/v1/jobs",
                "get": "GET /api/v1/jobs/{namespace}/{name}",
                "logs": "GET /api/v1/jobs/{namespace}/{name}/logs",
                "events": "GET /api/v1/jobs/{namespace}/{name}/events",
                "pods": "GET /api/v1/jobs/{namespace}/{name}/pods",
                "metrics": "GET /api/v1/jobs/{namespace}/{name}/metrics",
                "delete": "DELETE /api/v1/jobs/{namespace}/{name}",
                "bulk_delete": "DELETE /api/v1/jobs",
                "retry": "POST /api/v1/jobs/{namespace}/{name}/retry",
            },
            "templates": {
                "list": "GET /api/v1/templates",
                "create": "POST /api/v1/templates",
                "get": "GET /api/v1/templates/{name}",
                "delete": "DELETE /api/v1/templates/{name}",
                "create_job": "POST /api/v1/jobs/from-template/{name}",
            },
            "webhooks": {
                "list": "GET /api/v1/webhooks",
                "create": "POST /api/v1/webhooks",
                "delete": "DELETE /api/v1/webhooks/{id}",
            },
            "health": "GET /api/v1/health",
            "status": "GET /api/v1/status",
        },
    }


@app.get("/healthz", tags=["Health"])
async def kubernetes_health_check() -> dict[str, str]:
    """Kubernetes-style liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", tags=["Health"])
async def kubernetes_readiness_check() -> dict[str, str]:
    """Kubernetes-style readiness probe."""
    if manifest_processor is None:
        raise HTTPException(status_code=503, detail="Manifest processor not ready")
    return {"status": "ready"}


@app.get("/api/v1/health", tags=["Health"])
async def health_check() -> JSONResponse:
    """Health check endpoint for load balancer health checks."""
    try:
        if manifest_processor is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": "Manifest processor not initialized",
                },
            )

        try:
            manifest_processor.core_v1.list_namespace(limit=1)
            api_healthy = True
        except Exception as e:
            logger.error(f"Kubernetes API health check failed: {e}")
            api_healthy = False

        status_code = 200 if api_healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "healthy" if api_healthy else "unhealthy",
                "timestamp": datetime.now(UTC).isoformat(),
                "cluster_id": manifest_processor.cluster_id,
                "region": manifest_processor.region,
                "kubernetes_api": "connected" if api_healthy else "disconnected",
            },
        )

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": datetime.now(UTC).isoformat(),
                "error": "manifest processor unavailable",
            },
        )


@app.get("/api/v1/status", tags=["Health"])
async def get_service_status() -> dict[str, Any]:
    """Service status endpoint with detailed information."""
    templates_count = 0
    webhooks_count = 0
    try:
        if template_store:
            templates_count = len(template_store.list_templates())
        if webhook_store:
            webhooks_count = len(webhook_store.list_webhooks())
    except Exception as e:
        logger.warning(f"Failed to get store counts: {e}")

    status_info: dict[str, Any] = {
        "service": "GCO Manifest Processor API",
        "version": "2.0.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "manifest_processor_initialized": manifest_processor is not None,
        "environment": {
            "cluster_name": os.getenv("CLUSTER_NAME", "unknown"),
            "region": os.getenv("REGION", "unknown"),
            "max_cpu_per_manifest": os.getenv("MAX_CPU_PER_MANIFEST", "10"),
            "max_memory_per_manifest": os.getenv("MAX_MEMORY_PER_MANIFEST", "32Gi"),
            "max_gpu_per_manifest": os.getenv("MAX_GPU_PER_MANIFEST", "4"),
            "allowed_namespaces": os.getenv("ALLOWED_NAMESPACES", "default,gco-jobs"),
            "validation_enabled": os.getenv("VALIDATION_ENABLED", "true"),
        },
        "templates_count": templates_count,
        "webhooks_count": webhooks_count,
    }

    if manifest_processor:
        status_info.update(
            {
                "cluster_id": manifest_processor.cluster_id,
                "region": manifest_processor.region,
                "resource_limits": {
                    "max_cpu_millicores": manifest_processor.max_cpu_per_manifest,
                    "max_memory_bytes": manifest_processor.max_memory_per_manifest,
                    "max_gpu_count": manifest_processor.max_gpu_per_manifest,
                },
                "allowed_namespaces": list(manifest_processor.allowed_namespaces),
                "validation_enabled": manifest_processor.validation_enabled,
            }
        )

    return status_info


# =============================================================================
# Error Handlers
# =============================================================================


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for unhandled errors."""
    logger.error(f"Unhandled exception in {request.method} {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else "An unexpected error occurred",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


# =============================================================================
# App Factory & Entrypoint
# =============================================================================


def create_app() -> FastAPI:
    """Factory function to create the FastAPI app."""
    return app


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")  # nosec B104 — must bind all interfaces inside K8s pod
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    logger.info(f"Starting Manifest API on {host}:{port}")

    uvicorn.run(
        "gco.services.manifest_api:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=False,
    )
