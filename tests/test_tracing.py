"""Tests for gco.services.tracing and the trace fields it adds to JSON logs.

Nothing here reaches the network: an autouse fixture swaps the exporter's
default client for an ``httpx2.MockTransport`` standing in for X-Ray,
credentials are static fakes, and spans are captured in memory. The set-once
global tracer provider and the global propagator are restored after every
test, and every FastAPI app a test instruments is uninstrumented again (the
instrumentor also patches Starlette's BackgroundTask process-wide).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import botocore.session
import httpx2
import pytest
import truststore
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials, ReadOnlyCredentials
from fastapi import FastAPI
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.instrumentation import httpx as otel_httpx
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util._once import Once
from opentelemetry.util.http import parse_excluded_urls
from starlette.testclient import TestClient

from gco import __version__
from gco.services import tracing
from gco.services.structured_logging import (
    StructuredJsonFormatter,
    configure_structured_logging,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-1"
DEFAULT_ENDPOINT = f"https://xray.{REGION}.amazonaws.com/v1/traces"
ACCESS_KEY = "AKIDEXAMPLE"
SECRET_KEY = "example-secret-access-key"
SESSION_TOKEN = "example-session-token"
MODEL_URL = "https://model.gco-inference.svc.cluster.local:8443/v1/models"
CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN_ID = "00f067aa0ba902b7"
SIGNED_HEADERS = ["content-encoding", "content-type", "host", "x-amz-date", "x-amz-security-token"]

#: Everything the module reads from the environment, cleared around each test.
_TRACING_ENV = (
    tracing.TRACING_ENABLED_ENV,
    tracing.TRACING_SAMPLE_RATIO_ENV,
    tracing.TRACING_ENDPOINT_ENV,
    "REGION",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "CLUSTER_NAME",
    "POD_NAMESPACE",
    "POD_NAME",
)
#: The real client factory, captured before the autouse fixture replaces it.
_REAL_EXPORT_CLIENT = tracing._new_export_client
#: The probe and scrape paths, plus the health-monitor API route that ends in
#: the scrape path and must still be traced.
_PLAIN_ROUTES = ("/healthz", "/readyz", "/metrics", "/api/v1/health", "/api/v1/metrics")
_route_logger = logging.getLogger("tests.tracing.route")

Outcome = Callable[[httpx2.Request], httpx2.Response]


def _respond(status_code: int, **kwargs: Any) -> Outcome:
    return lambda request: httpx2.Response(status_code, **kwargs)


def _raise(error: type[httpx2.TransportError], message: str) -> Outcome:
    def outcome(request: httpx2.Request) -> httpx2.Response:
        raise error(message, request=request)

    return outcome


class _XRay:
    """Stands in for X-Ray's OTLP endpoint: records requests, replays scripted outcomes."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.outcomes: list[Outcome] = []
        self.clients: list[httpx2.Client] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else _respond(200)
        return outcome(request)

    def client(self) -> httpx2.Client:
        client = httpx2.Client(transport=httpx2.MockTransport(self.handle))
        self.clients.append(client)
        return client


class _Clock:
    """A monotonic clock moved by hand; each backoff wait advances it."""

    def __init__(self) -> None:
        self.now = 100.0
        self.waits: list[float] = []
        #: Extra time each wait takes beyond the requested delay.
        self.overshoot = 0.0
        #: What each wait reports: True means shutdown cut it short.
        self.interrupted = False

    def __call__(self) -> float:
        return self.now

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        self.now += delay + self.overshoot
        return self.interrupted


class _JsonLines(logging.Handler):
    """Collects the JSON lines the structured formatter renders."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(StructuredJsonFormatter(service_name="inference-proxy"))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture(autouse=True)
def _isolated_tracing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh module and OpenTelemetry globals per test; shuts down whatever a test configured."""
    for name in (*_TRACING_ENV, *(name for name in os.environ if name.startswith("OTEL_"))):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tracing, "_active", None)
    monkeypatch.setattr(tracing, "_configure_attempted", False)
    monkeypatch.setattr(tracing, "_instrumentation_warnings", tracing._Throttle(60.0))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", propagate.get_global_textmap())
    yield
    tracing.shutdown_tracing()


@pytest.fixture(autouse=True)
def xray(monkeypatch: pytest.MonkeyPatch) -> _XRay:
    """Every exporter the module builds posts to the stand-in, never to AWS."""
    stand_in = _XRay()
    monkeypatch.setattr(tracing, "_new_export_client", stand_in.client)
    return stand_in


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tracing on at ratio 1.0 with static credentials and no AWS config files."""
    monkeypatch.setenv(tracing.TRACING_ENABLED_ENV, "true")
    monkeypatch.setenv(tracing.TRACING_SAMPLE_RATIO_ENV, "1.0")
    monkeypatch.setenv("REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("AWS_SESSION_TOKEN", SESSION_TOKEN)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def apps() -> Iterator[list[FastAPI]]:
    """Apps built by a test, uninstrumented afterwards."""
    built: list[FastAPI] = []
    yield built
    for app in built:
        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            FastAPIInstrumentor.uninstrument_app(app)


@pytest.fixture
def route_logs() -> Iterator[list[dict[str, Any]]]:
    handler = _JsonLines()
    previous_level = _route_logger.level
    _route_logger.addHandler(handler)
    _route_logger.setLevel(logging.INFO)
    yield handler.lines
    _route_logger.removeHandler(handler)
    _route_logger.setLevel(previous_level)


def _activate(service_name: str = "inference-proxy") -> tuple[TracerProvider, InMemorySpanExporter]:
    """Configure tracing (needs ``aws_env``) and also capture its spans in memory."""
    assert tracing.configure_tracing(service_name) is True
    active = tracing._active
    assert active is not None
    memory = InMemorySpanExporter()
    active.provider.add_span_processor(SimpleSpanProcessor(memory))
    return active.provider, memory


def _build_app(apps: list[FastAPI], model_requests: list[httpx2.Request] | None = None) -> FastAPI:
    """A small API with probe, scrape and API routes, and one route that calls a model."""
    app = FastAPI()
    apps.append(app)
    calls = model_requests if model_requests is not None else []

    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    for path in _PLAIN_ROUTES:
        app.add_api_route(path, ok)

    @app.get("/api/v1/jobs/{namespace}/{name}/metrics")
    async def job_metrics(namespace: str, name: str) -> dict[str, str]:
        return {"job": f"{namespace}/{name}"}

    def model(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"data": []})

    @app.get("/api/v1/proxy")
    async def proxy() -> dict[str, str]:
        _route_logger.info("proxying to the model")
        transport = tracing.wrap_async_transport(httpx2.MockTransport(model))
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.get(MODEL_URL)
        return {"status": "proxied"}

    return app


def _finished_spans(*names: str) -> list[ReadableSpan]:
    """Real finished SDK spans, recorded by a private provider."""
    provider = TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("tests")
    for name in names:
        with tracer.start_as_current_span(name):
            pass
    provider.shutdown()
    return list(memory.get_finished_spans())


def _exporter(
    xray: _XRay,
    clock: _Clock,
    *,
    credentials: Any = None,
    timeout: float = 10.0,
) -> tracing._XRaySpanExporter:
    return tracing._XRaySpanExporter(
        endpoint=DEFAULT_ENDPOINT,
        region=REGION,
        credentials=credentials or Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN),
        client=xray.client(),
        timeout=timeout,
        clock=clock,
        wait=clock.wait,
    )


def _tracing_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == tracing.logger.name]


def _assert_sigv4(request: httpx2.Request, credentials: Any, signed_headers: list[str]) -> None:
    """Recompute the SigV4 signature from what went over the wire and compare.

    A match proves the body and every signed header reached the endpoint
    exactly as they were signed.
    """
    scheme, _, fields = request.headers["authorization"].partition(" ")
    parts = {
        key.strip(): value
        for key, _, value in (field.partition("=") for field in fields.split(","))
    }
    amz_date = request.headers["x-amz-date"]
    frozen = credentials.get_frozen_credentials()
    assert scheme == "AWS4-HMAC-SHA256"
    assert parts["Credential"] == f"{frozen.access_key}/{amz_date[:8]}/{REGION}/xray/aws4_request"
    assert parts["SignedHeaders"].split(";") == signed_headers
    replay = AWSRequest(
        method=request.method,
        url=str(request.url),
        data=request.content,
        headers={name: request.headers[name] for name in signed_headers},
    )
    replay.context["timestamp"] = amz_date
    signer = SigV4Auth(frozen, "xray", REGION)
    string_to_sign = signer.string_to_sign(replay, signer.canonical_request(replay))
    assert parts["Signature"] == signer.signature(string_to_sign, replay)


def _traceparent(request: httpx2.Request) -> tuple[str, str, int]:
    _, trace_id, parent_id, flags = request.headers["traceparent"].split("-")
    return trace_id, parent_id, int(flags, 16)


# --- environment ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("1", False),
        ("yes", False),
        ("true", True),
        ("TRUE", True),
        (" True ", True),
    ],
)
def test_only_true_enables_tracing(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    if raw is not None:
        monkeypatch.setenv(tracing.TRACING_ENABLED_ENV, raw)
    assert tracing.tracing_enabled() is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0.05), ("", 0.05), ("0", 0.0), ("1", 1.0), (" 0.25 ", 0.25)],
)
def test_sample_ratio_accepts_numbers_from_zero_to_one(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raw: str | None,
    expected: float,
) -> None:
    if raw is not None:
        monkeypatch.setenv(tracing.TRACING_SAMPLE_RATIO_ENV, raw)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.sample_ratio() == expected
    assert _tracing_messages(caplog) == []


@pytest.mark.parametrize("raw", ["abc", "5%", "-0.1", "1.5", "nan", "inf"])
def test_an_invalid_sample_ratio_warns_and_uses_the_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    monkeypatch.setenv(tracing.TRACING_SAMPLE_RATIO_ENV, raw)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.sample_ratio() == tracing.DEFAULT_SAMPLE_RATIO
    assert _tracing_messages(caplog) == [
        f"Ignoring GCO_TRACING_SAMPLE_RATIO={raw!r}: expected a number from 0 to 1; "
        "sampling 0.05 of traces instead"
    ]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"REGION": "eu-west-1", "AWS_REGION": "us-west-2"}, "eu-west-1"),
        ({"AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "ap-south-1"}, "us-west-2"),
        ({"REGION": " ", "AWS_DEFAULT_REGION": "ap-south-1"}, "ap-south-1"),
        ({}, ""),
    ],
)
def test_the_region_comes_from_region_then_the_aws_sdk_variables(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert tracing._configured_region() == expected


@pytest.mark.parametrize(
    ("region", "valid"),
    [
        ("us-east-1", True),
        ("us-gov-west-1", True),
        ("eusc-de-east-1", True),
        ("cn-north-1", True),
        ("US-EAST-1", False),
        ("us-east-1.example.test", False),
        ("us-east-1/v1", False),
        ("useast1", False),
    ],
)
def test_only_region_shaped_names_reach_the_endpoint_host(region: str, valid: bool) -> None:
    assert (tracing._REGION_RE.fullmatch(region) is not None) is valid


@pytest.mark.parametrize(
    ("region", "endpoint"),
    [
        ("us-east-1", "https://xray.us-east-1.amazonaws.com/v1/traces"),
        ("us-gov-west-1", "https://xray.us-gov-west-1.amazonaws.com/v1/traces"),
        # The China partition's own DNS suffix, from botocore's endpoint metadata.
        ("cn-north-1", "https://xray.cn-north-1.amazonaws.com.cn/v1/traces"),
    ],
)
def test_default_endpoint_follows_the_regions_partition(region: str, endpoint: str) -> None:
    assert tracing._default_endpoint(region) == endpoint


def test_default_endpoint_falls_back_to_the_commercial_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_endpoint_metadata(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("endpoint metadata unavailable")

    monkeypatch.setattr(botocore.session.Session, "create_client", _no_endpoint_metadata)

    assert tracing._default_endpoint("us-west-2") == (
        "https://xray.us-west-2.amazonaws.com/v1/traces"
    )


# --- inert unless enabled -------------------------------------------------


_IMPORT_PROBE = textwrap.dedent(
    """
    import logging
    import sys

    from gco.services import structured_logging, tracing

    sentinel = object()
    assert tracing.configure_tracing("health-monitor") is False
    tracing.instrument_fastapi_app(sentinel)
    assert tracing.wrap_async_transport(sentinel) is sentinel
    assert tracing.wrap_sync_transport(sentinel) is sentinel
    assert tracing.current_trace_fields() == {}
    record = logging.LogRecord("probe", logging.INFO, "probe.py", 1, "hello", (), None)
    assert "trace_id" not in structured_logging.StructuredJsonFormatter().format(record)
    tracing.shutdown_tracing()
    roots = {"botocore", "google", "httpx2", "opentelemetry"}
    print(sorted(name for name in sys.modules if name.partition(".")[0] in roots))
    """
)


@pytest.mark.parametrize(
    "env",
    [{}, {"GCO_TRACING_ENABLED": "false"}, {"GCO_TRACING_ENABLED": "true"}],
    ids=["unset", "false", "enabled-without-region"],
)
def test_inactive_tracing_imports_no_tracing_dependency(env: dict[str, str]) -> None:
    """The OpenTelemetry-free images import this module; so must a fresh interpreter."""
    child_env = {name: value for name, value in os.environ.items() if name not in _TRACING_ENV}
    child_env.update(env, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_every_entry_point_is_inert_until_tracing_is_configured(apps: list[FastAPI]) -> None:
    app = _build_app(apps)
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200))

    assert tracing.configure_tracing("health-monitor") is False
    tracing.instrument_fastapi_app(app)
    assert tracing.wrap_async_transport(transport) is transport
    assert tracing.wrap_sync_transport(transport) is transport
    assert tracing.current_trace_fields() == {}
    tracing.shutdown_tracing()

    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    assert trace._TRACER_PROVIDER is None


def test_a_disabled_call_does_not_use_up_the_one_configure_attempt(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(tracing.TRACING_ENABLED_ENV, "false")
    assert tracing.configure_tracing("health-monitor") is False
    monkeypatch.setenv(tracing.TRACING_ENABLED_ENV, "true")
    assert tracing.configure_tracing("health-monitor") is True


# --- configure: prerequisites ---------------------------------------------


def test_a_missing_region_disables_tracing_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(tracing.TRACING_ENABLED_ENV, "true")
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.configure_tracing("health-monitor") is False
        # The second import of a service module must not warn again.
        assert tracing.configure_tracing("health-monitor") is False
    assert _tracing_messages(caplog) == [
        "Tracing disabled: GCO_TRACING_ENABLED is true but no AWS region is set "
        "(REGION, AWS_REGION or AWS_DEFAULT_REGION)"
    ]


def test_a_malformed_region_disables_tracing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(tracing.TRACING_ENABLED_ENV, "true")
    monkeypatch.setenv("REGION", "us-east-1.example.test")
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.configure_tracing("health-monitor") is False
    assert _tracing_messages(caplog) == [
        "Tracing disabled: 'us-east-1.example.test' is not an AWS region name"
    ]


@pytest.mark.parametrize(
    ("endpoint", "scheme"),
    [
        ("http://collector.example.test:4318/v1/traces", "http"),
        ("https:///v1/traces", "https"),
        ("collector.example.test:4318", "collector.example.test"),
    ],
)
def test_an_endpoint_override_must_be_an_https_url(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    endpoint: str,
    scheme: str,
) -> None:
    monkeypatch.setenv(tracing.TRACING_ENDPOINT_ENV, endpoint)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.configure_tracing("health-monitor") is False
    assert _tracing_messages(caplog) == [
        "Tracing disabled: GCO_TRACING_ENDPOINT must be an https:// URL with a host "
        f"(got scheme {scheme!r})"
    ]
    assert trace._TRACER_PROVIDER is None


@pytest.mark.parametrize(
    "module",
    [
        "opentelemetry.sdk.trace",
        "opentelemetry.instrumentation.fastapi",
        "opentelemetry.instrumentation.httpx",
        "httpx2",
    ],
)
def test_a_missing_tracing_package_disables_tracing(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    xray: _XRay,
    module: str,
) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.configure_tracing("cost-monitor") is False
    assert _tracing_messages(caplog) == [
        f"Tracing disabled: a tracing package is missing from this image ({module})"
    ]
    assert xray.clients == []


def test_missing_credentials_disable_tracing(
    aws_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(botocore.session.Session, "get_credentials", lambda self: None)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.configure_tracing("cost-monitor") is False
    assert _tracing_messages(caplog) == [
        "Tracing disabled: no AWS credentials are available to sign X-Ray exports"
    ]
    assert trace._TRACER_PROVIDER is None


def test_an_unexpected_setup_error_is_logged_once_and_leaves_tracing_off(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    xray: _XRay,
) -> None:
    def broken(service_name: str, region: str) -> dict[str, str]:
        raise RuntimeError("resource detection broke")

    monkeypatch.setattr(tracing, "_resource_attributes", broken)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert tracing.configure_tracing("cost-monitor") is False
        assert tracing.configure_tracing("cost-monitor") is False
    (record,) = [record for record in caplog.records if record.name == tracing.logger.name]
    assert record.getMessage() == "Tracing disabled: setup failed"
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert xray.clients == []
    assert trace._TRACER_PROVIDER is None


# --- configure: the installed provider ------------------------------------


def test_configure_installs_one_provider_with_trace_context_only_propagation(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    xray: _XRay,
) -> None:
    monkeypatch.setenv("CLUSTER_NAME", "gco-us-east-1")
    monkeypatch.setenv("POD_NAMESPACE", "gco-system")
    monkeypatch.setenv("POD_NAME", "inference-proxy-5d9c7b8f4-abcde")

    with caplog.at_level(logging.INFO, logger=tracing.logger.name):
        assert tracing.configure_tracing("inference-proxy") is True
        # A service module runs twice per process (python -m, then Uvicorn).
        assert tracing.configure_tracing("inference-proxy") is True

    active = tracing._active
    assert active is not None
    assert trace.get_tracer_provider() is active.provider
    textmap = propagate.get_global_textmap()
    assert isinstance(textmap, TraceContextTextMapPropagator)
    assert textmap.fields == {"traceparent", "tracestate"}
    expected_resource = {
        "service.name": "inference-proxy",
        "service.namespace": "gco",
        "service.version": __version__,
        "cloud.provider": "aws",
        "cloud.platform": "aws_eks",
        "cloud.region": REGION,
        "k8s.cluster.name": "gco-us-east-1",
        "k8s.namespace.name": "gco-system",
        "k8s.pod.name": "inference-proxy-5d9c7b8f4-abcde",
    }
    assert dict(active.provider.resource.attributes).items() >= expected_resource.items()
    assert _tracing_messages(caplog) == [
        "Tracing enabled for inference-proxy: sampling 1.0 of traces to "
        f"xray.{REGION}.amazonaws.com"
    ]
    # One exporter, whose own client is the bare transport: never instrumented.
    (client,) = xray.clients
    assert isinstance(client._transport, httpx2.MockTransport)


def test_unset_pod_metadata_is_left_out_of_the_resource(aws_env: None) -> None:
    provider, _ = _activate("health-monitor")
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "health-monitor"
    assert {"k8s.cluster.name", "k8s.namespace.name", "k8s.pod.name"}.isdisjoint(attributes)


def test_roots_and_remote_parents_share_one_trace_id_ratio_sampler(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(tracing.TRACING_SAMPLE_RATIO_ENV, "0.25")
    provider, _ = _activate()
    assert provider.sampler.get_description() == (
        "ParentBased{root:TraceIdRatioBased{0.25},"
        "remoteParentSampled:TraceIdRatioBased{0.25},"
        "remoteParentNotSampled:TraceIdRatioBased{0.25},"
        "localParentSampled:AlwaysOnSampler,"
        "localParentNotSampled:AlwaysOffSampler}"
    )


def test_shutdown_exports_queued_spans_through_the_signed_exporter(
    aws_env: None, xray: _XRay
) -> None:
    provider, _ = _activate("cost-monitor")
    with provider.get_tracer("tests").start_as_current_span("scheduled-report"):
        pass

    tracing.shutdown_tracing()

    (request,) = xray.requests
    assert str(request.url) == DEFAULT_ENDPOINT
    _assert_sigv4(request, Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN), SIGNED_HEADERS)
    exported = ExportTraceServiceRequest.FromString(gzip.decompress(request.content))
    (resource_spans,) = exported.resource_spans
    resource = {item.key: item.value.string_value for item in resource_spans.resource.attributes}
    assert resource["service.name"] == "cost-monitor"
    assert [span.name for scope in resource_spans.scope_spans for span in scope.spans] == [
        "scheduled-report"
    ]
    assert xray.clients[0].is_closed

    # Idempotent, and final: a shut-down process does not trace again.
    tracing.shutdown_tracing()
    assert tracing.configure_tracing("cost-monitor") is False


def test_an_https_endpoint_override_receives_the_exports(
    aws_env: None, monkeypatch: pytest.MonkeyPatch, xray: _XRay
) -> None:
    override = "https://xray-endpoint.example.test/v1/traces"
    monkeypatch.setenv(tracing.TRACING_ENDPOINT_ENV, override)
    provider, _ = _activate()
    with provider.get_tracer("tests").start_as_current_span("work"):
        pass
    tracing.shutdown_tracing()
    (request,) = xray.requests
    assert str(request.url) == override


def test_a_failing_shutdown_is_logged_not_raised(
    aws_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    provider, _ = _activate()
    real_shutdown = provider.shutdown

    def broken() -> None:
        raise RuntimeError("flush broke")

    monkeypatch.setattr(provider, "shutdown", broken)
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        tracing.shutdown_tracing()
    assert _tracing_messages(caplog) == [
        "Tracing: shutdown did not complete; queued spans may be lost"
    ]
    real_shutdown()


# --- server spans ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "excluded"),
    [
        ("http://inference-proxy.gco-system.svc:8080/healthz", True),
        ("https://testserver/readyz", True),
        ("http://testserver/metrics", True),
        ("http://testserver/api/v1/health", True),
        ("http://testserver/api/v1/metrics", False),
        ("http://testserver/api/v1/jobs/gco-jobs/train/metrics", False),
        ("http://testserver/api/v1/health/detail", False),
        ("http://testserver/api/v1/healthz", False),
        ("http://testserver/healthz/", False),
        ("http://testserver/metricsz", False),
        ("http://testserver/healthz\n", False),
    ],
)
def test_excluded_url_patterns_match_the_exact_paths_only(url: str, excluded: bool) -> None:
    assert parse_excluded_urls(tracing._EXCLUDED_URLS).url_disabled(url) is excluded


def test_server_spans_cover_api_routes_but_not_probe_or_scrape_paths(
    aws_env: None, apps: list[FastAPI]
) -> None:
    _, memory = _activate()
    app = _build_app(apps)
    tracing.instrument_fastapi_app(app)

    with TestClient(app) as client:
        for path in (*_PLAIN_ROUTES, "/api/v1/jobs/gco-jobs/train/metrics"):
            response = client.get(
                path, headers={"authorization": "Bearer example", "x-api-key": "example"}
            )
            assert response.status_code == 200

    spans = memory.get_finished_spans()
    assert [span.name for span in spans] == [
        "GET /api/v1/metrics",
        "GET /api/v1/jobs/{namespace}/{name}/metrics",
    ]
    # No ASGI receive/send sub-spans, and no captured headers.
    assert {span.kind for span in spans} == {SpanKind.SERVER}
    assert not [key for span in spans for key in span.attributes or {} if "header" in key]


def test_each_app_is_instrumented_once_and_a_second_app_is_traced_too(
    aws_env: None, apps: list[FastAPI], caplog: pytest.LogCaptureFixture
) -> None:
    _, memory = _activate()
    first, second = _build_app(apps), _build_app(apps)
    with caplog.at_level(logging.WARNING):
        tracing.instrument_fastapi_app(first)
        tracing.instrument_fastapi_app(first)
        tracing.instrument_fastapi_app(second)
    assert "already instrumented" not in caplog.text

    with TestClient(first) as first_client, TestClient(second) as second_client:
        first_client.get("/api/v1/metrics")
        second_client.get("/api/v1/metrics")

    spans = memory.get_finished_spans()
    assert [(span.name, span.kind) for span in spans] == [
        ("GET /api/v1/metrics", SpanKind.SERVER)
    ] * 2
    assert spans[0].context.trace_id != spans[1].context.trace_id


def test_a_callers_unsampled_flag_cannot_suppress_recording(
    aws_env: None, apps: list[FastAPI]
) -> None:
    _, memory = _activate()
    app = _build_app(apps)
    tracing.instrument_fastapi_app(app)

    with TestClient(app) as client:
        client.get(
            "/api/v1/metrics", headers={"traceparent": f"00-{CALLER_TRACE_ID}-{CALLER_SPAN_ID}-00"}
        )

    (span,) = memory.get_finished_spans()
    assert f"{span.context.trace_id:032x}" == CALLER_TRACE_ID
    assert span.parent is not None and span.parent.is_remote
    assert f"{span.parent.span_id:016x}" == CALLER_SPAN_ID


def test_a_callers_sampled_flag_cannot_force_recording(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    apps: list[FastAPI],
    route_logs: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv(tracing.TRACING_SAMPLE_RATIO_ENV, "0")
    _, memory = _activate()
    model_requests: list[httpx2.Request] = []
    app = _build_app(apps, model_requests)
    tracing.instrument_fastapi_app(app)

    with TestClient(app) as client:
        client.get(
            "/api/v1/proxy", headers={"traceparent": f"00-{CALLER_TRACE_ID}-{CALLER_SPAN_ID}-01"}
        )

    assert memory.get_finished_spans() == ()
    # The trace id still correlates the logs, and the next hop learns the
    # trace is unsampled.
    (line,) = route_logs
    assert (line["trace_id"], line["trace_sampled"]) == (CALLER_TRACE_ID, False)
    trace_id, _, flags = _traceparent(model_requests[0])
    assert trace_id == CALLER_TRACE_ID
    assert not flags & 0x01


# --- client spans ---------------------------------------------------------


def test_client_spans_nest_under_the_server_span_and_carry_the_trace_on(
    aws_env: None, apps: list[FastAPI], route_logs: list[dict[str, Any]]
) -> None:
    _, memory = _activate()
    model_requests: list[httpx2.Request] = []
    app = _build_app(apps, model_requests)
    tracing.instrument_fastapi_app(app)

    with TestClient(app) as client:
        assert client.get("/api/v1/proxy").status_code == 200

    spans = memory.get_finished_spans()
    (server,) = [span for span in spans if span.kind is SpanKind.SERVER]
    (client_span,) = [span for span in spans if span.kind is SpanKind.CLIENT]
    assert server.name == "GET /api/v1/proxy"
    assert client_span.parent is not None
    assert client_span.parent.span_id == server.context.span_id
    assert client_span.context.trace_id == server.context.trace_id
    # The URL attribute's name depends on the semantic-convention opt-in.
    urls = [str(value) for key, value in (client_span.attributes or {}).items() if "url" in key]
    assert MODEL_URL in urls

    trace_id, parent_id, flags = _traceparent(model_requests[0])
    assert (trace_id, parent_id) == (
        f"{server.context.trace_id:032x}",
        f"{client_span.context.span_id:016x}",
    )
    assert flags & 0x01
    # The request's log line joins the server span.
    (line,) = route_logs
    assert (line["trace_id"], line["span_id"], line["trace_sampled"]) == (
        f"{server.context.trace_id:032x}",
        f"{server.context.span_id:016x}",
        True,
    )


def test_the_sync_wrapper_records_client_spans_and_propagates_the_trace(aws_env: None) -> None:
    provider, memory = _activate("cost-monitor")
    seen: list[httpx2.Request] = []

    def opencost(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"code": 200, "data": []})

    transport = tracing.wrap_sync_transport(httpx2.MockTransport(opencost))
    with (
        provider.get_tracer("tests").start_as_current_span("scheduled-report") as parent,
        httpx2.Client(transport=transport) as client,
    ):
        client.get("https://opencost-tls.monitoring.svc.cluster.local:9443/allocation/compute")

    (client_span,) = [span for span in memory.get_finished_spans() if span.kind is SpanKind.CLIENT]
    assert client_span.parent is not None
    assert client_span.parent.span_id == parent.get_span_context().span_id
    trace_id, parent_id, _ = _traceparent(seen[0])
    assert (trace_id, parent_id) == (
        f"{parent.get_span_context().trace_id:032x}",
        f"{client_span.context.span_id:016x}",
    )


def test_instrumentation_failures_warn_once_and_never_raise(
    aws_env: None,
    monkeypatch: pytest.MonkeyPatch,
    apps: list[FastAPI],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _activate()

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("instrumentation broke")

    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", staticmethod(broken))
    monkeypatch.setattr(otel_httpx.AsyncOpenTelemetryTransportHttpx2, "__init__", broken)
    monkeypatch.setattr(otel_httpx.SyncOpenTelemetryTransportHttpx2, "__init__", broken)
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200))

    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        tracing.instrument_fastapi_app(_build_app(apps))
        assert tracing.wrap_async_transport(transport) is transport
        assert tracing.wrap_sync_transport(transport) is transport

    # One warning a minute for all three; the service keeps working untraced.
    assert _tracing_messages(caplog) == [
        "Tracing: could not instrument the FastAPI app; serving it without server spans"
    ]


# --- log correlation ------------------------------------------------------


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "gco.services.api_routes.inference_proxy",
        logging.INFO,
        "inference_proxy.py",
        1,
        "proxied %s",
        ("request",),
        None,
    )
    for name, value in extra.items():
        setattr(record, name, value)
    return record


_BASE_KEYS = ["timestamp", "level", "logger", "message", "service"]


def test_trace_fields_follow_the_current_span(aws_env: None) -> None:
    provider, _ = _activate()
    assert tracing.current_trace_fields() == {}
    with provider.get_tracer("tests").start_as_current_span("request") as span:
        context = span.get_span_context()
        assert tracing.current_trace_fields() == {
            "trace_id": f"{context.trace_id:032x}",
            "span_id": f"{context.span_id:016x}",
            "trace_sampled": True,
        }
    assert tracing.current_trace_fields() == {}


def test_unsampled_spans_keep_their_ids_for_log_correlation(
    aws_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(tracing.TRACING_SAMPLE_RATIO_ENV, "0")
    provider, memory = _activate()
    with provider.get_tracer("tests").start_as_current_span("dropped") as span:
        fields = tracing.current_trace_fields()
    assert fields == {
        "trace_id": f"{span.get_span_context().trace_id:032x}",
        "span_id": f"{span.get_span_context().span_id:016x}",
        "trace_sampled": False,
    }
    assert memory.get_finished_spans() == ()


def test_trace_fields_ignore_spans_while_gco_tracing_is_inactive() -> None:
    other = TracerProvider()
    with other.get_tracer("tests").start_as_current_span("not-ours"):
        assert tracing.current_trace_fields() == {}
    other.shutdown()


def test_trace_fields_never_raise(aws_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _activate()

    def broken() -> None:
        raise RuntimeError("context broke")

    monkeypatch.setattr(trace, "get_current_span", broken)
    assert tracing.current_trace_fields() == {}


def test_json_lines_put_trace_fields_after_defaults_and_before_extra(aws_env: None) -> None:
    provider, _ = _activate()
    formatter = StructuredJsonFormatter(service_name="inference-proxy", cluster_id="gco-us-east-1")

    with provider.get_tracer("tests").start_as_current_span("request") as span:
        line = json.loads(formatter.format(_record(request_id="req-1")))
        overridden = json.loads(formatter.format(_record(trace_id="set-by-the-caller")))

    context = span.get_span_context()
    assert list(line) == [
        *_BASE_KEYS,
        "cluster_id",
        "trace_id",
        "span_id",
        "trace_sampled",
        "request_id",
    ]
    assert line["message"] == "proxied request"
    assert (line["trace_id"], line["span_id"], line["trace_sampled"]) == (
        f"{context.trace_id:032x}",
        f"{context.span_id:016x}",
        True,
    )
    # An explicit ``extra`` field still has the last word.
    assert overridden["trace_id"] == "set-by-the-caller"


def test_json_lines_carry_no_trace_fields_outside_a_span_or_while_tracing_is_off(
    aws_env: None,
) -> None:
    formatter = StructuredJsonFormatter()
    assert list(json.loads(formatter.format(_record()))) == _BASE_KEYS
    _activate()
    assert list(json.loads(formatter.format(_record()))) == _BASE_KEYS


def test_json_lines_serialize_exceptions() -> None:
    try:
        raise ValueError("upstream refused the request")
    except ValueError:
        record = logging.LogRecord("gco", logging.ERROR, "x.py", 1, "failed", (), sys.exc_info())
    exception = json.loads(StructuredJsonFormatter().format(record))["exception"]
    assert exception["type"] == "ValueError"
    assert exception["message"] == "upstream refused the request"
    assert "ValueError: upstream refused the request" in "".join(exception["traceback"])


@pytest.mark.parametrize(("log_format", "json_lines"), [("json", True), ("text", False)])
def test_configure_structured_logging_installs_one_root_handler(
    monkeypatch: pytest.MonkeyPatch, log_format: str, json_lines: bool
) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    monkeypatch.setenv("LOG_FORMAT", log_format)
    monkeypatch.setenv("LOG_LEVEL", "debug")
    try:
        configure_structured_logging(service_name="cost-monitor", cluster_id="gco-us-east-1")
        (handler,) = root.handlers
        formatter = handler.formatter
        assert root.level == logging.DEBUG
        assert isinstance(formatter, StructuredJsonFormatter) is json_lines
        if isinstance(formatter, StructuredJsonFormatter):
            assert formatter.service_name == "cost-monitor"
            assert formatter.default_fields == {"cluster_id": "gco-us-east-1"}
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# --- the X-Ray exporter ---------------------------------------------------


def test_export_posts_one_gzipped_protobuf_batch_signed_for_xray(xray: _XRay) -> None:
    spans = _finished_spans("GET /api/v1/jobs", "GET")
    credentials = Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN)

    assert _exporter(xray, _Clock(), credentials=credentials).export(spans) is (
        SpanExportResult.SUCCESS
    )

    (request,) = xray.requests
    assert request.method == "POST"
    assert str(request.url) == DEFAULT_ENDPOINT
    assert request.headers["content-type"] == "application/x-protobuf"
    assert request.headers["content-encoding"] == "gzip"
    assert request.headers["x-amz-security-token"] == SESSION_TOKEN
    assert gzip.decompress(request.content) == encode_spans(spans).SerializeToString()
    _assert_sigv4(request, credentials, SIGNED_HEADERS)


def test_credentials_without_a_session_token_sign_without_the_token_header(
    xray: _XRay,
) -> None:
    credentials = Credentials(ACCESS_KEY, SECRET_KEY)
    exporter = _exporter(xray, _Clock(), credentials=credentials)
    assert exporter.export(_finished_spans("work")) is SpanExportResult.SUCCESS
    (request,) = xray.requests
    assert "x-amz-security-token" not in request.headers
    _assert_sigv4(request, credentials, SIGNED_HEADERS[:-1])


class _RotatingCredentials:
    """Resolves a new key pair on every call, like credentials after a rotation."""

    def __init__(self) -> None:
        self.issued: list[Credentials] = []

    def get_frozen_credentials(self) -> ReadOnlyCredentials:
        credentials = Credentials(f"{ACCESS_KEY}{len(self.issued)}", SECRET_KEY, SESSION_TOKEN)
        self.issued.append(credentials)
        return credentials.get_frozen_credentials()


def test_every_attempt_is_signed_with_freshly_resolved_credentials(xray: _XRay) -> None:
    xray.outcomes = [_respond(503)]
    rotating = _RotatingCredentials()

    assert _exporter(xray, _Clock(), credentials=rotating).export(_finished_spans("work")) is (
        SpanExportResult.SUCCESS
    )

    assert len(xray.requests) == 2
    for request, credentials in zip(xray.requests, rotating.issued, strict=True):
        _assert_sigv4(request, credentials, SIGNED_HEADERS)


@pytest.mark.parametrize(
    "failure",
    [
        _respond(429),
        _respond(500),
        _respond(503),
        _raise(httpx2.ConnectError, "connection refused"),
        _raise(httpx2.ReadTimeout, "timed out"),
    ],
    ids=["throttled", "server-error", "unavailable", "connect-error", "read-timeout"],
)
def test_retryable_failures_are_retried_with_exponential_backoff(
    xray: _XRay, failure: Outcome
) -> None:
    xray.outcomes = [failure] * 3
    clock = _Clock()
    assert _exporter(xray, clock).export(_finished_spans("work")) is SpanExportResult.SUCCESS
    assert len(xray.requests) == 4
    assert clock.waits == [0.5, 1.0, 2.0]


def test_retries_stop_after_the_last_attempt(xray: _XRay, caplog: pytest.LogCaptureFixture) -> None:
    xray.outcomes = [_respond(500, text="internal error")] * 5
    clock = _Clock()
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert _exporter(xray, clock).export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert len(xray.requests) == 4
    assert clock.waits == [0.5, 1.0, 2.0]
    assert _tracing_messages(caplog) == [
        "Dropped 1 span(s): X-Ray export failed after 4 attempt(s): HTTP 500: 'internal error'"
    ]


@pytest.mark.parametrize("status_code", [400, 403, 413])
def test_client_errors_fail_without_a_retry(
    xray: _XRay, caplog: pytest.LogCaptureFixture, status_code: int
) -> None:
    xray.outcomes = [_respond(status_code, text="Transaction Search\nis not enabled")]
    clock = _Clock()
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert _exporter(xray, clock).export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert len(xray.requests) == 1
    assert clock.waits == []
    # The quoted body cannot break the log line.
    assert _tracing_messages(caplog) == [
        f"Dropped 1 span(s): X-Ray export failed after 1 attempt(s): HTTP {status_code}: "
        "'Transaction Search\\nis not enabled'"
    ]


def test_a_retry_that_cannot_fit_the_deadline_is_not_attempted(xray: _XRay) -> None:
    xray.outcomes = [_respond(503)] * 3
    clock = _Clock()
    exporter = _exporter(xray, clock, timeout=1.0)
    assert exporter.export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert clock.waits == [0.5]
    # Each attempt only gets what is left of the deadline.
    assert [request.extensions["timeout"]["read"] for request in xray.requests] == [1.0, 0.5]


def test_no_attempt_starts_after_the_deadline_passed(
    xray: _XRay, caplog: pytest.LogCaptureFixture
) -> None:
    xray.outcomes = [_respond(503)]
    clock = _Clock()
    clock.overshoot = 60.0
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert _exporter(xray, clock).export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert len(xray.requests) == 1
    assert _tracing_messages(caplog) == [
        "Dropped 1 span(s): X-Ray export failed after 1 attempt(s): HTTP 503: ''"
    ]


def test_an_export_without_a_time_budget_sends_nothing(
    xray: _XRay, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        exporter = _exporter(xray, _Clock(), timeout=0.0)
        assert exporter.export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert xray.requests == []
    assert _tracing_messages(caplog) == [
        "Dropped 1 span(s): X-Ray export failed after 0 attempt(s): "
        "the export deadline passed before the first attempt"
    ]


def test_shutdown_cuts_a_backoff_short(xray: _XRay) -> None:
    xray.outcomes = [_respond(503)]
    clock = _Clock()
    clock.interrupted = True
    assert _exporter(xray, clock).export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert len(xray.requests) == 1
    assert clock.waits == [0.5]


def test_the_default_backoff_wait_is_the_shutdown_signal(xray: _XRay) -> None:
    exporter = tracing._XRaySpanExporter(
        endpoint=DEFAULT_ENDPOINT,
        region=REGION,
        credentials=Credentials(ACCESS_KEY, SECRET_KEY),
        client=xray.client(),
    )
    assert exporter._wait(0.0) is False
    exporter.shutdown()
    # Returns at once, reporting that shutdown interrupted the wait.
    assert exporter._wait(30.0) is True


class _BrokenCredentials:
    def get_frozen_credentials(self) -> ReadOnlyCredentials:
        raise RuntimeError("credential refresh failed")


def test_a_signing_failure_drops_the_batch_without_retrying(
    xray: _XRay, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock()
    exporter = _exporter(xray, clock, credentials=_BrokenCredentials())
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert exporter.export(_finished_spans("work")) is SpanExportResult.FAILURE
    assert xray.requests == []
    assert clock.waits == []
    assert _tracing_messages(caplog) == [
        "Dropped 1 span(s): X-Ray export failed after 1 attempt(s): "
        "RuntimeError: 'credential refresh failed'"
    ]


def test_an_unencodable_batch_is_dropped_not_raised(
    xray: _XRay, caplog: pytest.LogCaptureFixture
) -> None:
    exporter = _exporter(xray, _Clock())
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        assert exporter.export([object()]) is SpanExportResult.FAILURE  # type: ignore[list-item]
    assert xray.requests == []
    (message,) = _tracing_messages(caplog)
    assert message.startswith("Dropped 1 span(s): they could not be encoded (AttributeError: ")


def test_export_warnings_are_limited_to_one_a_minute(
    xray: _XRay, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock()
    exporter = _exporter(xray, clock)
    xray.outcomes = [_respond(403)] * 3
    spans = _finished_spans("work")
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        exporter.export(spans)
        clock.now += 30
        exporter.export(spans)
        clock.now += 31
        exporter.export(spans)
    failure = "Dropped 1 span(s): X-Ray export failed after 1 attempt(s): HTTP 403: ''"
    assert _tracing_messages(caplog) == [failure, f"{failure} (1 similar warnings suppressed)"]


def test_spans_rejected_inside_an_accepted_request_are_reported(
    xray: _XRay, caplog: pytest.LogCaptureFixture
) -> None:
    rejection = ExportTraceServiceResponse(
        partial_success=ExportTracePartialSuccess(
            rejected_spans=1, error_message="span end time is too old"
        )
    )
    xray.outcomes = [
        _respond(
            200,
            content=rejection.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
        )
    ]
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        result = _exporter(xray, _Clock()).export(_finished_spans("old", "new"))
    assert result is SpanExportResult.SUCCESS
    assert _tracing_messages(caplog) == [
        "X-Ray rejected 1 of 2 exported span(s): 'span end time is too old'"
    ]


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("application/x-protobuf", ExportTraceServiceResponse().SerializeToString()),
        ("application/x-protobuf", b"\xff\xff\xff"),
        ("application/json", b'{"partialSuccess": {"rejectedSpans": 1}}'),
    ],
    ids=["no-rejections", "undecodable", "not-protobuf"],
)
def test_accepted_requests_without_readable_rejections_log_nothing(
    xray: _XRay, caplog: pytest.LogCaptureFixture, content_type: str, content: bytes
) -> None:
    xray.outcomes = [_respond(200, content=content, headers={"content-type": content_type})]
    with caplog.at_level(logging.WARNING, logger=tracing.logger.name):
        result = _exporter(xray, _Clock()).export(_finished_spans("work"))
    assert result is SpanExportResult.SUCCESS
    assert _tracing_messages(caplog) == []


def test_exporter_lifecycle(xray: _XRay) -> None:
    exporter = _exporter(xray, _Clock())
    assert exporter.export([]) is SpanExportResult.SUCCESS
    assert exporter.force_flush() is True

    exporter.shutdown()
    exporter.shutdown()

    assert xray.clients[0].is_closed
    assert exporter.export(_finished_spans("late")) is SpanExportResult.FAILURE
    assert xray.requests == []


def test_the_default_export_client_trusts_public_cas_and_is_never_instrumented() -> None:
    client = _REAL_EXPORT_CLIENT()
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout == httpx2.Timeout(tracing._EXPORT_TIMEOUT_SECONDS)
        assert type(client._transport) is httpx2.HTTPTransport
        assert isinstance(client._transport._pool._ssl_context, truststore.SSLContext)
    finally:
        client.close()
