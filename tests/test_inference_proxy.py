"""Focused security and streaming tests for the managed inference proxy."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.types import Message

from gco.services import internal_tls, tracing
from gco.services.api_routes import inference_proxy as proxy
from gco.services.internal_tls import InternalTLSError

# Captured before any test swaps the constructor for a spy.
_REAL_ASYNC_TRANSPORT = httpx2.AsyncHTTPTransport


@pytest.fixture(autouse=True)
def _fresh_internal_ca_cache() -> Iterator[None]:
    """A context one test cached never answers another test's CA lookup."""
    internal_tls.clear_cache()
    yield
    internal_tls.clear_cache()


class _FakeStore:
    def __init__(self, endpoint: dict[str, object] | None):
        self.endpoint = endpoint

    def get_endpoint(self, endpoint_name: str) -> dict[str, object] | None:
        return self.endpoint


class _HeaderItems:
    def __init__(self, items: list[tuple[str, str]]):
        self._items = items

    def items(self):
        return iter(self._items)


class _FakeUpstreamResponse:
    def __init__(
        self,
        *,
        chunks: tuple[bytes, ...] = (),
        status_code: int = 200,
        headers: list[tuple[str, str]] | None = None,
        stream_error: Exception | None = None,
    ):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = httpx2.Headers(headers or [])
        self.stream_error = stream_error
        self.iterated = False
        self.closed = False

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterated = True
        for chunk in self.chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error

    async def aclose(self) -> None:
        self.closed = True


class _FakeHTTPClient:
    def __init__(
        self,
        response: _FakeUpstreamResponse | None = None,
        error: BaseException | None = None,
        build_error: Exception | None = None,
    ):
        self.response = response
        self.error = error
        self.build_error = build_error
        self.build_args: tuple[str, str] | None = None
        self.build_kwargs: dict[str, object] | None = None
        self.built_request = object()
        self.sent_request: object | None = None
        self.send_stream: bool | None = None
        self.send_started = asyncio.Event()
        self.closed = False

    def build_request(self, method: str, url: str, **kwargs: object) -> object:
        self.build_args = (method, url)
        self.build_kwargs = kwargs
        if self.build_error is not None:
            raise self.build_error
        return self.built_request

    async def send(self, request: object, *, stream: bool = False) -> _FakeUpstreamResponse:
        self.sent_request = request
        self.send_stream = stream
        self.send_started.set()
        if self.error is not None:
            raise self.error
        if self.response is None:
            # A model that never answers: the request can only be cancelled.
            await asyncio.Event().wait()
        assert self.response is not None
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def _request(
    method: str = "GET",
    *,
    path: str = "/inference/model",
    query: bytes = b"",
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [(name.encode(), value.encode()) for name, value in (headers or [])],
        "client": ("test-client", 1234),
        "server": ("testserver", 443),
    }
    return Request(scope, receive)


def _running_endpoint(**overrides: object) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "namespace": "gco-inference",
        "target_regions": ["us-west-2"],
        "desired_state": "running",
        "region_status": {"us-west-2": {"state": "running"}},
        "spec": {},
    }
    endpoint.update(overrides)
    return endpoint


def _install_store(monkeypatch: pytest.MonkeyPatch, endpoint: dict[str, object] | None) -> None:
    monkeypatch.setattr(proxy, "_get_inference_store", lambda: _FakeStore(endpoint))


def _install_http_client(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeHTTPClient,
) -> Mock:
    """Hand ``client`` to the next proxied request as its upstream client."""
    factory = Mock(return_value=client)
    monkeypatch.setattr(proxy, "_new_upstream_client", factory)
    return factory


def _streamed(
    *chunks: bytes,
    status_code: int = 200,
    headers: list[tuple[str, str]] | None = None,
) -> httpx2.Response:
    """A mock upstream response whose body is still a stream, like the real wire.

    A bytes body would be read eagerly by ``httpx2.Response`` and could then
    no longer be relayed raw.
    """

    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return httpx2.Response(status_code, headers=headers, content=body())


_Handler = Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]


class _RecordingTransport(httpx2.AsyncBaseTransport):
    """Stands in for the traced transport of one request's client.

    Answers from the wire's handler instead of the network and records that
    the client closed it; closing also closes the real transport it wraps.
    """

    def __init__(self, wire: _UpstreamWire, inner: httpx2.AsyncBaseTransport) -> None:
        self._wire = wire
        self.inner = inner
        self.closed = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._wire.requests.append(request)
        answer = self._wire.handler(request)
        return await answer if inspect.isawaitable(answer) else answer

    async def aclose(self) -> None:
        self.closed = True
        await self.inner.aclose()


class _UpstreamWire:
    """Real per-request upstream clients, answering from memory.

    The transport constructor is spied, and the tracing seam every upstream
    client passes through swaps in a :class:`_RecordingTransport` per client.
    Unless ``real_ca`` is set, the internal CA lookup returns one stand-in
    context; with it, the real cached lookup reads ``GCO_INTERNAL_CA_FILE``.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, real_ca: bool = False) -> None:
        self.context = ssl.create_default_context()
        self.context_lookups = 0
        self.transport_kwargs: list[dict[str, Any]] = []
        self.transports: list[_RecordingTransport] = []
        self.requests: list[httpx2.Request] = []
        self.handler: _Handler = lambda _request: _streamed()
        if not real_ca:
            monkeypatch.setattr(internal_tls, "internal_ssl_context", self._context)
        monkeypatch.setattr(httpx2, "AsyncHTTPTransport", self._transport)
        monkeypatch.setattr(tracing, "wrap_async_transport", self._wrap)

    def _context(self, ca_file: object = None) -> ssl.SSLContext:
        self.context_lookups += 1
        return self.context

    def _transport(self, **kwargs: Any) -> httpx2.AsyncHTTPTransport:
        self.transport_kwargs.append(kwargs)
        return _REAL_ASYNC_TRANSPORT(**kwargs)

    def _wrap(self, transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncBaseTransport:
        recording = _RecordingTransport(self, transport)
        self.transports.append(recording)
        return recording


def _ca_pem(common_name: str) -> bytes:
    """A throwaway self-signed CA certificate, PEM-encoded."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _trusted_common_names(context: ssl.SSLContext) -> list[str]:
    """Common names of the CA certificates ``context`` trusts."""
    return [
        value
        for ca in context.get_ca_certs()
        for rdn in ca["subject"]
        for key, value in rdn
        if key == "commonName"
    ]


def _asgi_scope(spec_version: str) -> dict[str, object]:
    """A minimal ASGI HTTP scope for driving a response directly."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "path": "/inference/model/v1/completions",
        "headers": [],
    }


def _ready_canary_status(**overrides: object) -> dict[str, object]:
    status: dict[str, object] = {
        "state": "running",
        "image": "registry.example/model:v2",
        "replicas_ready": 2,
        "replicas_desired": 2,
    }
    status.update(overrides)
    return status


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 5.0),
        ("0.1", 0.1),
        ("30", 30.0),
        ("not-a-number", 5.0),
        ("nan", 5.0),
        ("0.09", 5.0),
        ("30.01", 5.0),
    ],
)
def test_bounded_timeout_uses_only_finite_in_range_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
    expected: float,
) -> None:
    name = "TEST_INFERENCE_TIMEOUT"
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)

    assert proxy._bounded_timeout(name, 5.0, 0.1, 30.0) == expected


def test_inference_store_factory_is_process_local_and_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = object()
    factory = Mock(return_value=store)
    proxy._get_inference_store.cache_clear()
    monkeypatch.setattr(proxy, "get_inference_endpoint_store", factory)

    try:
        assert proxy._get_inference_store() is store
        assert proxy._get_inference_store() is store
        factory.assert_called_once_with()
    finally:
        proxy._get_inference_store.cache_clear()


@pytest.mark.parametrize("label", ["a", "0", "model-1", "a--b", "a" * 63])
def test_validate_label_accepts_kubernetes_dns_labels(label: str) -> None:
    assert proxy._validate_label(label, "name") == label


@pytest.mark.parametrize(
    "label",
    [None, 7, "", "Model", "-model", "model-", "model_name", "model.name", "a" * 64],
)
def test_validate_label_hides_invalid_identifiers(label: object) -> None:
    with pytest.raises(HTTPException) as raised:
        proxy._validate_label(label, "namespace")

    assert raised.value.status_code == 404
    assert raised.value.detail == "Invalid inference namespace"


@pytest.mark.parametrize("spec", [None, [], "invalid"])
def test_target_service_rejects_malformed_specs(spec: object) -> None:
    with pytest.raises(HTTPException) as raised:
        proxy._target_service({"spec": spec}, "model")

    assert raised.value.status_code == 503
    assert raised.value.detail == "Inference endpoint has an invalid spec"


@pytest.mark.parametrize("mode", ["disaggregated", "both"])
def test_target_service_uses_mooncake_proxy_before_canary_logic(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(proxy.secrets, "randbelow", Mock(side_effect=AssertionError("sampled")))
    endpoint = {
        "spec": {
            "mooncake": {"mode": mode},
            "canary": {"weight": 50, "image": "registry.example/model:v2"},
        },
        "region_status": {
            "us-west-2": {"canary": _ready_canary_status()},
        },
    }

    assert proxy._target_service(endpoint, "model") == "model-proxy"


def test_target_service_revalidates_derived_mooncake_service_name() -> None:
    with pytest.raises(HTTPException) as raised:
        proxy._target_service(
            {"spec": {"mooncake": {"mode": "disaggregated"}}},
            "a" * 58,
        )

    assert raised.value.status_code == 404
    assert raised.value.detail == "Invalid inference service"


def test_target_service_uses_plain_service_for_non_disaggregated_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    endpoint = {
        "spec": {"mooncake": {"mode": "monolithic"}},
        "region_status": {"us-west-2": {"state": "running"}},
    }

    assert proxy._target_service(endpoint, "model") == "model"


@pytest.mark.parametrize(
    ("weight", "sample", "expected"),
    [(1, 0, "model-canary"), (25, 24, "model-canary"), (25, 25, "model"), (99, 98, "model-canary")],
)
def test_target_service_samples_only_ready_canaries(
    monkeypatch: pytest.MonkeyPatch,
    weight: int,
    sample: int,
    expected: str,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    sample_mock = Mock(return_value=sample)
    monkeypatch.setattr(proxy.secrets, "randbelow", sample_mock)
    endpoint = {
        "spec": {
            "canary": {"weight": weight, "image": "registry.example/model:v2"},
        },
        "region_status": {
            "us-west-2": {"canary": _ready_canary_status()},
        },
    }

    assert proxy._target_service(endpoint, "model") == expected
    sample_mock.assert_called_once_with(100)


@pytest.mark.parametrize(
    ("canary", "region_status"),
    [
        ("invalid", {"us-west-2": {"canary": _ready_canary_status()}}),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": "invalid"}},
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status(state="deploying")}},
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status(image="other:v2")}},
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status(replicas_desired=0)}},
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status(replicas_ready=1)}},
        ),
        (
            {"weight": 0, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status()}},
        ),
        (
            {"weight": 100, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status()}},
        ),
        (
            {"weight": "invalid", "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status()}},
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": {"canary": _ready_canary_status(replicas_ready=None)}},
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            None,
        ),
        (
            {"weight": 25, "image": "registry.example/model:v2"},
            {"us-west-2": []},
        ),
    ],
)
def test_target_service_falls_back_for_unready_or_malformed_canaries(
    monkeypatch: pytest.MonkeyPatch,
    canary: object,
    region_status: object,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setattr(proxy.secrets, "randbelow", Mock(side_effect=AssertionError("sampled")))
    endpoint = {"spec": {"canary": canary}, "region_status": region_status}

    assert proxy._target_service(endpoint, "model") == "model"


def test_target_service_revalidates_derived_canary_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setattr(proxy.secrets, "randbelow", Mock(return_value=0))
    endpoint = {
        "spec": {
            "canary": {"weight": 50, "image": "registry.example/model:v2"},
        },
        "region_status": {"us-west-2": {"canary": _ready_canary_status()}},
    }

    with pytest.raises(HTTPException) as raised:
        proxy._target_service(endpoint, "a" * 57)

    assert raised.value.status_code == 404
    assert raised.value.detail == "Invalid inference service"


async def test_resolve_upstream_rejects_invalid_name_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_factory = Mock(side_effect=AssertionError("store accessed"))
    monkeypatch.setattr(proxy, "_get_inference_store", store_factory)

    with pytest.raises(HTTPException) as raised:
        await proxy._resolve_upstream("../model")

    assert raised.value.status_code == 404
    assert raised.value.detail == "Invalid inference name"
    store_factory.assert_not_called()


async def test_resolve_upstream_returns_not_found_for_missing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_store(monkeypatch, None)

    with pytest.raises(HTTPException) as raised:
        await proxy._resolve_upstream("missing")

    assert raised.value.status_code == 404
    assert raised.value.detail == "Inference endpoint 'missing' not found"


@pytest.mark.parametrize(
    ("namespace", "allowed_namespace", "status", "detail"),
    [
        ("invalid/name", "gco-inference", 404, "Invalid inference namespace"),
        (
            "private-models",
            "gco-inference",
            503,
            "Inference endpoint namespace is not routable",
        ),
    ],
)
async def test_resolve_upstream_enforces_namespace_boundary(
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    allowed_namespace: str,
    status: int,
    detail: str,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setenv("INFERENCE_NAMESPACE", allowed_namespace)
    _install_store(monkeypatch, _running_endpoint(namespace=namespace))

    with pytest.raises(HTTPException) as raised:
        await proxy._resolve_upstream("model")

    assert raised.value.status_code == status
    assert raised.value.detail == detail


@pytest.mark.parametrize(
    ("region", "target_regions"),
    [
        ("", ["us-west-2"]),
        ("us-west-2", "us-west-2"),
        ("us-west-2", ["us-east-1"]),
    ],
)
async def test_resolve_upstream_hides_endpoints_not_deployed_locally(
    monkeypatch: pytest.MonkeyPatch,
    region: str,
    target_regions: object,
) -> None:
    monkeypatch.setenv("REGION", region)
    monkeypatch.setenv("INFERENCE_NAMESPACE", "gco-inference")
    _install_store(monkeypatch, _running_endpoint(target_regions=target_regions))

    with pytest.raises(HTTPException) as raised:
        await proxy._resolve_upstream("model")

    assert raised.value.status_code == 404
    assert raised.value.detail == "Inference endpoint is not deployed in this region"


@pytest.mark.parametrize(
    ("desired_state", "region_status"),
    [
        ("stopped", {"us-west-2": {"state": "running"}}),
        ("running", None),
        ("running", {"us-west-2": []}),
        ("running", {"us-west-2": {"state": "deploying"}}),
    ],
)
async def test_resolve_upstream_requires_desired_and_local_running_state(
    monkeypatch: pytest.MonkeyPatch,
    desired_state: str,
    region_status: object,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setenv("INFERENCE_NAMESPACE", "gco-inference")
    _install_store(
        monkeypatch,
        _running_endpoint(desired_state=desired_state, region_status=region_status),
    )

    with pytest.raises(HTTPException) as raised:
        await proxy._resolve_upstream("model")

    assert raised.value.status_code == 503
    assert raised.value.detail == "Inference endpoint is not ready in this region"


@pytest.mark.parametrize(
    ("spec", "expected_health_path"),
    [
        ({}, "/health"),
        ({"health_check_path": "/readyz"}, "/readyz"),
        ({"health_check_path": "readyz"}, "/health"),
        ({"health_check_path": 123}, "/health"),
        ({"health_check_path": "/"}, "/"),
    ],
)
async def test_resolve_upstream_defaults_and_validates_health_path(
    monkeypatch: pytest.MonkeyPatch,
    spec: dict[str, object],
    expected_health_path: str,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.delenv("INFERENCE_NAMESPACE", raising=False)
    endpoint = _running_endpoint(spec=spec)
    endpoint.pop("namespace")
    _install_store(monkeypatch, endpoint)

    assert await proxy._resolve_upstream("model") == (
        "model",
        "gco-inference",
        expected_health_path,
    )


async def test_resolve_upstream_rejects_malformed_spec_after_readiness_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setenv("INFERENCE_NAMESPACE", "gco-inference")
    _install_store(monkeypatch, _running_endpoint(spec=None))

    with pytest.raises(HTTPException) as raised:
        await proxy._resolve_upstream("model")

    assert raised.value.status_code == 503
    assert raised.value.detail == "Inference endpoint has an invalid spec"


async def test_resolve_upstream_returns_derived_mooncake_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGION", "us-west-2")
    monkeypatch.setenv("INFERENCE_NAMESPACE", "gco-inference")
    _install_store(
        monkeypatch,
        _running_endpoint(spec={"mooncake": {"mode": "both"}, "health_check_path": "/healthz"}),
    )

    assert await proxy._resolve_upstream("model") == (
        "model-proxy",
        "gco-inference",
        "/healthz",
    )


def test_request_headers_forward_only_explicit_model_headers() -> None:
    request = _request(
        headers=[
            ("Accept", "text/event-stream"),
            ("Authorization", "Bearer secret"),
            ("Content-Type", "application/json"),
            ("Cookie", "session=secret"),
            ("Host", "public.example"),
            ("Connection", "keep-alive"),
            ("X-Request-ID", "request-123"),
            ("Range", "bytes=0-99"),
            ("X-Forwarded-For", "203.0.113.10"),
        ]
    )

    assert proxy._request_headers(request) == [
        ("accept", "text/event-stream"),
        ("content-type", "application/json"),
        ("x-request-id", "request-123"),
        ("range", "bytes=0-99"),
    ]


def test_response_headers_drop_framing_but_preserve_end_to_end_metadata() -> None:
    response = SimpleNamespace(
        headers=_HeaderItems(
            [
                ("Content-Type", "application/json"),
                ("Content-Length", "999"),
                ("Connection", "close"),
                ("Keep-Alive", "timeout=5"),
                ("Proxy-Authenticate", "Basic"),
                ("Proxy-Authorization", "secret"),
                ("TE", "trailers"),
                ("Trailer", "Expires"),
                ("Transfer-Encoding", "chunked"),
                ("Upgrade", "websocket"),
                ("ETag", '"model-v1"'),
                ("Set-Cookie", "model-cookie=value"),
            ]
        )
    )

    assert proxy._response_headers(response) == {
        "Content-Type": "application/json",
        "ETag": '"model-v1"',
        "Set-Cookie": "model-cookie=value",
    }


@pytest.mark.parametrize(
    ("path", "method", "health_path", "expected"),
    [
        ("", "GET", "/health", ""),
        ("////", "HEAD", "/health", ""),
        ("health", "get", "/health", "health"),
        ("/internal/ready/", "HEAD", "/internal/ready", "internal/ready"),
        ("v1/models", "GET", "/health", "v1/models"),
        ("v1/models/model-a", "HEAD", "/health", "v1/models/model-a"),
        ("server_info", "GET", "/health", "server_info"),
        ("/server_info/", "HEAD", "/health", "server_info"),
        ("v1/chat/completions", "POST", "/health", "v1/chat/completions"),
        ("v1/completions", "POST", "/health", "v1/completions"),
        ("v1/embeddings", "POST", "/health", "v1/embeddings"),
        ("v1/responses", "POST", "/health", "v1/responses"),
        ("generate", "POST", "/health", "generate"),
        ("v2/models", "GET", "/health", "v2/models"),
        ("v2/models/model-a", "HEAD", "/health", "v2/models/model-a"),
        ("v2/models/model-a/config", "GET", "/health", "v2/models/model-a/config"),
        ("v2/models/model-a/infer", "POST", "/health", "v2/models/model-a/infer"),
        ("v2/models/model-a/ready", "GET", "/health", "v2/models/model-a/ready"),
        ("v2/models/model-a/stats", "POST", "/health", "v2/models/model-a/stats"),
    ],
)
def test_validate_upstream_path_allows_only_serving_and_health_apis(
    path: str,
    method: str,
    health_path: str,
    expected: str,
) -> None:
    assert proxy._validate_upstream_path(path, method, health_path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "admin",
        "v1/debug/status",
        "docs",
        "v2/models/model/instances",
        "V1/METRICS",
        "openapi.json",
    ],
)
def test_validate_upstream_path_blocks_privileged_segments_before_allowlisting(
    path: str,
) -> None:
    with pytest.raises(HTTPException) as raised:
        proxy._validate_upstream_path(path, "GET", f"/{path}")

    assert raised.value.status_code == 404
    assert raised.value.detail == "Inference path is not exposed"


@pytest.mark.parametrize(
    ("path", "method", "health_path"),
    [
        ("unknown", "GET", "/health"),
        ("readyz", "GET", "/"),
        ("health", "POST", "/health"),
        ("v1/models", "POST", "/health"),
        ("server_info", "POST", "/health"),
        # Paths of the retired TGI runtime; nothing answers them any more.
        ("info", "GET", "/health"),
        ("generate_stream", "POST", "/health"),
        # SGLang's other management/introspection routes stay unexposed.
        ("get_server_info", "GET", "/health"),
        ("get_model_info", "GET", "/health"),
        ("flush_cache", "POST", "/health"),
        ("update_weights_from_disk", "POST", "/health"),
        ("v1/models/model/extra", "GET", "/health"),
        ("v1/chat/completions", "GET", "/health"),
        ("V1/models", "GET", "/health"),
        ("v2/models/model/unknown", "POST", "/health"),
        ("v2/models", "DELETE", "/health"),
    ],
)
def test_validate_upstream_path_rejects_unlisted_path_method_pairs(
    path: str,
    method: str,
    health_path: str,
) -> None:
    with pytest.raises(HTTPException) as raised:
        proxy._validate_upstream_path(path, method, health_path)

    assert raised.value.status_code == 404
    assert raised.value.detail == "Inference path is not exposed"


async def test_stream_response_yields_raw_chunks_then_closes_response_and_client() -> None:
    response = _FakeUpstreamResponse(chunks=(b"first", b"second"))
    client = _FakeHTTPClient(response)

    chunks = [chunk async for chunk in proxy._stream_response(response, client)]

    assert chunks == [b"first", b"second"]
    # The client belongs to this one request, so it goes with the stream.
    assert response.closed is True
    assert client.closed is True


async def test_stream_response_releases_the_upstream_when_iteration_fails() -> None:
    response = _FakeUpstreamResponse(
        chunks=(b"partial",),
        stream_error=RuntimeError("upstream stream failed"),
    )
    client = _FakeHTTPClient(response)

    with pytest.raises(RuntimeError, match="upstream stream failed"):
        [chunk async for chunk in proxy._stream_response(response, client)]

    assert response.closed is True
    assert client.closed is True


async def test_stream_response_shields_cleanup_from_consumer_cancellation() -> None:
    response_close_started = asyncio.Event()
    release_response_close = asyncio.Event()
    response_closed = asyncio.Event()

    class BlockingResponse:
        async def aiter_raw(self) -> AsyncIterator[bytes]:
            yield b"chunk"

        async def aclose(self) -> None:
            response_close_started.set()
            await release_response_close.wait()
            response_closed.set()

    client = _FakeHTTPClient()
    stream = proxy._stream_response(BlockingResponse(), client)
    assert await anext(stream) == b"chunk"

    close_task = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(response_close_started.wait(), timeout=1)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    # The cancelled consumer left the cleanup running, held until it finishes.
    [cleanup] = proxy._cleanup_tasks
    assert client.closed is False
    release_response_close.set()
    await asyncio.wait_for(response_closed.wait(), timeout=1)
    await asyncio.wait_for(cleanup, timeout=1)
    assert client.closed is True
    # Done callbacks run on the loop's next turn; then the task is let go.
    await asyncio.sleep(0)
    assert proxy._cleanup_tasks == set()


async def test_close_upstream_closes_the_client_even_when_the_response_close_fails() -> None:
    class BrokenResponse:
        async def aclose(self) -> None:
            raise RuntimeError("response close failed")

    client = _FakeHTTPClient()

    with pytest.raises(RuntimeError, match="response close failed"):
        await proxy._close_upstream(BrokenResponse(), client)

    assert client.closed is True


async def test_close_upstream_without_a_response_closes_only_the_client() -> None:
    client = _FakeHTTPClient()

    await proxy._close_upstream(None, client)

    assert client.closed is True


async def test_streaming_response_relays_the_body_and_releases_the_upstream() -> None:
    response = _FakeUpstreamResponse(
        chunks=(b"data: one\n\n", b"data: two\n\n"),
        status_code=201,
        headers=[("content-type", "text/event-stream"), ("content-length", "22")],
    )
    client = _FakeHTTPClient(response)
    streamed = proxy._UpstreamStreamingResponse(response, client)
    sent: list[Message] = []

    async def receive() -> Message:
        # The caller stays connected until the stream is done.
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await streamed(_asgi_scope("2.3"), receive, send)

    assert sent[0]["status"] == 201
    assert (b"content-type", b"text/event-stream") in sent[0]["headers"]
    assert all(name != b"content-length" for name, _value in sent[0]["headers"])
    assert [message["body"] for message in sent[1:]] == [
        b"data: one\n\n",
        b"data: two\n\n",
        b"",
    ]
    assert response.closed is True
    assert client.closed is True
    assert proxy._cleanup_tasks == set()


async def test_streaming_response_releases_the_upstream_when_the_caller_left_first() -> None:
    """The body generator never starts; the response still releases the upstream."""
    response = _FakeUpstreamResponse(chunks=(b"never relayed",))
    client = _FakeHTTPClient(response)
    streamed = proxy._UpstreamStreamingResponse(response, client)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        # The caller hung up while the model was answering (ASGI 2.4 servers
        # report that as OSError), so the headers never leave.
        raise OSError("caller disconnected")

    with pytest.raises(ClientDisconnect):
        await streamed(_asgi_scope("2.4"), receive, send)

    assert response.iterated is False
    assert response.closed is True
    assert client.closed is True


async def test_streaming_response_releases_the_upstream_when_cancelled_before_the_body() -> None:
    """A disconnect seen before the first chunk cancels the relay before it starts."""
    response = _FakeUpstreamResponse(chunks=(b"never relayed",))
    client = _FakeHTTPClient(response)
    streamed = proxy._UpstreamStreamingResponse(response, client)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        # A middleware's send yields to the loop, where the disconnect lands.
        await asyncio.sleep(0)

    await streamed(_asgi_scope("2.3"), receive, send)

    assert [message["type"] for message in sent] == ["http.response.start"]
    assert response.iterated is False
    assert response.closed is True
    assert client.closed is True


@pytest.mark.parametrize("path", ["..", ".", "v1/../models", "v1/models/./secret"])
async def test_proxy_rejects_traversal_before_endpoint_or_network_access(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    resolve = AsyncMock(side_effect=AssertionError("endpoint resolved"))
    client_factory = Mock(side_effect=AssertionError("client created"))
    monkeypatch.setattr(proxy, "_resolve_upstream", resolve)
    monkeypatch.setattr(proxy, "_new_upstream_client", client_factory)

    with pytest.raises(HTTPException) as raised:
        await proxy._proxy(_request("GET"), "model", path)

    assert raised.value.status_code == 400
    assert raised.value.detail == "Invalid inference path"
    resolve.assert_not_awaited()
    client_factory.assert_not_called()


async def test_proxy_builds_bounded_request_and_streams_filtered_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFERENCE_PROXY_CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("INFERENCE_PROXY_READ_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("INFERENCE_PROXY_WRITE_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("INFERENCE_PROXY_POOL_TIMEOUT_SECONDS", "2.5")
    resolve = AsyncMock(return_value=("model", "gco-inference", "/readyz"))
    monkeypatch.setattr(proxy, "_resolve_upstream", resolve)
    upstream = _FakeUpstreamResponse(
        chunks=(b'{"token":', b'"ok"}'),
        status_code=207,
        headers=[
            ("content-type", "application/json"),
            ("content-length", "999"),
            ("transfer-encoding", "chunked"),
            ("x-upstream-request-id", "upstream-1"),
        ],
    )
    client = _FakeHTTPClient(upstream)
    factory = _install_http_client(monkeypatch, client)
    request = _request(
        "POST",
        query=b"tenant=alpha&tenant=beta&empty=",
        headers=[
            ("Content-Type", "application/json"),
            ("Accept", "application/json"),
            ("Authorization", "Bearer secret"),
            ("X-Request-ID", "request-1"),
            ("Connection", "keep-alive"),
        ],
        body=b'{"prompt":"hello"}',
    )

    streamed = await proxy._proxy(request, "model", "v1/chat/completions")

    resolve.assert_awaited_once_with("model")
    factory.assert_called_once_with()
    assert isinstance(streamed, proxy._UpstreamStreamingResponse)
    assert client.build_args == (
        "POST",
        "https://model.gco-inference.svc.cluster.local:8443/v1/chat/completions",
    )
    assert client.build_kwargs is not None
    timeout = client.build_kwargs.pop("timeout")
    assert isinstance(timeout, httpx2.Timeout)
    assert timeout.connect == 1.5
    assert timeout.read == 45.0
    assert timeout.write == 12.0
    assert timeout.pool == 2.5
    assert client.build_kwargs == {
        "params": [("tenant", "alpha"), ("tenant", "beta"), ("empty", "")],
        "headers": [
            ("content-type", "application/json"),
            ("accept", "application/json"),
            ("x-request-id", "request-1"),
        ],
        "content": b'{"prompt":"hello"}',
    }
    assert client.sent_request is client.built_request
    assert client.send_stream is True
    assert streamed.status_code == 207
    assert streamed.headers["content-type"] == "application/json"
    assert streamed.headers["x-upstream-request-id"] == "upstream-1"
    assert "content-length" not in streamed.headers
    assert "transfer-encoding" not in streamed.headers

    # The request's client stays open while its response streams ...
    assert client.closed is False
    assert [chunk async for chunk in streamed.body_iterator] == [b'{"token":', b'"ok"}']
    # ... and goes with the stream.
    assert upstream.closed is True
    assert client.closed is True


@pytest.mark.parametrize(
    ("path", "expected_suffix"),
    [
        ("", "/"),
        ("v1/models/model name", "/v1/models/model%20name"),
        ("v1/models/%2e%2e", "/v1/models/%252e%252e"),
    ],
)
async def test_proxy_constructs_root_and_percent_encoded_urls(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_suffix: str,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    upstream = _FakeUpstreamResponse()
    client = _FakeHTTPClient(upstream)
    _install_http_client(monkeypatch, client)

    streamed = await proxy._proxy(_request("GET"), "model", path)

    assert client.build_args == (
        "GET",
        f"https://model.gco-inference.svc.cluster.local:8443{expected_suffix}",
    )
    assert [chunk async for chunk in streamed.body_iterator] == []
    assert upstream.closed is True
    assert client.closed is True


@pytest.mark.parametrize(
    ("exception_type", "expected_status", "expected_detail", "logged"),
    [
        (httpx2.ReadTimeout, 504, "Inference endpoint timed out", False),
        (httpx2.ConnectError, 502, "Inference endpoint is unavailable", True),
    ],
)
async def test_proxy_maps_transport_failures_and_releases_the_client(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exception_type: type[httpx2.HTTPError],
    expected_status: int,
    expected_detail: str,
    logged: bool,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    upstream_request = httpx2.Request("POST", "https://upstream.invalid/v1/chat/completions")
    transport_error = exception_type("transport failed", request=upstream_request)
    client = _FakeHTTPClient(error=transport_error)
    _install_http_client(monkeypatch, client)

    with (
        caplog.at_level(logging.WARNING, logger=proxy.__name__),
        pytest.raises(HTTPException) as raised,
    ):
        await proxy._proxy(_request("POST"), "model", "v1/chat/completions")

    assert raised.value.status_code == expected_status
    assert raised.value.detail == expected_detail
    assert raised.value.__cause__ is transport_error
    # The failed request's own client is released before the error is reported.
    assert client.closed is True
    assert ("Inference upstream request failed" in caplog.text) is logged
    if logged:
        assert "service=model namespace=gco-inference error_type=ConnectError" in caplog.text


async def test_proxy_releases_the_client_when_the_request_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    build_error = ValueError("unencodable request")
    client = _FakeHTTPClient(build_error=build_error)
    _install_http_client(monkeypatch, client)

    with pytest.raises(ValueError, match="unencodable request") as raised:
        await proxy._proxy(_request("GET"), "model", "v1/models")

    # Unmapped failures propagate unchanged, and still release the client.
    assert raised.value is build_error
    assert client.sent_request is None
    assert client.closed is True


async def test_proxy_releases_the_upstream_when_the_response_cannot_be_relayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    upstream = _FakeUpstreamResponse(chunks=(b"{}",))
    client = _FakeHTTPClient(upstream)
    _install_http_client(monkeypatch, client)
    monkeypatch.setattr(
        proxy, "_response_headers", Mock(side_effect=RuntimeError("headers unusable"))
    )

    with pytest.raises(RuntimeError, match="headers unusable"):
        await proxy._proxy(_request("GET"), "model", "v1/models")

    assert upstream.iterated is False
    assert upstream.closed is True
    assert client.closed is True


async def test_proxy_releases_the_client_when_the_request_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    client = _FakeHTTPClient()
    _install_http_client(monkeypatch, client)

    task = asyncio.create_task(proxy._proxy(_request("POST"), "model", "v1/completions"))
    await asyncio.wait_for(client.send_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.closed is True
    assert proxy._cleanup_tasks == set()


async def test_every_request_gets_its_own_verified_no_keepalive_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = _UpstreamWire(monkeypatch)

    first = proxy._new_upstream_client()
    second = proxy._new_upstream_client()

    assert first is not second
    # Trust is looked up for every client, never captured once per process.
    assert wire.context_lookups == 2
    assert len(wire.transport_kwargs) == 2
    for kwargs in wire.transport_kwargs:
        # TLS trust and pool limits live on the transport: a client given
        # transport= ignores its own verify/limits.
        assert kwargs["verify"] is wire.context
        assert kwargs["limits"] == httpx2.Limits(max_connections=None, max_keepalive_connections=0)
        assert kwargs["trust_env"] is False
    # Each client's real transport went through the tracing seam.
    assert [type(transport.inner) for transport in wire.transports] == [
        _REAL_ASYNC_TRANSPORT,
        _REAL_ASYNC_TRANSPORT,
    ]
    for client in (first, second):
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert "accept-encoding" not in client.headers
        await client.aclose()
    assert all(transport.closed for transport in wire.transports)


async def test_proxy_relays_through_per_request_clients_with_only_caller_encodings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end over real clients: per-request timeout, no default encoding."""
    monkeypatch.setenv("INFERENCE_PROXY_READ_TIMEOUT_SECONDS", "45")
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    wire = _UpstreamWire(monkeypatch)
    wire.handler = lambda _request: _streamed(
        b"data: one\n\n",
        b"data: two\n\n",
        headers=[("content-type", "text/event-stream"), ("content-encoding", "br")],
    )

    encoded = await proxy._proxy(
        _request("POST", headers=[("Accept-Encoding", "br")], body=b"{}"),
        "model",
        "v1/chat/completions",
    )
    assert [chunk async for chunk in encoded.body_iterator] == [
        b"data: one\n\n",
        b"data: two\n\n",
    ]
    wire.handler = lambda _request: _streamed(b"{}")
    plain = await proxy._proxy(_request("GET"), "model", "v1/models")
    assert [chunk async for chunk in plain.body_iterator] == [b"{}"]

    first, second = wire.requests
    assert str(first.url) == (
        "https://model.gco-inference.svc.cluster.local:8443/v1/chat/completions"
    )
    assert first.headers["accept-encoding"] == "br"
    assert encoded.headers["content-encoding"] == "br"
    # Without a caller encoding the model is asked for identity bytes, which
    # is what the raw relay hands the caller.
    assert "accept-encoding" not in second.headers
    assert first.extensions["timeout"] == {
        "connect": 5.0,
        "read": 45.0,
        "write": 30.0,
        "pool": 5.0,
    }
    # One client per request, each closed with its stream.
    assert len(wire.transport_kwargs) == 2
    assert [transport.closed for transport in wire.transports] == [True, True]


async def test_a_rotated_internal_ca_is_trusted_from_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The upgrade case: the projected ca.crt changes under a running proxy."""
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(_ca_pem("GCO internal CA before rotation"))
    monkeypatch.setenv(internal_tls.INTERNAL_CA_FILE_ENV, str(ca_file))
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    wire = _UpstreamWire(monkeypatch, real_ca=True)

    async def proxied_trust() -> ssl.SSLContext:
        """Serve one request; return the context its client's transport verified with."""
        streamed = await proxy._proxy(_request("GET"), "model", "v1/models")
        assert [chunk async for chunk in streamed.body_iterator] == []
        verify = wire.transport_kwargs[-1]["verify"]
        assert isinstance(verify, ssl.SSLContext)
        return verify

    before = await proxied_trust()
    # An unchanged bundle is answered from the cache, not reloaded per request.
    assert await proxied_trust() is before

    # Kubernetes updates a projected Secret by swapping in a new file.
    staged = tmp_path / "ca.crt.staged"
    staged.write_bytes(_ca_pem("GCO internal CA after rotation"))
    os.replace(staged, ca_file)

    after = await proxied_trust()
    assert after is not before
    assert _trusted_common_names(before) == ["GCO internal CA before rotation"]
    assert _trusted_common_names(after) == ["GCO internal CA after rotation"]
    assert after.verify_mode is ssl.CERT_REQUIRED
    assert after.check_hostname is True
    assert len(wire.requests) == 3
    assert all(transport.closed for transport in wire.transports)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (httpx2.ReadTimeout("model timed out"), 504),
        (httpx2.ConnectError("certificate verify failed"), 502),
    ],
)
async def test_real_client_is_closed_when_the_upstream_request_fails(
    monkeypatch: pytest.MonkeyPatch,
    error: httpx2.HTTPError,
    expected_status: int,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    wire = _UpstreamWire(monkeypatch)

    def fail(request: httpx2.Request) -> httpx2.Response:
        raise error

    wire.handler = fail

    with pytest.raises(HTTPException) as raised:
        await proxy._proxy(_request("POST", body=b"{}"), "model", "v1/completions")

    assert raised.value.status_code == expected_status
    [transport] = wire.transports
    assert transport.closed is True


async def test_real_client_is_closed_when_the_request_is_cancelled_mid_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    wire = _UpstreamWire(monkeypatch)
    sent = asyncio.Event()

    async def never_answers(request: httpx2.Request) -> httpx2.Response:
        sent.set()
        await asyncio.Event().wait()
        return _streamed()

    wire.handler = never_answers

    task = asyncio.create_task(proxy._proxy(_request("GET"), "model", "v1/models"))
    await asyncio.wait_for(sent.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [transport] = wire.transports
    assert transport.closed is True


async def test_real_client_is_closed_when_the_stream_fails_mid_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    wire = _UpstreamWire(monkeypatch)

    async def truncated() -> AsyncIterator[bytes]:
        yield b"data: one\n\n"
        raise httpx2.RemoteProtocolError("peer closed connection mid-stream")

    wire.handler = lambda _request: httpx2.Response(200, content=truncated())

    streamed = await proxy._proxy(_request("POST", body=b"{}"), "model", "v1/completions")
    relayed: list[object] = []

    async def relay() -> None:
        async for chunk in streamed.body_iterator:
            relayed.append(chunk)

    with pytest.raises(httpx2.RemoteProtocolError, match="mid-stream"):
        await relay()

    assert relayed == [b"data: one\n\n"]
    [transport] = wire.transports
    assert transport.closed is True


async def test_proxy_fails_closed_without_the_internal_ca_and_retries_next_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    wire = _UpstreamWire(monkeypatch)
    missing = InternalTLSError("GCO internal CA bundle is not readable")
    monkeypatch.setattr(internal_tls, "internal_ssl_context", Mock(side_effect=missing))

    with (
        caplog.at_level(logging.ERROR, logger=proxy.__name__),
        pytest.raises(HTTPException) as raised,
    ):
        await proxy._proxy(_request("GET"), "model", "v1/models")

    assert raised.value.status_code == 502
    assert raised.value.detail == "Inference endpoint is unavailable"
    assert raised.value.__cause__ is missing
    assert "Inference upstream TLS trust is unavailable" in caplog.text
    # The CA is read before any transport or client exists: nothing to close.
    assert wire.transport_kwargs == []
    assert wire.transports == []
    assert wire.requests == []

    # Once the bundle is mounted the next request builds its client.
    monkeypatch.setattr(internal_tls, "internal_ssl_context", wire._context)
    streamed = await proxy._proxy(_request("GET"), "model", "v1/models")
    assert streamed.status_code == 200
    assert [chunk async for chunk in streamed.body_iterator] == []
    assert len(wire.requests) == 1
    [transport] = wire.transports
    assert transport.closed is True


async def test_route_wrappers_delegate_root_and_subpaths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("GET")
    root_response = object()
    path_response = object()
    proxy_call = AsyncMock(return_value=root_response)
    monkeypatch.setattr(proxy, "_proxy", proxy_call)

    assert await proxy.proxy_inference_root(request, "model") is root_response
    proxy_call.assert_awaited_once_with(request, "model")

    proxy_call.reset_mock()
    proxy_call.return_value = path_response
    assert await proxy.proxy_inference_path(request, "model", "v1/models") is path_response
    proxy_call.assert_awaited_once_with(request, "model", "v1/models")


def test_router_exposes_only_supported_methods() -> None:
    """Only GET, HEAD, and POST reach either proxy path.

    Methods are unioned per path because each verb is registered as its own
    route (see ``test_router_operation_ids_are_unique_per_method``), so a path
    legitimately appears once per supported method.
    """
    methods_by_path: dict[str, set[str]] = {}
    for route in proxy.router.routes:
        methods_by_path.setdefault(route.path, set()).update(route.methods)

    assert methods_by_path["/inference/{endpoint_name}"] == {"GET", "HEAD", "POST"}
    assert methods_by_path["/inference/{endpoint_name}/{upstream_path:path}"] == {
        "GET",
        "HEAD",
        "POST",
    }


def test_router_operation_ids_are_unique_per_method() -> None:
    """Each proxy route carries exactly one method so operationIds stay unique.

    FastAPI's ``generate_unique_id`` appends ``list(route.methods)[0]`` — one
    arbitrary member of an unordered set — and evaluates it once per route, so
    a single route serving GET, HEAD, and POST would emit three OpenAPI
    operations sharing one operationId. That violates the spec's uniqueness
    requirement and collides in generated clients, so the router registers one
    route per verb instead.
    """
    from fastapi.utils import generate_unique_id

    proxy_routes = [
        route
        for route in proxy.router.routes
        if getattr(route, "path", "").startswith("/inference/{endpoint_name}")
    ]
    assert proxy_routes, "expected the inference proxy routes to be registered"

    for route in proxy_routes:
        assert len(route.methods) == 1, (
            f"{route.path} serves {sorted(route.methods)}; a multi-method route "
            "yields duplicate operationIds"
        )

    operation_ids = [generate_unique_id(route) for route in proxy_routes]
    assert len(set(operation_ids)) == len(operation_ids), (
        f"duplicate operationIds generated: {sorted(operation_ids)}"
    )
