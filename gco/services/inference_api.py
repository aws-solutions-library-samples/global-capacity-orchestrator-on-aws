"""Dedicated authenticated streaming proxy API for managed inference endpoints."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gco.services.auth_middleware import AuthenticationMiddleware
from gco.services.request_size_middleware import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    RequestSizeLimitMiddleware,
)
from gco.services.service_metrics import mount_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="GCO Inference Proxy API",
    description="Authenticated streaming reverse proxy for managed GCO inference endpoints",
    version="1.0.0",
)

# Starlette executes middleware in reverse registration order. The size limit
# runs first so an oversized request is rejected before secret retrieval or
# HMAC work; AuthenticationMiddleware still validates the exact cached body.
app.add_middleware(AuthenticationMiddleware)
_max_body_bytes = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES)))
app.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=_max_body_bytes)

mount_metrics(app, "inference-proxy")

from gco.services.api_routes.inference_proxy import router as inference_proxy_router  # noqa: E402

app.include_router(inference_proxy_router)


@app.get("/", tags=["Info"])
async def root() -> dict[str, Any]:
    """Return a small authenticated service descriptor."""
    return {
        "service": "GCO Inference Proxy API",
        "version": "1.0.0",
        "status": "running",
        "region": os.getenv("REGION", "unknown"),
    }


@app.get("/healthz", tags=["Health"])
async def kubernetes_health_check() -> dict[str, str]:
    """Kubernetes and ALB liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", tags=["Health"])
async def kubernetes_readiness_check() -> dict[str, str]:
    """Readiness probe; external dependencies are resolved per request."""
    return {"status": "ready"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unexpected failures without exposing internal routing details."""
    logger.exception(
        "Unhandled inference proxy exception for %s %s", request.method, request.url.path
    )
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


def create_app() -> FastAPI:
    """Return the process-wide FastAPI application."""
    return app


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")  # nosec B104 — container listener
    port = int(os.getenv("PORT", "8080"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    logger.info("Starting Inference Proxy API on %s:%d", host, port)
    uvicorn.run(
        "gco.services.inference_api:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=False,
    )
