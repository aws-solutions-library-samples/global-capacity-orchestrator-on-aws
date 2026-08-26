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

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from gco.services.auth_middleware import AuthenticationMiddleware
from gco.services.central_queue_worker import CentralQueueWorker
from gco.services.manifest_processor import (
    ManifestProcessor,
    create_manifest_processor_from_env,
)
from gco.services.metrics_publisher import ManifestProcessorMetrics
from gco.services.request_size_middleware import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    RequestSizeLimitMiddleware,
)
from gco.services.structured_logging import configure_structured_logging
from gco.services.template_store import (
    JobStore,
    TemplateStore,
    WebhookStore,
    get_job_store,
    get_template_store,
    get_webhook_store,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-14T03:46:22Z
# Flowchart(s) generated from this file:
#   * ``lifespan`` -> ``diagrams/code_diagrams/gco/services/manifest_api.lifespan.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/manifest_api.lifespan.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Global state — populated by lifespan, read by routers via this module.
# ---------------------------------------------------------------------------
manifest_processor: ManifestProcessor | None = None
manifest_metrics: ManifestProcessorMetrics | None = None
template_store: TemplateStore | None = None
webhook_store: WebhookStore | None = None
job_store: JobStore | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse an explicit deployment boolean without truthy-string surprises."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_number(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a finite bounded worker setting from the environment."""
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


# =============================================================================
# Pydantic Models for API
# =============================================================================


# =============================================================================
# Application Lifecycle
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize API dependencies and the optional regional queue worker."""
    global manifest_processor, manifest_metrics, template_store, webhook_store, job_store

    queue_worker: CentralQueueWorker | None = None
    queue_worker_task: asyncio.Task[None] | None = None
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

        if _env_bool("CENTRAL_QUEUE_WORKER_ENABLED"):
            queue_worker = CentralQueueWorker(
                processor=manifest_processor,
                store=job_store,
                poll_interval_seconds=_env_number(
                    "CENTRAL_QUEUE_POLL_INTERVAL_SECONDS", 10.0, 1.0, 300.0
                ),
                batch_size=int(_env_number("CENTRAL_QUEUE_BATCH_SIZE", 5.0, 1.0, 20.0)),
                reconcile_limit=int(
                    _env_number("CENTRAL_QUEUE_RECONCILE_LIMIT", 100.0, 1.0, 500.0)
                ),
                lease_renewal_seconds=_env_number(
                    "CENTRAL_QUEUE_LEASE_RENEWAL_SECONDS", 60.0, 1.0, 300.0
                ),
            )
            queue_worker_task = asyncio.create_task(
                queue_worker.run(),
                name=f"central-queue-worker-{manifest_processor.region}",
            )
            app.state.central_queue_worker = queue_worker
            app.state.central_queue_worker_task = queue_worker_task
        else:
            app.state.central_queue_worker = None
            app.state.central_queue_worker_task = None
    except Exception as e:
        logger.error(f"Failed to initialize manifest processor: {e}")
        raise

    try:
        yield
    finally:
        if queue_worker is not None and queue_worker_task is not None:
            queue_worker.stop()
            try:
                await asyncio.wait_for(queue_worker_task, timeout=30)
            except TimeoutError:
                queue_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await queue_worker_task
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
from gco.services.api_routes.cost import router as cost_router  # noqa: E402
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
app.include_router(cost_router)


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
            "cost": {
                "status": "GET /api/v1/cost/status",
                "reports": "GET /api/v1/cost/reports",
                "generate_report": "POST /api/v1/cost/reports",
            },
            "health": "GET /api/v1/health",
            "status": "GET /api/v1/status",
            "policy": "GET /api/v1/policy",
        },
    }


@app.get("/healthz", tags=["Health"])
async def kubernetes_health_check() -> dict[str, str]:
    """Kubernetes-style liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", tags=["Health"])
async def kubernetes_readiness_check() -> dict[str, str]:
    """Kubernetes readiness includes the enabled queue worker task."""
    if manifest_processor is None:
        raise HTTPException(status_code=503, detail="Manifest processor not ready")
    worker_task = getattr(app.state, "central_queue_worker_task", None)
    if worker_task is not None and worker_task.done():
        raise HTTPException(status_code=503, detail="Central queue worker stopped unexpectedly")
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
            "allowed_namespaces": os.getenv("ALLOWED_NAMESPACES", "gco-jobs"),
            "validation_enabled": os.getenv("VALIDATION_ENABLED", "true"),
        },
        "templates_count": templates_count,
        "webhooks_count": webhooks_count,
        "central_queue_worker": (
            worker.health()
            if (worker := getattr(app.state, "central_queue_worker", None)) is not None
            else {"enabled": False, "running": False}
        ),
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


@app.get("/api/v1/policy", tags=["Health"])
async def get_job_validation_policy() -> dict[str, Any]:
    """The validation policy this region actually enforces, as deployed.

    Answers "will this cluster admit the job I am about to pay to run?"
    before submission, so a policy conflict surfaces at plan time instead of
    after a region has been provisioned and billed.

    Reads the live ``ManifestProcessor`` instance rather than any config file.
    A local ``cdk.json`` is the *input* to a deploy, not the state of one:
    the cluster may have been deployed from a different checkout, and CDK
    augments ``trusted_registries`` with the project's own ECR hostnames at
    synth time, so the effective allowlist is strictly larger than the
    configured one.

    Three layers govern admission and all three are reported:

    1. ``policy`` — the front-door checks the manifest processor and the SQS
       queue processor both apply (they read the same env vars, so neither
       submission path is a bypass).
    2. ``cluster_enforcement.limit_ranges`` — per-container ceilings.
    3. ``cluster_enforcement.resource_quotas`` — namespace aggregate ceilings.

    A manifest must clear all three. Layers 2 and 3 are read live from the
    Kubernetes API and degrade to ``status="unavailable"`` rather than
    failing the whole response.
    """
    if manifest_processor is None:
        raise HTTPException(status_code=503, detail="Manifest processor not ready")

    return {
        "service": "GCO Manifest Processor API",
        "timestamp": datetime.now(UTC).isoformat(),
        "cluster_id": manifest_processor.cluster_id,
        "region": manifest_processor.region,
        # Names the origin of these values so a caller never mistakes this for
        # a config-file read.
        "source": "deployed-cluster-runtime",
        "policy": manifest_processor.effective_job_validation_policy(),
        "cluster_enforcement": manifest_processor.cluster_resource_governance(),
    }


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
