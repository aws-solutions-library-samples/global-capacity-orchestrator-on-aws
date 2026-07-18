"""Focused security and streaming tests for the managed inference proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gco.services.api_routes import inference_proxy as proxy


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
        self.headers = httpx.Headers(headers or [])
        self.stream_error = stream_error
        self.closed = False

    async def aiter_raw(self) -> AsyncIterator[bytes]:
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
        error: httpx.HTTPError | None = None,
    ):
        self.response = response
        self.error = error
        self.build_args: tuple[str, str] | None = None
        self.build_kwargs: dict[str, object] | None = None
        self.built_request = object()
        self.sent_request: object | None = None
        self.send_stream: bool | None = None
        self.closed = False

    def build_request(self, method: str, url: str, **kwargs: object) -> object:
        self.build_args = (method, url)
        self.build_kwargs = kwargs
        return self.built_request

    async def send(self, request: object, *, stream: bool = False) -> _FakeUpstreamResponse:
        self.sent_request = request
        self.send_stream = stream
        if self.error is not None:
            raise self.error
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
) -> dict[str, object]:
    constructor_kwargs: dict[str, object] = {}

    def factory(**kwargs: object) -> _FakeHTTPClient:
        constructor_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)
    return constructor_kwargs


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
        ("v1/chat/completions", "POST", "/health", "v1/chat/completions"),
        ("v1/completions", "POST", "/health", "v1/completions"),
        ("v1/embeddings", "POST", "/health", "v1/embeddings"),
        ("v1/responses", "POST", "/health", "v1/responses"),
        ("generate", "POST", "/health", "generate"),
        ("generate_stream", "POST", "/health", "generate_stream"),
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


async def test_stream_response_yields_raw_chunks_and_closes_both_resources() -> None:
    response = _FakeUpstreamResponse(chunks=(b"first", b"second"))
    client = _FakeHTTPClient(response)

    chunks = [chunk async for chunk in proxy._stream_response(response, client)]

    assert chunks == [b"first", b"second"]
    assert response.closed is True
    assert client.closed is True


async def test_stream_response_closes_resources_when_upstream_iteration_fails() -> None:
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
    client_closed = asyncio.Event()

    class BlockingResponse:
        async def aiter_raw(self) -> AsyncIterator[bytes]:
            yield b"chunk"

        async def aclose(self) -> None:
            response_close_started.set()
            await release_response_close.wait()

    class TrackingClient:
        async def aclose(self) -> None:
            client_closed.set()

    stream = proxy._stream_response(BlockingResponse(), TrackingClient())
    assert await anext(stream) == b"chunk"

    close_task = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(response_close_started.wait(), timeout=1)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    release_response_close.set()
    await asyncio.wait_for(client_closed.wait(), timeout=1)


@pytest.mark.parametrize("path", ["..", ".", "v1/../models", "v1/models/./secret"])
async def test_proxy_rejects_traversal_before_endpoint_or_network_access(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    resolve = AsyncMock(side_effect=AssertionError("endpoint resolved"))
    client_factory = Mock(side_effect=AssertionError("client created"))
    monkeypatch.setattr(proxy, "_resolve_upstream", resolve)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", client_factory)

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
    constructor_kwargs = _install_http_client(monkeypatch, client)
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
    assert constructor_kwargs["follow_redirects"] is False
    assert constructor_kwargs["trust_env"] is False
    timeout = constructor_kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 1.5
    assert timeout.read == 45.0
    assert timeout.write == 12.0
    assert timeout.pool == 2.5
    assert client.build_args == (
        "POST",
        "http://model.gco-inference.svc.cluster.local/v1/chat/completions",
    )
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

    assert [chunk async for chunk in streamed.body_iterator] == [b'{"token":', b'"ok"}']
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
        f"http://model.gco-inference.svc.cluster.local{expected_suffix}",
    )
    assert [chunk async for chunk in streamed.body_iterator] == []
    assert upstream.closed is True
    assert client.closed is True


@pytest.mark.parametrize(
    ("exception_type", "expected_status", "expected_detail"),
    [
        (httpx.ReadTimeout, 504, "Inference endpoint timed out"),
        (httpx.ConnectError, 502, "Inference endpoint is unavailable"),
    ],
)
async def test_proxy_maps_transport_failures_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[httpx.HTTPError],
    expected_status: int,
    expected_detail: str,
) -> None:
    monkeypatch.setattr(
        proxy,
        "_resolve_upstream",
        AsyncMock(return_value=("model", "gco-inference", "/health")),
    )
    upstream_request = httpx.Request("POST", "http://upstream.invalid/v1/chat/completions")
    transport_error = exception_type("transport failed", request=upstream_request)
    client = _FakeHTTPClient(error=transport_error)
    _install_http_client(monkeypatch, client)

    with pytest.raises(HTTPException) as raised:
        await proxy._proxy(_request("POST"), "model", "v1/chat/completions")

    assert raised.value.status_code == expected_status
    assert raised.value.detail == expected_detail
    assert raised.value.__cause__ is transport_error
    assert client.closed is True


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
    methods_by_path = {route.path: route.methods for route in proxy.router.routes}

    assert methods_by_path["/inference/{endpoint_name}"] == {"GET", "HEAD", "POST"}
    assert methods_by_path["/inference/{endpoint_name}/{upstream_path:path}"] == {
        "GET",
        "HEAD",
        "POST",
    }
