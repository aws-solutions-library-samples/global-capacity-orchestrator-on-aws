"""Mooncake prefill-decode (PD) proxy for disaggregated inference endpoints.

This is the program the ``{name}-proxy`` pod runs. It is shipped to the pod as a
ConfigMap (the monitor reads this file's own source and mounts it at
``/etc/pd-proxy/mooncake_pd_proxy.py``), and the proxy container runs it with
``python3 /etc/pd-proxy/mooncake_pd_proxy.py``. It therefore must depend only on
what the upstream ``vllm/vllm-openai`` image already ships — ``fastapi``,
``uvicorn`` and ``httpx`` — must run on that image's Python 3.12, and must not
import anything from the ``gco`` package. It uses ``httpx2`` when the image
provides it (as GCO's own environment does) and otherwise the image's
``httpx``; the two share the API used here.

The proxy binds ``PD_PROXY_HOST`` (loopback in the pod, behind the pod's TLS
sidecar) and reaches prefill and decode over verified HTTPS: with
``PD_PROXY_CA_FILE`` set it trusts exactly that CA bundle (the GCO internal
CA), and hostname verification stays on.

Per request on the public ``/v1/*`` serving paths it:

1. Treats the prompt as not resident in the shared store. The residency check is
   non-blocking and bounded by ``PD_PROXY_RESIDENCY_TIMEOUT_SECONDS``; a miss or a
   check that does not finish in time is sent straight to prefill, so a slow or
   unreachable store never holds the request.
2. Primes a prefill pod with the request at ``max_tokens=1`` and
   ``kv_transfer_params={"do_remote_decode": true}`` so prefill computes and
   exports the prompt KV through the MooncakeConnector.
3. Sends the original request to a decode pod, relaying any ``kv_transfer_params``
   the prefill step returned (with ``do_remote_prefill=true``) so decode pulls the
   KV instead of recomputing, and streams the decode response back to the client.

Non-health GET requests (for example OpenAI-compatible ``/v1/models`` discovery)
and non-generation POST requests pass through to decode with their query string
preserved. JSON request bodies must be objects; arrays and scalars are rejected
with a client error before either backend is called.

Prefill and decode are addressed through their in-cluster Services, so kube-proxy
load-balances across only the Ready role pods. When the decode Service has no
Ready endpoints the proxy rejects the request with a stable 503 rather than
emitting partial output. The privileged ``/instances/add`` admin path requires
the ``ADMIN_API_KEY`` header and is never published on the public Ingress.

The ``kv_transfer_params`` handshake is best-effort and pass-through: the proxy
sets only the outer ``do_remote_decode`` / ``do_remote_prefill`` flags and relays
whatever inner fields the connector returns, so it does not hard-code a
connector-version-specific schema. If prefill returns no transfer params (or the
priming call fails), the decode request is still served correctly — the connector
falls back to its own KV matching or decode recomputes — so the invoke path keeps
working either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-28T07:34:51Z
# Generated from Git commit: 95213a3dfe214f41ea8e3977b79711b1be061ac0
# Flowchart(s) generated from this file:
#   * ``_dispatch`` -> ``diagrams/code_diagrams/gco/services/mooncake_pd_proxy._dispatch.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/mooncake_pd_proxy._dispatch.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


try:
    import httpx2 as httpx  # type: ignore[import-not-found,unused-ignore]
except ImportError:
    import httpx  # type: ignore[no-redef,unused-ignore]


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [mooncake-pd-proxy] %(message)s"
)
logger = logging.getLogger("mooncake-pd-proxy")

HOST = os.environ.get("PD_PROXY_HOST", "0.0.0.0")
PORT = int(os.environ.get("PD_PROXY_PORT", "8000"))
PREFILL_URL = os.environ.get("PD_PROXY_PREFILL_URL", "").rstrip("/")
DECODE_URL = os.environ.get("PD_PROXY_DECODE_URL", "").rstrip("/")
CA_FILE = os.environ.get("PD_PROXY_CA_FILE", "").strip()
RESIDENCY_TIMEOUT = float(os.environ.get("PD_PROXY_RESIDENCY_TIMEOUT_SECONDS", "2"))
NO_DECODE_STATUS = int(os.environ.get("PD_PROXY_NO_DECODE_BACKEND_STATUS", "503"))
NO_DECODE_MESSAGE = os.environ.get(
    "PD_PROXY_NO_DECODE_BACKEND_MESSAGE", "no available decode backend"
)
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ADMIN_PATH = "/instances/add"

# Per-request upstream timeout. Connect is kept short so an endpoint with no
# Ready decode pods (empty Service endpoints) surfaces quickly as a 503 rather
# than hanging the client; reads are unbounded for long generations.
_TIMEOUT = httpx.Timeout(None, connect=5.0)

# Keep this allowlist aligned with the authenticated inference proxy's public
# boundary. ``content-encoding`` is intentionally excluded here: this proxy
# parses and re-serializes JSON, so forwarding the original encoding would
# falsely describe the new body bytes.
_ALLOWED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "cache-control",
        "content-type",
        "idempotency-key",
        "if-match",
        "if-none-match",
        "prefer",
        "range",
        "user-agent",
        "x-request-id",
    }
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


#: The trust context for the CA file as it was last read, keyed on the file's
#: identity (inode, modification time, size).
_verify_cache: tuple[tuple[int, int, int], ssl.SSLContext] | None = None


def _upstream_verify() -> ssl.SSLContext | bool:
    """TLS trust for the prefill and decode hops, re-read when the CA changes.

    With ``PD_PROXY_CA_FILE`` set, trust exactly that bundle and nothing from
    the system store; hostname verification stays on, so each role Service
    must present a certificate for the name the proxy dialled. Unset keeps the
    library default for plain ``http://`` backends and local runs.

    The context is cached on the file's identity. Kubernetes updates a
    projected Secret by swapping a symlink, which changes the resolved inode
    and modification time, so a rotated internal CA is trusted from the next
    request on without restarting the pod, and an unchanged one costs a
    ``stat``. A bundle that is missing or is not a CA raises ``OSError``
    (``ssl.SSLError`` is one): at import that stops the program rather than
    serving over a weaker trust, and per request it answers like an
    unreachable decode backend.
    """
    global _verify_cache
    if not CA_FILE:
        return True
    info = os.stat(CA_FILE)
    identity = (info.st_ino, info.st_mtime_ns, info.st_size)
    cached = _verify_cache
    if cached is not None and cached[0] == identity:
        return cached[1]
    context = ssl.create_default_context(cafile=CA_FILE)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    _verify_cache = (identity, context)
    return context


def _new_client() -> httpx.AsyncClient:
    """Build the client for one upstream call; the caller closes it.

    A client per call takes its trust from :func:`_upstream_verify` when the
    call starts, which is what lets a rotated CA take effect without a
    restart; a process-wide client would verify against the bundle it loaded
    first until the pod restarted. Keep-alive stays off: kube-proxy balances
    connections, not requests, so a pooled connection would pin every request
    to one prefill or decode pod. The library's default ``Accept-Encoding`` is
    removed because the proxy relays decode's raw bytes: only an encoding the
    caller asked for (forwarded by :func:`_request_headers`) may shape the
    body the client receives.
    """
    client = httpx.AsyncClient(
        timeout=_TIMEOUT,
        verify=_upstream_verify(),
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    del client.headers["accept-encoding"]
    return client


# Fail at start on a configured bundle that cannot be used.
_upstream_verify()

app = FastAPI()

#: Upstream closes still running. The event loop holds only weak references
#: to tasks, and a close outlives its awaiter whenever the request that
#: started it is cancelled, so each one is held here until it finishes.
_closing: set[asyncio.Task[None]] = set()


async def _close_upstream(response: httpx.Response | None, client: httpx.AsyncClient) -> None:
    """Close one call's upstream response, if it got one, then its client."""
    try:
        if response is not None:
            await response.aclose()
    finally:
        await client.aclose()


async def _release_upstream(response: httpx.Response | None, client: httpx.AsyncClient) -> None:
    """Close one call's upstream in its own task, shielded from cancellation.

    Cancelling the request (the caller went away) interrupts only this wait,
    never the close itself, so a cancelled request still releases its
    connection. Both closes are idempotent, so releasing twice is harmless.
    """
    closing = asyncio.create_task(_close_upstream(response, client))
    _closing.add(closing)
    closing.add_done_callback(_closing.discard)
    await asyncio.shield(closing)


@app.get("/healthz")
@app.get("/health")
async def _health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _is_serving_path(path: str) -> bool:
    """True for the OpenAI-compatible serving paths the proxy disaggregates."""
    return path.endswith(("/completions", "/chat/completions", "/embeddings"))


def _prefill_body(body: dict[str, Any]) -> dict[str, Any]:
    """Body for priming prefill: one token, no stream, request remote decode."""
    pf = dict(body)
    pf["stream"] = False
    pf["max_tokens"] = 1
    if "max_completion_tokens" in pf:
        pf["max_completion_tokens"] = 1
    kvp = dict(pf.get("kv_transfer_params") or {})
    kvp["do_remote_decode"] = True
    kvp["do_remote_prefill"] = False
    pf["kv_transfer_params"] = kvp
    return pf


def _decode_body(body: dict[str, Any], prefill_kv_params: dict[str, Any]) -> dict[str, Any]:
    """Body for decode: original request, relaying prefill's transfer params."""
    dc = dict(body)
    if prefill_kv_params:
        kvp = dict(prefill_kv_params)
        kvp["do_remote_prefill"] = True
        kvp["do_remote_decode"] = False
        dc["kv_transfer_params"] = kvp
    return dc


async def _prime_prefill(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Run the prefill step; return its kv_transfer_params (best-effort)."""
    if not PREFILL_URL:
        return {}
    try:
        client = _new_client()
        try:
            resp = await client.post(f"{PREFILL_URL}{path}", json=_prefill_body(body))
        finally:
            await client.aclose()
        resp.raise_for_status()
        data = resp.json()
        return data.get("kv_transfer_params") or {}
    except Exception as exc:  # priming is best-effort
        logger.warning("prefill priming failed; decode will serve directly: %s", exc)
        return {}


def _request_target(request: Request) -> str:
    """Return the path and query string exactly as they should reach decode."""
    path = request.url.path
    return f"{path}?{request.url.query}" if request.url.query else path


def _request_headers(request: Request) -> list[tuple[bytes, bytes]]:
    """Forward only explicitly supported end-to-end model headers, as sent.

    Raw ASGI bytes, not Starlette's latin-1 decoded strings: httpx encodes a
    ``str`` header value as ASCII, so one non-ASCII byte would fail the
    request instead of reaching decode unchanged.
    """
    return [
        (name.lower(), value)
        for name, value in request.headers.raw
        if name.lower().decode("latin-1") in _ALLOWED_REQUEST_HEADERS
    ]


def _response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    """Relay end-to-end metadata byte for byte while dropping hop-by-hop framing.

    Starlette would re-encode ``str`` values as latin-1, failing a UTF-8 value
    outside latin-1 and transcoding one inside it; raw bytes keep each value
    exact and each repeated header on its own line. Names are lowercased, as
    ASGI requires.
    """
    blocked = _HOP_BY_HOP_HEADERS | {"content-length"}
    relayed: list[tuple[bytes, bytes]] = []
    for name, value in response.headers.raw:
        lowered = name.lower()
        if lowered.decode("latin-1") not in blocked:
            relayed.append((lowered, value))
    return relayed


def _no_decode_backend() -> JSONResponse:
    """The stable answer for a decode backend the proxy cannot reach."""
    return JSONResponse(
        {"error": {"message": NO_DECODE_MESSAGE, "type": "no_decode_backend"}},
        status_code=NO_DECODE_STATUS,
    )


async def _relay_decode(
    response: httpx.Response, client: httpx.AsyncClient
) -> AsyncIterator[bytes]:
    """Yield decode's body unchanged, then release the call's upstream."""
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await _release_upstream(response, client)


class _DecodeStreamingResponse(StreamingResponse):
    """Relay one decode response, releasing its client however the relay ends.

    The body generator releases the upstream when the stream finishes, fails,
    or is cancelled mid-body. Starlette starts that generator only once the
    headers are on their way, though: a caller who disconnected while decode
    was still answering cancels the response before its first chunk, so the
    response itself releases the upstream again once it is done (a no-op
    after the generator's release).
    """

    def __init__(
        self, response: httpx.Response, client: httpx.AsyncClient, *, want_stream: bool
    ) -> None:
        relayed = _response_headers(response)
        upstream_type = next(
            (value for name, value in relayed if name == b"content-type"), b"application/json"
        )
        headers = [(name, value) for name, value in relayed if name != b"content-type"]
        headers.append((b"content-type", b"text/event-stream" if want_stream else upstream_type))
        super().__init__(
            _relay_decode(response, client), status_code=response.status_code, media_type=None
        )
        # Assigned rather than passed as ``headers=``, which re-encodes ``str``
        # values as latin-1 (see _response_headers).
        self.raw_headers = headers
        self._upstream = (response, client)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _release_upstream(*self._upstream)


async def _stream_decode(
    method: str,
    target: str,
    body: dict[str, Any] | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Response:
    """Forward one request to decode and stream its response to the client."""
    want_stream = bool(body and body.get("stream"))
    url = f"{DECODE_URL}{target}"
    request_kwargs: dict[str, Any] = {}
    if body is not None:
        request_kwargs["json"] = body
    if headers is not None:
        request_kwargs["headers"] = headers
    try:
        client = _new_client()
    except OSError as exc:
        # The CA bundle is missing or unusable (ssl.SSLError is an OSError):
        # fail closed, like a decode backend the proxy cannot verify.
        logger.warning("decode backend trust is unavailable: %s", exc)
        return _no_decode_backend()

    # Until the streaming response owns the upstream, every exit releases it.
    resp: httpx.Response | None = None
    try:
        upstream_request = client.build_request(method, url, **request_kwargs)
        resp = await client.send(upstream_request, stream=True)
        return _DecodeStreamingResponse(resp, client, want_stream=want_stream)
    except httpx.ConnectError as exc:
        # No Ready decode endpoint behind the Service (or a TLS handshake the
        # decode sidecar failed): reject with a stable status instead of
        # emitting any partial output, and log why for the operator.
        await _release_upstream(resp, client)
        logger.warning("decode backend unreachable: %s", exc)
        return _no_decode_backend()
    except BaseException:
        # Anything else, a cancelled request included, releases it too.
        await _release_upstream(resp, client)
        raise


@app.post(ADMIN_PATH)
async def _admin_add(request: Request) -> JSONResponse:
    """Privileged admin endpoint, guarded by the ADMIN_API_KEY header.

    Routing is via the prefill/decode Services, so kube-proxy already tracks
    Ready pods and no per-pod registration is required; this endpoint exists so
    the admin surface is present and authenticated (and kept off the public
    Ingress), returning 200 for an authorized caller.
    """
    provided = (
        request.headers.get("x-admin-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if not ADMIN_API_KEY or provided != ADMIN_API_KEY:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"status": "ok"})


@app.api_route("/{full_path:path}", methods=["GET"])
async def _get_passthrough(full_path: str, request: Request) -> Response:
    """Forward non-health GETs, including OpenAI-compatible model discovery."""
    return await _stream_decode(
        "GET",
        _request_target(request),
        headers=_request_headers(request),
    )


@app.api_route("/{full_path:path}", methods=["POST"])
async def _dispatch(full_path: str, request: Request) -> Any:
    """Disaggregate one serving request: prime prefill, then stream decode."""
    path = request.url.path
    if path.endswith(ADMIN_PATH):
        return await _admin_add(request)

    raw = await request.body()
    try:
        decoded = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(decoded, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

    request_headers = _request_headers(request)
    target = _request_target(request)
    if not _is_serving_path(path):
        return await _stream_decode("POST", target, decoded, request_headers)

    # Residency check: non-blocking, treated as a miss so the prompt always goes
    # to prefill first (the store is never on the request's critical path).
    prefill_kv_params = await _prime_prefill(path, decoded)
    return await _stream_decode(
        "POST",
        target,
        _decode_body(decoded, prefill_kv_params),
        request_headers,
    )


if __name__ == "__main__":
    logger.info(
        "starting PD proxy on %s:%d (prefill=%s decode=%s)", HOST, PORT, PREFILL_URL, DECODE_URL
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
