"""Prometheus metrics for the in-cluster GCO services.

Exposes a Prometheus ``/metrics`` surface for the three long-running GCO
services so the self-hosted cluster Prometheus can scrape them:

- ``mount_metrics(app, name)`` adds a ``GET /metrics`` endpoint and request
  instrumentation (request count + latency) to a FastAPI app. Used by the
  health-monitor and manifest-processor API services. The services' auth
  middleware already treats ``/metrics`` as unauthenticated, so the in-cluster
  Prometheus scrapes it over the existing service port without credentials.
- ``start_metrics_server(port, name, metrics_fn)`` starts a standalone metrics
  HTTP server for the loop-based inference monitor (which has no HTTP server of
  its own) and registers a collector that reflects the monitor's live counters
  at scrape time.

``prometheus-client`` is already a project dependency, so instrumenting these
services pulls no new package into their container images. All series carry a
``service`` label so a single Prometheus scrapes all three without collision.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

# Module-level metrics live in the default registry (what ``generate_latest``
# and ``start_http_server`` serve). Created once per process; ``mount_metrics``
# is safe to call more than once because it never re-declares them.
_REQUESTS = Counter(
    "gco_http_requests_total",
    "Total HTTP requests handled by a GCO service, by method and status.",
    ["service", "method", "status"],
)
_LATENCY = Histogram(
    "gco_http_request_duration_seconds",
    "HTTP request duration handled by a GCO service, in seconds.",
    ["service", "method"],
)
_INFO = Gauge(
    "gco_service_info",
    "GCO service liveness marker (always 1 while the process is up).",
    ["service"],
)

_METRICS_PATH = "/metrics"


def _render_latest() -> tuple[bytes, str]:
    """Return the current Prometheus exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST


def mount_metrics(app: FastAPI, service_name: str) -> None:
    """Add a ``/metrics`` endpoint and request instrumentation to a FastAPI app.

    Records request count and latency for every non-``/metrics`` request and
    exposes the default Prometheus registry at ``/metrics``. Cardinality is kept
    low: series are labelled by service, HTTP method, and status code only, not
    by raw request path.
    """
    from starlette.responses import Response

    _INFO.labels(service=service_name).set(1)

    @app.middleware("http")
    async def _instrument(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == _METRICS_PATH:
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        _LATENCY.labels(service=service_name, method=request.method).observe(elapsed)
        _REQUESTS.labels(
            service=service_name, method=request.method, status=str(response.status_code)
        ).inc()
        return response

    async def _metrics_endpoint(_request: Request) -> Response:
        payload, content_type = _render_latest()
        return Response(content=payload, media_type=content_type)

    app.add_route(_METRICS_PATH, _metrics_endpoint, methods=["GET"])


class _CallableCollector(Collector):
    """Prometheus collector that reflects a service's live counters at scrape time.

    Calls ``metrics_fn`` on every scrape so the exported values are always
    current, without the service having to push updates from its control loop.
    Numeric values become ``gco_<service>_metric{name=...}`` samples; booleans
    are exported as 1/0; non-numeric values (ids, regions) are skipped.
    """

    def __init__(self, service_name: str, metrics_fn: Callable[[], Mapping[str, Any]]) -> None:
        self._service = service_name
        self._metrics_fn = metrics_fn
        self._metric_name = f"gco_{service_name.replace('-', '_')}_metric"

    def collect(self) -> Iterator[GaugeMetricFamily]:
        info = GaugeMetricFamily(
            "gco_service_info",
            "GCO service liveness marker (always 1 while the process is up).",
            labels=["service"],
        )
        info.add_metric([self._service], 1.0)
        yield info

        family = GaugeMetricFamily(
            self._metric_name,
            f"Numeric counters reported by the {self._service} control loop.",
            labels=["name"],
        )
        try:
            reported = self._metrics_fn() or {}
        except Exception:  # noqa: BLE001 - a scrape must never crash the service
            reported = {}
        for key, value in reported.items():
            if isinstance(value, bool):
                family.add_metric([key], 1.0 if value else 0.0)
            elif isinstance(value, (int, float)):
                family.add_metric([key], float(value))
        yield family


def start_metrics_server(
    port: int,
    service_name: str,
    metrics_fn: Callable[[], Mapping[str, Any]],
) -> None:
    """Start a standalone Prometheus metrics HTTP server for a loop-based service.

    Serves a scrape-time collector backed by ``metrics_fn`` (e.g. the inference
    monitor's ``get_metrics``) on ``port``.

    The collector is registered on its own ``CollectorRegistry`` rather than the
    default one. The collector emits its own ``gco_service_info`` liveness series
    (so a loop service that never calls ``mount_metrics`` still reports
    liveness), which would collide with the module-level ``_INFO`` gauge if both
    lived in the default registry. A dedicated registry keeps the loop service's
    exposition to exactly its own series and lets the function be called more
    than once per process (e.g. across tests) without tripping the default
    registry's duplicate-name guard.
    """
    from prometheus_client import CollectorRegistry, start_http_server

    registry = CollectorRegistry()
    registry.register(_CallableCollector(service_name, metrics_fn))
    start_http_server(port, registry=registry)
