"""OpenTelemetry tracing for the GCO API services, exported to AWS X-Ray.

The four FastAPI services (``health-monitor``, ``manifest-processor``,
``inference-proxy``, ``cost-monitor``) record a server span per request and a
client span per in-cluster hop they make, and send them straight to X-Ray's
OTLP endpoint, where Transaction Search indexes them into the ``aws/spans`` log
group. There is no collector to deploy or keep patched: each process signs its
own OTLP/HTTP exports with the pod's AWS credentials.

Design constraints, and how this module meets them:

* **Inert unless asked.** Nothing happens unless ``GCO_TRACING_ENABLED`` is
  ``true``, so tests, local runs and images without AWS credentials never
  export. GCO deployments trace by default only because the manifests always
  set the variable from ``cdk.json``.
* **Import-light.** OpenTelemetry, botocore and httpx2 are imported inside the
  functions that need them, and never while tracing is off. The
  inference-monitor and queue-processor images ship without OpenTelemetry and
  still import this module through :mod:`gco.services.structured_logging`.
* **Never breaks the service.** A missing region, credential or package logs
  one warning and leaves tracing off. Exports run on the SDK's background
  thread, never raise, and rate-limit their warnings.
* **Callers cannot steer sampling.** Roots and remote parents alike are sampled
  by trace-id ratio, so an incoming ``traceparent`` flag neither forces nor
  suppresses recording. Every GCO hop applies the same ratio to the same trace
  id, so a trace is kept or dropped as a whole.
* **No self-tracing.** The exporter's HTTP client is never instrumented, so
  exporting spans cannot produce spans.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx2
    from fastapi import FastAPI
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    # The exporter implements the SDK's SpanExporter interface but inherits
    # from it only for the type checker: a real base class would import the
    # SDK whenever this module loads, which the OpenTelemetry-free images
    # cannot do. The SDK never isinstance-checks its exporters.
    _SpanExporterBase = SpanExporter

    class _CredentialSource(Protocol):
        """What the exporter needs from botocore credentials.

        Refreshable credentials (Pod Identity, IRSA) renew themselves inside
        ``get_frozen_credentials``, so asking again for every attempt is how
        a long-lived exporter keeps signing with current keys.
        """

        def get_frozen_credentials(self) -> object:
            """Return the credentials to sign one request with, refreshed first if due."""

else:
    _SpanExporterBase = object

logger = logging.getLogger(__name__)

TRACING_ENABLED_ENV = "GCO_TRACING_ENABLED"
TRACING_SAMPLE_RATIO_ENV = "GCO_TRACING_SAMPLE_RATIO"
TRACING_ENDPOINT_ENV = "GCO_TRACING_ENDPOINT"
DEFAULT_SAMPLE_RATIO = 0.05
EXCLUDED_PATHS = ("/healthz", "/readyz", "/metrics", "/api/v1/health")

#: ``excluded_urls`` for the FastAPI instrumentation, which ``re.search``-es
#: each pattern against ``scheme://host[:port]/path`` (query already removed).
#: Anchoring both ends, with an authority that cannot contain ``/``, excludes
#: exactly these paths: the ``/metrics`` scrape is skipped while the
#: health-monitor's ``/api/v1/metrics`` API route is still traced.
_EXCLUDED_URLS = ",".join(
    rf"^[a-z][a-z0-9+.-]*://[^/]*{re.escape(path)}\Z" for path in EXCLUDED_PATHS
)

#: The region becomes part of the default endpoint's host name.
_REGION_RE = re.compile(r"[a-z]+(?:-[a-z]+)+-\d+")
_XRAY_SIGNING_NAME = "xray"
#: Per-export budget: the first attempt's timeout, and the deadline every
#: retry and backoff must also fit inside.
_EXPORT_TIMEOUT_SECONDS = 10.0
_MAX_EXPORT_ATTEMPTS = 4
_RETRY_BASE_DELAY_SECONDS = 0.5
_WARNING_INTERVAL_SECONDS = 60.0
#: How much of an error response a warning quotes.
_BODY_PREVIEW_CHARS = 200


class _Throttle:
    """Admits one event per interval and counts the ones it holds back.

    Export failures repeat on every batch (a missing permission, an
    unreachable endpoint), so one warning a minute that carries the number of
    repeats it stands for says everything without flooding the service log.
    """

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._last: float | None = None
        self._held = 0

    def admit(self) -> int | None:
        """Return how many events were held back since the last admitted one,
        or ``None`` when this event must be held back too."""
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self._interval:
                self._held += 1
                return None
            self._last = now
            held, self._held = self._held, 0
            return held


def _throttled_warning(throttle: _Throttle, message: str, *args: object) -> None:
    held = throttle.admit()
    if held is None:
        return
    if held:
        message += " (%d similar warnings suppressed)"
        args = (*args, held)
    logger.warning(message, *args)


def _is_retryable_status(status_code: int) -> bool:
    """Throttling and server-side failures are worth another attempt."""
    return status_code == 429 or 500 <= status_code <= 599


def _new_export_client() -> httpx2.Client:
    """Build the exporter's own client.

    It trusts the public CA store (httpx2's default ``verify``, backed by
    truststore), ignores proxy and CA variables from the environment, and is
    never wrapped by the tracing transport.
    """
    import httpx2

    return httpx2.Client(timeout=_EXPORT_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False)


class _XRaySpanExporter(_SpanExporterBase):
    """Sends span batches to X-Ray's OTLP endpoint as SigV4-signed HTTPS posts.

    Each batch is encoded once, as gzip-compressed OTLP protobuf, and posted
    through one shared client. Every attempt is signed afresh, so
    ``X-Amz-Date`` stays current across retries and rotated credentials are
    picked up. Throttling, server errors and transport errors are retried with
    exponential backoff while the next attempt still fits the export deadline;
    a backoff wait ends early when the exporter shuts down.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        credentials: _CredentialSource,
        client: httpx2.Client | None = None,
        timeout: float = _EXPORT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._region = region
        self._credentials = credentials
        self._client = client if client is not None else _new_export_client()
        self._timeout = timeout
        self._clock = clock
        self._stopped = threading.Event()
        # Returns True when shutdown cut the wait short.
        self._wait = wait if wait is not None else self._stopped.wait
        self._warnings = _Throttle(_WARNING_INTERVAL_SECONDS, clock)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        from opentelemetry.sdk.trace.export import SpanExportResult

        if self._stopped.is_set():
            return SpanExportResult.FAILURE
        if not spans:
            return SpanExportResult.SUCCESS
        try:
            body = self._encode(spans)
        except Exception as exc:
            self._warn(
                "Dropped %d span(s): they could not be encoded (%s: %r)",
                len(spans),
                type(exc).__name__,
                str(exc),
            )
            return SpanExportResult.FAILURE
        if self._post(body, len(spans)):
            return SpanExportResult.SUCCESS
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._client.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # Nothing is buffered here: the batch span processor owns the queue.
        return True

    @staticmethod
    def _encode(spans: Sequence[ReadableSpan]) -> bytes:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

        return gzip.compress(encode_spans(spans).SerializeToString(), compresslevel=6)

    def _post(self, body: bytes, span_count: int) -> bool:
        """Deliver one encoded batch; True once X-Ray accepted it."""
        import httpx2

        deadline = self._clock() + self._timeout
        attempts = 0
        failure = "the export deadline passed before the first attempt"
        for attempt in range(_MAX_EXPORT_ATTEMPTS):
            if attempt:
                delay = _RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
                if delay >= deadline - self._clock() or self._wait(delay):
                    break
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            attempts += 1
            try:
                response = self._send(body, remaining)
            except httpx2.TransportError as exc:
                failure = f"{type(exc).__name__}: {str(exc)!r}"
                continue
            except Exception as exc:
                # Signing or refreshing credentials failed; an instant retry cannot help.
                failure = f"{type(exc).__name__}: {str(exc)!r}"
                break
            if response.is_success:
                self._report_partial_success(response, span_count)
                return True
            failure = f"HTTP {response.status_code}: {response.text[:_BODY_PREVIEW_CHARS]!r}"
            if not _is_retryable_status(response.status_code):
                break
        self._warn(
            "Dropped %d span(s): X-Ray export failed after %d attempt(s): %s",
            span_count,
            attempts,
            failure,
        )
        return False

    def _send(self, body: bytes, timeout: float) -> httpx2.Response:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        request = AWSRequest(
            method="POST",
            url=self._endpoint,
            data=body,
            headers={"Content-Type": "application/x-protobuf", "Content-Encoding": "gzip"},
        )
        SigV4Auth(
            self._credentials.get_frozen_credentials(), _XRAY_SIGNING_NAME, self._region
        ).add_auth(request)
        # The body and every signed header go out exactly as they were signed.
        headers = {str(name): str(value) for name, value in request.headers.items()}
        return self._client.post(self._endpoint, content=body, headers=headers, timeout=timeout)

    def _report_partial_success(self, response: httpx2.Response, span_count: int) -> None:
        """Warn when X-Ray accepted the request but rejected some of its spans."""
        from google.protobuf.message import DecodeError
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceResponse,
        )

        if not response.headers.get("content-type", "").startswith("application/x-protobuf"):
            return
        try:
            partial = ExportTraceServiceResponse.FromString(response.content).partial_success
        except DecodeError:
            return
        if partial.rejected_spans:
            self._warn(
                "X-Ray rejected %d of %d exported span(s): %r",
                partial.rejected_spans,
                span_count,
                partial.error_message[:_BODY_PREVIEW_CHARS],
            )

    def _warn(self, message: str, *args: object) -> None:
        _throttled_warning(self._warnings, message, *args)


@dataclass(frozen=True, slots=True)
class _ActiveTracing:
    """What the other entry points use once :func:`configure_tracing` succeeded."""

    provider: TracerProvider
    #: Instrumentation histograms go nowhere: GCO's service metrics are Prometheus.
    meter_provider: MeterProvider


_lock = threading.Lock()
_active: _ActiveTracing | None = None
#: Set by the first enabled configure call whatever its outcome, so a failed
#: setup warns once per process and a shut-down process stays untraced. Only
#: read and set while holding ``_lock``.
_configure_attempted = threading.Event()
_instrumentation_warnings = _Throttle(_WARNING_INTERVAL_SECONDS)


def tracing_enabled() -> bool:
    """True only when ``GCO_TRACING_ENABLED`` is ``true`` (case-insensitive)."""
    return os.getenv(TRACING_ENABLED_ENV, "").strip().lower() == "true"


def sample_ratio() -> float:
    """Head-sampling ratio in [0, 1]; invalid or unset values use the default."""
    raw = os.getenv(TRACING_SAMPLE_RATIO_ENV, "").strip()
    if not raw:
        return DEFAULT_SAMPLE_RATIO
    try:
        value = float(raw)
    except ValueError:
        pass
    else:
        if 0.0 <= value <= 1.0:
            return value
    logger.warning(
        "Ignoring %s=%r: expected a number from 0 to 1; sampling %s of traces instead",
        TRACING_SAMPLE_RATIO_ENV,
        raw,
        DEFAULT_SAMPLE_RATIO,
    )
    return DEFAULT_SAMPLE_RATIO


def _configured_region() -> str:
    for name in ("REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _default_endpoint(region: str) -> str:
    """X-Ray's OTLP traces URL in ``region``, on that Region's partition.

    botocore's endpoint metadata knows each partition's DNS suffix (for
    example ``amazonaws.com.cn`` in the China Regions), so the URL names the
    host the X-Ray API itself answers on in that Region; no request is made
    and no credentials are needed. An unexpected lookup failure falls back to
    the commercial suffix, which ``GCO_TRACING_ENDPOINT`` can override.
    """
    try:
        import botocore.session

        base = (
            botocore.session.Session().create_client("xray", region_name=region).meta.endpoint_url
        )
    except Exception:
        return f"https://xray.{region}.amazonaws.com/v1/traces"
    return f"{str(base).rstrip('/')}/v1/traces"


def _resource_attributes(service_name: str, region: str) -> dict[str, str]:
    """Attributes naming the service and where it runs; unset ones are omitted."""
    from gco import __version__

    attributes = {
        "service.name": service_name,
        "service.namespace": "gco",
        "service.version": __version__,
        "cloud.provider": "aws",
        "cloud.platform": "aws_eks",
        "cloud.region": region,
    }
    for attribute, env_name in (
        ("k8s.cluster.name", "CLUSTER_NAME"),
        ("k8s.namespace.name", "POD_NAMESPACE"),
        ("k8s.pod.name", "POD_NAME"),
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            attributes[attribute] = value
    return attributes


def _setup(service_name: str) -> _ActiveTracing | None:
    """Build and install the process tracer provider, or log why not and return None."""
    region = _configured_region()
    if not region:
        logger.warning(
            "Tracing disabled: %s is true but no AWS region is set "
            "(REGION, AWS_REGION or AWS_DEFAULT_REGION)",
            TRACING_ENABLED_ENV,
        )
        return None
    if _REGION_RE.fullmatch(region) is None:
        logger.warning("Tracing disabled: %r is not an AWS region name", region)
        return None
    endpoint = os.getenv(TRACING_ENDPOINT_ENV, "").strip() or _default_endpoint(region)
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname:
        # Signed exports carry a session token, which must not travel in clear text.
        logger.warning(
            "Tracing disabled: %s must be an https:// URL with a host (got scheme %r)",
            TRACING_ENDPOINT_ENV,
            parts.scheme,
        )
        return None

    try:
        # Everything used later (by the exporter, instrument_fastapi_app and
        # the transport wrappers) is imported now too, so a package missing
        # from the image costs this one warning instead of tracing that only
        # half works.
        import botocore.auth
        import botocore.awsrequest
        import botocore.session
        import httpx2  # noqa: F401
        import opentelemetry.exporter.otlp.proto.common.trace_encoder
        import opentelemetry.instrumentation.fastapi
        import opentelemetry.instrumentation.httpx
        import opentelemetry.proto.collector.trace.v1.trace_service_pb2  # noqa: F401
        from opentelemetry import trace
        from opentelemetry.metrics import NoOpMeterProvider
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    except ImportError as exc:
        logger.warning(
            "Tracing disabled: a tracing package is missing from this image (%s)", exc.name or exc
        )
        return None

    credentials = botocore.session.Session().get_credentials()
    if credentials is None:
        logger.warning("Tracing disabled: no AWS credentials are available to sign X-Ray exports")
        return None

    ratio = sample_ratio()
    sampler = TraceIdRatioBased(ratio)
    provider = TracerProvider(
        resource=Resource.create(_resource_attributes(service_name, region)),
        sampler=ParentBased(
            root=sampler, remote_parent_sampled=sampler, remote_parent_not_sampled=sampler
        ),
    )
    exporter = _XRaySpanExporter(endpoint=endpoint, region=region, credentials=credentials)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # W3C trace context only: GCO propagates no baggage.
    set_global_textmap(TraceContextTextMapPropagator())
    logger.info(
        "Tracing enabled for %s: sampling %s of traces to %s", service_name, ratio, parts.hostname
    )
    return _ActiveTracing(provider=provider, meter_provider=NoOpMeterProvider())


def configure_tracing(service_name: str) -> bool:
    """Install the process-wide tracer provider once; True when tracing is active.

    Safe to call from every module that builds an app: a service module runs
    twice per process (``python -m`` and Uvicorn's import string), and the
    second call reuses the first call's provider. Never raises.
    """
    global _active
    if not tracing_enabled():
        return False
    with _lock:
        if not _configure_attempted.is_set():
            _configure_attempted.set()
            try:
                _active = _setup(service_name)
            except Exception:
                logger.warning("Tracing disabled: setup failed", exc_info=True)
        return _active is not None


def instrument_fastapi_app(app: FastAPI) -> None:
    """Add server spans to one FastAPI app when tracing is active.

    Probe and scrape paths (:data:`EXCLUDED_PATHS`) are not traced, the ASGI
    ``receive``/``send`` sub-spans are dropped and no headers are captured.
    Instrumenting the same app again does nothing.
    """
    active = _active
    if active is None or getattr(app, "_is_instrumented_by_opentelemetry", False):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=active.provider,
            meter_provider=active.meter_provider,
            excluded_urls=_EXCLUDED_URLS,
            exclude_spans=["receive", "send"],
        )
    except Exception:
        _throttled_warning(
            _instrumentation_warnings,
            "Tracing: could not instrument the FastAPI app; serving it without server spans",
        )


def wrap_async_transport(transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncBaseTransport:
    """Wrap an async httpx2 transport with client spans when tracing is active.

    The wrapper also injects ``traceparent`` into each request, so the next
    GCO hop continues the trace.
    """
    active = _active
    if active is None:
        return transport
    try:
        from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransportHttpx2

        return AsyncOpenTelemetryTransportHttpx2(
            # The instrumentation annotates the wrapped transport with httpx's
            # class; this wrapper is its httpx2 flavour, same interface.
            cast(Any, transport),
            tracer_provider=active.provider,
            meter_provider=active.meter_provider,
        )
    except Exception:
        _throttled_warning(
            _instrumentation_warnings,
            "Tracing: could not wrap an HTTP transport; its requests are not traced",
        )
        return transport


def wrap_sync_transport(transport: httpx2.BaseTransport) -> httpx2.BaseTransport:
    """Wrap a sync httpx2 transport with client spans when tracing is active."""
    active = _active
    if active is None:
        return transport
    try:
        from opentelemetry.instrumentation.httpx import SyncOpenTelemetryTransportHttpx2

        return SyncOpenTelemetryTransportHttpx2(
            cast(Any, transport),
            tracer_provider=active.provider,
            meter_provider=active.meter_provider,
        )
    except Exception:
        _throttled_warning(
            _instrumentation_warnings,
            "Tracing: could not wrap an HTTP transport; its requests are not traced",
        )
        return transport


def current_trace_fields() -> dict[str, object]:
    """Trace/span ids of the current span for log correlation, or ``{}``.

    Runs for every JSON log line, so while tracing is inactive it answers from
    one global read. OpenTelemetry is only consulted after
    :func:`configure_tracing` imported it, which images without it never do.
    Unsampled requests keep their ids (``trace_sampled`` is false), so their
    log lines still correlate across services. Never raises.
    """
    if _active is None:
        return {}
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return {}
        return {
            "trace_id": format(context.trace_id, "032x"),
            "span_id": format(context.span_id, "016x"),
            "trace_sampled": context.trace_flags.sampled,
        }
    except Exception:
        return {}


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider; idempotent and never raises.

    Called from each service's lifespan shutdown: queued spans are exported,
    within the export deadline, before the exporter's client closes. The
    process does not trace again afterwards.
    """
    global _active
    with _lock:
        active, _active = _active, None
    if active is None:
        return
    try:
        active.provider.shutdown()
    except Exception:
        logger.warning(
            "Tracing: shutdown did not complete; queued spans may be lost", exc_info=True
        )
