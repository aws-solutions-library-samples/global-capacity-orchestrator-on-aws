"""Upstream-facing behavior of the Mooncake PD proxy program.

The request-shaping helpers are pinned elsewhere; these checks drive the parts
of the proxy that talk to the prefill and decode Services and answer the admin
and health surfaces. Every test mocks the module-level outbound httpx client and
the URL / key globals through monkeypatch so the restore is automatic and no
state leaks across xdist workers, and no real network is ever touched. The
suite also pins explicit health handling, GET passthrough, query preservation,
and rejection of valid JSON values that are not request objects.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

import gco.services.mooncake_pd_proxy as proxy


class _FakeStreamResponse:
    """Stand-in for the streamed httpx response returned by _client.send.

    Exposes what _stream_decode consumes: an async aiter_raw chunk source, an
    async aclose that records that it ran, plus headers and status_code for the
    relayed StreamingResponse.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_type: str = "application/json",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = chunks
        self.headers = {"content-type": content_type, **(headers or {})}
        self.status_code = status_code
        self.closed = False

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _fake_request(
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    query: str = "",
):
    """A minimal stand-in for the Starlette Request the handlers read from."""

    async def _body() -> bytes:
        return body

    return SimpleNamespace(
        url=SimpleNamespace(path=path, query=query),
        headers=headers or {},
        body=_body,
    )


async def _drive(coro):
    """Await a handler returning a StreamingResponse and drain its body."""
    response = await coro
    chunks = [chunk async for chunk in response.body_iterator]
    return response, chunks


def test_prime_prefill_returns_empty_without_prefill_url(monkeypatch) -> None:
    """With no prefill backend configured, priming is skipped and returns empty."""
    client = MagicMock()
    client.post = AsyncMock()
    monkeypatch.setattr(proxy, "PREFILL_URL", "")
    monkeypatch.setattr(proxy, "_client", client)

    result = asyncio.run(proxy._prime_prefill("/v1/completions", {"prompt": "hi"}))

    assert result == {}
    client.post.assert_not_called()


def test_prime_prefill_returns_transfer_params_on_success(monkeypatch) -> None:
    """A successful prime relays the connector-supplied kv_transfer_params."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"kv_transfer_params": {"remote_block_ids": [1, 2]}}
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    monkeypatch.setattr(proxy, "PREFILL_URL", "http://ep-prefill:8000")
    monkeypatch.setattr(proxy, "_client", client)

    result = asyncio.run(proxy._prime_prefill("/v1/completions", {"prompt": "hi"}))

    assert result == {"remote_block_ids": [1, 2]}
    client.post.assert_awaited_once()


def test_prime_prefill_returns_empty_when_response_has_no_params(monkeypatch) -> None:
    """A prime that returns no transfer params degrades to empty (decode self-serves)."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": []}
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    monkeypatch.setattr(proxy, "PREFILL_URL", "http://ep-prefill:8000")
    monkeypatch.setattr(proxy, "_client", client)

    result = asyncio.run(proxy._prime_prefill("/v1/completions", {"prompt": "hi"}))

    assert result == {}


def test_prime_prefill_swallows_errors_and_returns_empty(monkeypatch) -> None:
    """Priming is best-effort: any upstream error is logged and yields empty."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=RuntimeError("prefill exploded"))
    monkeypatch.setattr(proxy, "PREFILL_URL", "http://ep-prefill:8000")
    monkeypatch.setattr(proxy, "_client", client)

    result = asyncio.run(proxy._prime_prefill("/v1/completions", {"prompt": "hi"}))

    assert result == {}


def test_stream_decode_rejects_when_no_decode_backend(monkeypatch) -> None:
    """A connect failure (no Ready decode endpoint) becomes a stable 503."""
    client = MagicMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(side_effect=httpx.ConnectError("no route to decode"))
    monkeypatch.setattr(proxy, "DECODE_URL", "http://ep-decode:8000")
    monkeypatch.setattr(proxy, "NO_DECODE_STATUS", 503)
    monkeypatch.setattr(proxy, "NO_DECODE_MESSAGE", "no available decode backend")
    monkeypatch.setattr(proxy, "_client", client)

    response = asyncio.run(proxy._stream_decode("POST", "/v1/completions", {"stream": True}))

    assert isinstance(response, proxy.JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "no_decode_backend"
    assert payload["error"]["message"] == "no available decode backend"


def test_stream_decode_streams_decode_response(monkeypatch) -> None:
    """The decode body is relayed chunk-for-chunk and the upstream is closed."""
    upstream = _FakeStreamResponse(
        [b"hello ", b"world"], content_type="application/x-ndjson", status_code=200
    )
    client = MagicMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(return_value=upstream)
    monkeypatch.setattr(proxy, "DECODE_URL", "http://ep-decode:8000")
    monkeypatch.setattr(proxy, "_client", client)

    response, chunks = asyncio.run(
        _drive(proxy._stream_decode("POST", "/v1/chat/completions", {"stream": False}))
    )

    assert isinstance(response, proxy.StreamingResponse)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert chunks == [b"hello ", b"world"]
    assert upstream.closed is True
    client.send.assert_awaited_once()


def test_stream_decode_uses_event_stream_media_type_when_streaming(monkeypatch) -> None:
    """A streaming request is served as text/event-stream regardless of upstream type."""
    upstream = _FakeStreamResponse([b"data: tok\n\n"], content_type="application/json")
    client = MagicMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(return_value=upstream)
    monkeypatch.setattr(proxy, "DECODE_URL", "http://ep-decode:8000")
    monkeypatch.setattr(proxy, "_client", client)

    response, chunks = asyncio.run(
        _drive(proxy._stream_decode("POST", "/v1/completions", {"stream": True}))
    )

    assert response.headers["content-type"] == "text/event-stream"
    assert chunks == [b"data: tok\n\n"]
    assert upstream.closed is True


def test_admin_add_rejects_when_no_key_configured(monkeypatch) -> None:
    """With no ADMIN_API_KEY configured the admin surface refuses every caller."""
    monkeypatch.setattr(proxy, "ADMIN_API_KEY", "")
    request = _fake_request("/instances/add", headers={"x-admin-api-key": "anything"})

    response = asyncio.run(proxy._admin_add(request))

    assert response.status_code == 403


def test_admin_add_rejects_wrong_key(monkeypatch) -> None:
    """A mismatched key is forbidden even when an admin key is configured."""
    monkeypatch.setattr(proxy, "ADMIN_API_KEY", "secret")
    request = _fake_request("/instances/add", headers={"x-admin-api-key": "nope"})

    response = asyncio.run(proxy._admin_add(request))

    assert response.status_code == 403


def test_admin_add_accepts_matching_header_key(monkeypatch) -> None:
    """The matching x-admin-api-key header authorizes the call."""
    monkeypatch.setattr(proxy, "ADMIN_API_KEY", "secret")
    request = _fake_request("/instances/add", headers={"x-admin-api-key": "secret"})

    response = asyncio.run(proxy._admin_add(request))

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok"}


def test_admin_add_accepts_bearer_token(monkeypatch) -> None:
    """A Bearer Authorization header is also honored as the admin key."""
    monkeypatch.setattr(proxy, "ADMIN_API_KEY", "secret")
    request = _fake_request("/instances/add", headers={"authorization": "Bearer secret"})

    response = asyncio.run(proxy._admin_add(request))

    assert response.status_code == 200


def test_dispatch_routes_admin_path_to_admin_handler(monkeypatch) -> None:
    """A request whose path ends in the admin suffix is handed to the admin guard."""
    monkeypatch.setattr(proxy, "ADMIN_API_KEY", "secret")
    request = _fake_request("/inference/ep/instances/add", headers={"x-admin-api-key": "secret"})

    response = asyncio.run(proxy._dispatch("inference/ep/instances/add", request))

    assert response.status_code == 200


def test_header_policy_matches_authenticated_proxy_boundary() -> None:
    """The standalone ConfigMap program mirrors the outer proxy allowlist."""
    from gco.services.api_routes import inference_proxy

    assert proxy._HOP_BY_HOP_HEADERS == inference_proxy._HOP_BY_HOP_HEADERS
    assert (
        inference_proxy._ALLOWED_REQUEST_HEADERS - {"content-encoding"}
        == proxy._ALLOWED_REQUEST_HEADERS
    )


def test_request_target_without_query() -> None:
    """A request without a query string keeps only its path."""
    assert proxy._request_target(_fake_request("/v1/models")) == "/v1/models"


def test_dispatch_rejects_invalid_json_body() -> None:
    """A body that is not valid JSON is rejected with a 400 before any upstream call."""
    request = _fake_request("/v1/completions", body=b"{not valid json")

    response = asyncio.run(proxy._dispatch("v1/completions", request))

    assert isinstance(response, proxy.JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "invalid JSON body"}


@pytest.mark.parametrize("body", [b"null", b"[]", b'"text"', b"1", b"true"])
def test_dispatch_rejects_non_object_json(body: bytes) -> None:
    """Valid JSON scalars and arrays are client errors, not internal failures."""
    request = _fake_request("/v1/completions", body=body)

    response = asyncio.run(proxy._dispatch("v1/completions", request))

    assert isinstance(response, proxy.JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "JSON body must be an object"}


def test_dispatch_passes_non_serving_post_straight_to_decode(monkeypatch) -> None:
    """A non-generation POST skips prefill and preserves its query string."""
    upstream = _FakeStreamResponse([b'{"data": []}'])
    client = MagicMock()
    client.post = AsyncMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(return_value=upstream)
    monkeypatch.setattr(proxy, "PREFILL_URL", "http://ep-prefill:8000")
    monkeypatch.setattr(proxy, "DECODE_URL", "http://ep-decode:8000")
    monkeypatch.setattr(proxy, "_client", client)
    request = _fake_request("/v1/models", body=b"{}", query="limit=5")

    response, chunks = asyncio.run(_drive(proxy._dispatch("v1/models", request)))

    assert isinstance(response, proxy.StreamingResponse)
    assert chunks == [b'{"data": []}']
    client.post.assert_not_called()
    client.build_request.assert_called_once_with(
        "POST",
        "http://ep-decode:8000/v1/models?limit=5",
        json={},
        headers=[],
    )
    client.send.assert_awaited_once()


def test_dispatch_primes_prefill_then_streams_decode_for_serving_path(monkeypatch) -> None:
    """A serving path primes prefill first, then streams the decode response back."""
    prefill_resp = MagicMock()
    prefill_resp.raise_for_status.return_value = None
    prefill_resp.json.return_value = {"kv_transfer_params": {"remote_block_ids": [9]}}
    upstream = _FakeStreamResponse([b"data: hi\n\n"])
    client = MagicMock()
    client.post = AsyncMock(return_value=prefill_resp)
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(return_value=upstream)
    monkeypatch.setattr(proxy, "PREFILL_URL", "http://ep-prefill:8000")
    monkeypatch.setattr(proxy, "DECODE_URL", "http://ep-decode:8000")
    monkeypatch.setattr(proxy, "_client", client)
    request = _fake_request(
        "/v1/completions",
        body=json.dumps({"prompt": "hi", "stream": True}).encode(),
        query="trace=request-1",
    )

    response, chunks = asyncio.run(_drive(proxy._dispatch("v1/completions", request)))

    assert isinstance(response, proxy.StreamingResponse)
    assert chunks == [b"data: hi\n\n"]
    client.post.assert_awaited_once()
    client.build_request.assert_called_once()
    assert client.build_request.call_args.args == (
        "POST",
        "http://ep-decode:8000/v1/completions?trace=request-1",
    )
    client.send.assert_awaited_once()


def test_health_endpoints_remain_local(monkeypatch) -> None:
    """Explicit liveness routes win over the GET passthrough."""
    outbound = MagicMock()
    outbound.send = AsyncMock()
    monkeypatch.setattr(proxy, "_client", outbound)

    with TestClient(proxy.app) as client:
        for path in ("/health", "/healthz"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    outbound.send.assert_not_called()


def test_get_passthrough_preserves_safe_transport_metadata(monkeypatch) -> None:
    """Model discovery keeps safe headers and strips hop-by-hop framing."""
    upstream = _FakeStreamResponse(
        [b"compressed-model-list"],
        headers={
            "cache-control": "max-age=30",
            "content-encoding": "gzip",
            "content-length": "21",
            "connection": "keep-alive",
            "etag": '"models-v1"',
            "x-request-id": "decode-request",
        },
    )
    client = MagicMock()
    client.build_request = MagicMock(return_value="REQ")
    client.send = AsyncMock(return_value=upstream)
    monkeypatch.setattr(proxy, "DECODE_URL", "http://ep-decode:8000")
    monkeypatch.setattr(proxy, "_client", client)
    request = _fake_request(
        "/v1/models",
        query="limit=5",
        headers={
            "accept": "application/json",
            "accept-encoding": "gzip",
            "authorization": "Bearer must-not-forward",
            "host": "public.example",
            "if-none-match": '"models-v1"',
            "x-request-id": "outer-request",
        },
    )

    response, chunks = asyncio.run(_drive(proxy._get_passthrough("v1/models", request)))

    assert response.status_code == 200
    assert chunks == [b"compressed-model-list"]
    client.build_request.assert_called_once_with(
        "GET",
        "http://ep-decode:8000/v1/models?limit=5",
        headers=[
            ("accept", "application/json"),
            ("accept-encoding", "gzip"),
            ("if-none-match", '"models-v1"'),
            ("x-request-id", "outer-request"),
        ],
    )
    client.send.assert_awaited_once_with("REQ", stream=True)
    assert response.headers["cache-control"] == "max-age=30"
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["etag"] == '"models-v1"'
    assert response.headers["x-request-id"] == "decode-request"
    assert "content-length" not in response.headers
    assert "connection" not in response.headers
