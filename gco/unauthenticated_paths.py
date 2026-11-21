"""The API paths the GCO services answer without request authentication.

The auth middleware (``gco/services/auth_middleware.py``) lets requests for
these paths through without the API Gateway's HMAC envelope, so load-balancer
probes and Global Accelerator health checks reach them. Every other path
needs the envelope.

This module imports nothing but the standard library, so ``ConfigLoader`` can
check ``global_accelerator.health_check_path`` against the same set at synth
without importing FastAPI. ``gco.services.auth_middleware`` re-exports the
set under the same name.
"""

from __future__ import annotations

#: URL paths (without the query string) that bypass authentication: the
#: Kubernetes probes, the Prometheus scrape, and ``/api/v1/health``, which
#: Global Accelerator probes for health-based routing.
UNAUTHENTICATED_PATHS = frozenset(["/healthz", "/readyz", "/metrics", "/api/v1/health"])
