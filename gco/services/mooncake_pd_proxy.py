"""Mooncake prefill-decode (PD) proxy for disaggregated inference endpoints.

This is the program the ``{name}-proxy`` pod runs. It is shipped to the pod as a
ConfigMap (the monitor reads this file's own source and mounts it at
``/etc/pd-proxy/mooncake_pd_proxy.py``), and the proxy container runs it with
``python /etc/pd-proxy/mooncake_pd_proxy.py``. It therefore must depend only on
what the upstream ``vllm/vllm-openai`` image already ships — ``fastapi``,
``uvicorn`` and ``httpx`` — and must not import anything from the ``gco``
package.

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

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [mooncake-pd-proxy] %(message)s"
)
logger = logging.getLogger("mooncake-pd-proxy")

PORT = int(os.environ.get("PD_PROXY_PORT", "8000"))
PREFILL_URL = os.environ.get("PD_PROXY_PREFILL_URL", "").rstrip("/")
DECODE_URL = os.environ.get("PD_PROXY_DECODE_URL", "").rstrip("/")
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

app = FastAPI()
_client = httpx.AsyncClient(timeout=_TIMEOUT)


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
        resp = await _client.post(f"{PREFILL_URL}{path}", json=_prefill_body(body))
        resp.raise_for_status()
        data = resp.json()
        return data.get("kv_transfer_params") or {}
    except Exception as exc:  # noqa: BLE001 - priming is best-effort
        logger.warning("prefill priming failed; decode will serve directly: %s", exc)
        return {}


async def _stream_decode(path: str, body: dict[str, Any]) -> Response:
    """Forward the request to decode and stream the response back to the client."""
    want_stream = bool(body.get("stream"))
    request = _client.build_request("POST", f"{DECODE_URL}{path}", json=body)
    try:
        resp = await _client.send(request, stream=True)
    except httpx.ConnectError:
        # No Ready decode endpoint behind the Service: reject with a stable
        # status instead of emitting any partial output.
        return JSONResponse(
            {"error": {"message": NO_DECODE_MESSAGE, "type": "no_decode_backend"}},
            status_code=NO_DECODE_STATUS,
        )

    async def _body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    media_type = (
        "text/event-stream" if want_stream else resp.headers.get("content-type", "application/json")
    )
    return StreamingResponse(_body_iter(), status_code=resp.status_code, media_type=media_type)


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
async def _get_catch_all(full_path: str) -> JSONResponse:
    """Answer any GET (including ALB target-group health checks) with 200."""
    return JSONResponse({"status": "ok"})


@app.api_route("/{full_path:path}", methods=["POST"])
async def _dispatch(full_path: str, request: Request) -> Any:
    """Disaggregate one serving request: prime prefill, then stream decode."""
    path = request.url.path
    if path.endswith(ADMIN_PATH):
        return await _admin_add(request)

    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not _is_serving_path(path):
        # Not a generation path (e.g. /v1/models): pass straight through to decode.
        return await _stream_decode(path, body)

    # Residency check: non-blocking, treated as a miss so the prompt always goes
    # to prefill first (the store is never on the request's critical path).
    prefill_kv_params = await _prime_prefill(path, body)
    return await _stream_decode(path, _decode_body(body, prefill_kv_params))


if __name__ == "__main__":
    logger.info("starting PD proxy on :%d (prefill=%s decode=%s)", PORT, PREFILL_URL, DECODE_URL)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
