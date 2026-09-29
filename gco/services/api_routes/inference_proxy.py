"""Authenticated reverse proxy for managed inference endpoints.

All public inference traffic terminates at the dedicated inference-proxy
service, whose ``AuthenticationMiddleware`` validates the Lambda proxy's
short-lived HMAC envelope (timestamp, nonce, method, target, and body digest).
The service then forwards to one strictly derived in-cluster Service name. This
keeps model traffic out of the manifest processor and removes the historical
direct ALB target groups that allowed callers to bypass API Gateway through
Global Accelerator.

The upstream hop is verified HTTPS on port 8443: every managed model pod runs
a TLS sidecar serving the ``gco-inference`` wildcard certificate, and this
proxy trusts only the GCO internal CA (see :mod:`gco.services.internal_tls`).

Every proxied request builds its own upstream client and closes it once its
stream ends. A process-wide client would pool nothing: kube-proxy balances
connections, not requests, so keep-alive stays off to spread requests across
the Ready model replicas, and each request opens its own connection either
way. A client per request also takes its TLS context from
:func:`~gco.services.internal_tls.internal_ssl_context` when the request
arrives. That cache hands back the same context until the projected
``ca.crt`` changes and a fresh one afterwards, so a rotated internal CA (for
example the post-Helm pass re-issuing ``inference-proxy-tls`` during an
in-place upgrade) is trusted from the next request on, without restarting
the pod; a long-lived client would keep verifying against the bundle it
loaded first and answer 502 until a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import httpx2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from gco.services import internal_tls, tracing
from gco.services.inference_store import InferenceEndpointStore, get_inference_endpoint_store

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-28T07:34:51Z
# Generated from Git commit: 95213a3dfe214f41ea8e3977b79711b1be061ac0
# Flowchart(s) generated from this file:
#   * ``_resolve_upstream`` -> ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._resolve_upstream.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._resolve_upstream.png``)
#   * ``_proxy`` -> ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._proxy.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._proxy.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


router = APIRouter(prefix="/inference", tags=["Inference"])
logger = logging.getLogger(__name__)

_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_SUPPORTED_METHODS = ["GET", "HEAD", "POST"]
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
_ALLOWED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "cache-control",
        "content-encoding",
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
_BLOCKED_PATH_SEGMENTS = frozenset(
    {"admin", "debug", "docs", "instances", "metrics", "openapi.json"}
)
_V1_MODELS_RE = re.compile(r"^v1/models(?:/[^/]+)?$")
_V1_GENERATION_RE = re.compile(r"^v1/(?:chat/completions|completions|embeddings|responses)$")
_V2_MODELS_RE = re.compile(r"^v2/models(?:/[^/]+(?:/(?:config|infer|ready|stats))?)?$")


def _bounded_timeout(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read one finite, bounded timeout from the environment."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


@lru_cache(maxsize=1)
def _get_inference_store() -> InferenceEndpointStore:
    """Create one process-local DynamoDB endpoint-store client lazily."""
    return get_inference_endpoint_store()


#: Upstream cleanups still running. The event loop holds only weak references
#: to tasks, and a cleanup outlives its awaiter whenever the request that
#: started it is cancelled, so each one is held here until it finishes.
_cleanup_tasks: set[asyncio.Task[None]] = set()


def _new_upstream_client() -> httpx2.AsyncClient:
    """Build the client for one proxied request; the caller releases it.

    The TLS context is looked up now, per request, which is what lets a
    rotated internal CA take effect without a restart (see the module
    docstring); a lookup whose CA file is unchanged costs one ``stat``, not a
    CA load. No keep-alive and no connection cap: the client carries exactly
    one request, and its connection closes as soon as that response ends.
    ``verify`` and ``limits`` sit on the transport because a client given
    ``transport=`` ignores its own. The default ``Accept-Encoding`` is removed
    because the proxy relays raw upstream bytes: only an encoding the caller
    asked for may reach the model. Raises
    :class:`~gco.services.internal_tls.InternalTLSError` while the CA bundle
    is missing or invalid, before anything exists that would need closing.
    """
    transport = httpx2.AsyncHTTPTransport(
        verify=internal_tls.internal_ssl_context(),
        limits=httpx2.Limits(max_connections=None, max_keepalive_connections=0),
        trust_env=False,
    )
    client = httpx2.AsyncClient(
        transport=tracing.wrap_async_transport(transport),
        follow_redirects=False,
        trust_env=False,
    )
    del client.headers["accept-encoding"]
    return client


def _validate_label(value: object, field: str) -> str:
    """Return a safe Kubernetes DNS label or reject the request."""
    if not isinstance(value, str) or _DNS_LABEL_RE.fullmatch(value) is None:
        raise HTTPException(status_code=404, detail=f"Invalid inference {field}")
    return value


def _target_service(endpoint: dict[str, Any], endpoint_name: str) -> str:
    """Resolve the only in-cluster Service this endpoint may use.

    Plain endpoints use ``<name>``. Mooncake disaggregated/both endpoints use
    their reconciled ``<name>-proxy`` Service. During an active canary, a
    cryptographically unbiased request sample is routed to ``<name>-canary``.
    Every value is derived from a validated endpoint record, never from a URL or
    header supplied by the caller.
    """
    spec = endpoint.get("spec")
    if not isinstance(spec, dict):
        raise HTTPException(status_code=503, detail="Inference endpoint has an invalid spec")

    mooncake = spec.get("mooncake")
    if isinstance(mooncake, dict) and mooncake.get("mode") in {"disaggregated", "both"}:
        return _validate_label(f"{endpoint_name}-proxy", "service")

    canary = spec.get("canary")
    region = os.getenv("REGION", "")
    region_status = endpoint.get("region_status")
    local_status = region_status.get(region, {}) if isinstance(region_status, dict) else {}
    canary_status = local_status.get("canary") if isinstance(local_status, dict) else None
    if isinstance(canary, dict) and isinstance(canary_status, dict):
        try:
            weight = int(canary.get("weight", 0))
            ready = int(canary_status.get("replicas_ready", 0))
            desired = int(canary_status.get("replicas_desired", 0))
        except TypeError, ValueError:
            weight = ready = desired = 0
        canary_is_ready = (
            canary_status.get("state") == "running"
            and canary_status.get("image") == canary.get("image")
            and desired > 0
            and ready >= desired
        )
        if canary_is_ready and 1 <= weight <= 99 and secrets.randbelow(100) < weight:
            return _validate_label(f"{endpoint_name}-canary", "service")

    return endpoint_name


async def _resolve_upstream(endpoint_name: str) -> tuple[str, str, str]:
    """Resolve the authorized Service, namespace, and configured health path."""
    endpoint_name = _validate_label(endpoint_name, "name")
    endpoint = await asyncio.to_thread(_get_inference_store().get_endpoint, endpoint_name)
    if not endpoint:
        raise HTTPException(
            status_code=404, detail=f"Inference endpoint '{endpoint_name}' not found"
        )

    namespace = _validate_label(endpoint.get("namespace", "gco-inference"), "namespace")
    allowed_namespace = os.getenv("INFERENCE_NAMESPACE", "gco-inference")
    if namespace != allowed_namespace:
        raise HTTPException(status_code=503, detail="Inference endpoint namespace is not routable")

    region = os.getenv("REGION", "")
    target_regions = endpoint.get("target_regions")
    if not region or not isinstance(target_regions, list) or region not in target_regions:
        raise HTTPException(
            status_code=404, detail="Inference endpoint is not deployed in this region"
        )

    desired_state = endpoint.get("desired_state")
    region_status = endpoint.get("region_status")
    local_status = region_status.get(region, {}) if isinstance(region_status, dict) else {}
    local_state = local_status.get("state") if isinstance(local_status, dict) else None
    if desired_state != "running" or local_state != "running":
        raise HTTPException(
            status_code=503, detail="Inference endpoint is not ready in this region"
        )

    spec = endpoint.get("spec")
    configured_health_path = (
        spec.get("health_check_path", "/health") if isinstance(spec, dict) else "/health"
    )
    if not isinstance(configured_health_path, str) or not configured_health_path.startswith("/"):
        configured_health_path = "/health"

    return _target_service(endpoint, endpoint_name), namespace, configured_health_path


def _request_headers(request: Request) -> list[tuple[bytes, bytes]]:
    """Forward only explicitly supported end-to-end model request headers, as sent.

    The raw ASGI bytes are forwarded rather than Starlette's latin-1 decoded
    strings: httpx2 encodes a ``str`` header value as ASCII, so one non-ASCII
    byte (a UTF-8 ``user-agent``, say) would fail the request with a 500
    instead of reaching the model unchanged.
    """
    return [
        (name.lower(), value)
        for name, value in request.headers.raw
        if name.lower().decode("latin-1") in _ALLOWED_REQUEST_HEADERS
    ]


def _response_headers(response: httpx2.Response) -> list[tuple[bytes, bytes]]:
    """Copy end-to-end response headers byte for byte, dropping hop-by-hop framing.

    Starlette encodes ``str`` header values as latin-1, so relaying httpx2's
    decoded values failed any response carrying a UTF-8 value outside latin-1
    with a 500 and silently transcoded one inside it. The raw bytes keep every
    value exact and every repeated header on its own line; names are
    lowercased, as ASGI requires.
    """
    blocked = _HOP_BY_HOP_HEADERS | {"content-length"}
    relayed: list[tuple[bytes, bytes]] = []
    for name, value in response.headers.raw:
        lowered = name.lower()
        if lowered.decode("latin-1") not in blocked:
            relayed.append((lowered, value))
    return relayed


def _validate_upstream_path(
    upstream_path: str,
    method: str,
    configured_health_path: str = "/health",
) -> str:
    """Allow serving/configured-health APIs while denying privileged paths."""
    normalized = upstream_path.strip("/")
    segments = [segment.lower() for segment in normalized.split("/") if segment]
    if any(segment in _BLOCKED_PATH_SEGMENTS for segment in segments):
        raise HTTPException(status_code=404, detail="Inference path is not exposed")

    method = method.upper()
    configured_health = configured_health_path.strip("/")
    # ``server_info`` is SGLang's read-only identity document (the launcher
    # arguments the running server resolved, including ``model_path`` and
    # ``revision``); it is what ``gco inference models`` reads for that runtime.
    if (
        not normalized
        or normalized == "health"
        or (configured_health and normalized == configured_health)
        or normalized == "server_info"
        or _V1_MODELS_RE.fullmatch(normalized)
    ) and method in {"GET", "HEAD"}:
        return normalized
    # ``generate`` is SGLang's native generation API; streaming is a body flag
    # on the same path.
    if (_V1_GENERATION_RE.fullmatch(normalized) or normalized == "generate") and method == "POST":
        return normalized
    if _V2_MODELS_RE.fullmatch(normalized) and method in {"GET", "HEAD", "POST"}:
        return normalized

    raise HTTPException(status_code=404, detail="Inference path is not exposed")


async def _close_upstream(response: httpx2.Response | None, client: httpx2.AsyncClient) -> None:
    """Close one request's upstream response, if it got one, then its client.

    Both closes are idempotent, so releasing an upstream twice is harmless.
    """
    try:
        if response is not None:
            await response.aclose()
    finally:
        await client.aclose()


async def _release_upstream(response: httpx2.Response | None, client: httpx2.AsyncClient) -> None:
    """Close one request's upstream in its own task, shielded from cancellation.

    Cancelling the request (the caller went away, or shutdown outlived its
    grace period) interrupts only this wait, never the close itself, so a
    cancelled request still releases its model connection.
    """
    cleanup = asyncio.create_task(_close_upstream(response, client))
    _cleanup_tasks.add(cleanup)
    cleanup.add_done_callback(_cleanup_tasks.discard)
    await asyncio.shield(cleanup)


async def _stream_response(
    response: httpx2.Response, client: httpx2.AsyncClient
) -> AsyncIterator[bytes]:
    """Yield the upstream body unchanged, then release the request's upstream."""
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await _release_upstream(response, client)


class _UpstreamStreamingResponse(StreamingResponse):
    """Relay one upstream response, releasing its client however the relay ends.

    The body generator releases the upstream as soon as the stream finishes,
    fails, or is cancelled mid-body. Starlette starts that generator only once
    the headers are on their way, though: when the caller disconnected while
    the model was still answering, the response is cancelled before its first
    chunk and the generator never runs, so the response itself releases the
    upstream again once it is done (a no-op after the generator's release).
    """

    def __init__(self, response: httpx2.Response, client: httpx2.AsyncClient) -> None:
        relayed_headers = _response_headers(response)
        super().__init__(
            _stream_response(response, client),
            status_code=response.status_code,
            media_type=None,
        )
        # Assigned rather than passed as ``headers=``, which takes ``str``
        # values and re-encodes them as latin-1 (see _response_headers).
        self.raw_headers = relayed_headers
        self._upstream = (response, client)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _release_upstream(*self._upstream)


async def _proxy(
    request: Request, endpoint_name: str, upstream_path: str = ""
) -> StreamingResponse:
    """Forward one authenticated request to a managed in-cluster endpoint."""
    if any(part in {".", ".."} for part in upstream_path.split("/")):
        raise HTTPException(status_code=400, detail="Invalid inference path")
    service_name, namespace, configured_health_path = await _resolve_upstream(endpoint_name)
    upstream_path = _validate_upstream_path(
        upstream_path,
        request.method,
        configured_health_path,
    )
    encoded_suffix = quote(upstream_path, safe="/:@-._~")
    upstream_path_value = f"/{encoded_suffix}" if encoded_suffix else "/"
    upstream_url = (  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host
        # Both host labels passed the strict Kubernetes DNS-label allowlist in
        # _resolve_upstream; callers cannot supply a URL, address, or suffix.
        # Port 8443 is the model pod's TLS sidecar, the only port its Service
        # publishes.
        f"https://{service_name}.{namespace}.svc.cluster.local:8443{upstream_path_value}"
    )

    body = await request.body()
    timeout = httpx2.Timeout(
        connect=_bounded_timeout("INFERENCE_PROXY_CONNECT_TIMEOUT_SECONDS", 5.0, 0.1, 30.0),
        read=_bounded_timeout("INFERENCE_PROXY_READ_TIMEOUT_SECONDS", 300.0, 1.0, 900.0),
        write=_bounded_timeout("INFERENCE_PROXY_WRITE_TIMEOUT_SECONDS", 30.0, 1.0, 300.0),
        pool=_bounded_timeout("INFERENCE_PROXY_POOL_TIMEOUT_SECONDS", 5.0, 0.1, 30.0),
    )
    try:
        client = _new_upstream_client()
    except internal_tls.InternalTLSError as exc:
        logger.error("Inference upstream TLS trust is unavailable: %s", exc)
        raise HTTPException(status_code=502, detail="Inference endpoint is unavailable") from exc

    # Until the streaming response owns the upstream, every exit releases it.
    response: httpx2.Response | None = None
    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            params=list(request.query_params.multi_items()),
            headers=_request_headers(request),
            content=body,
            timeout=timeout,
        )
        response = await client.send(upstream_request, stream=True)
        return _UpstreamStreamingResponse(response, client)
    except httpx2.TimeoutException as exc:
        await _release_upstream(response, client)
        raise HTTPException(status_code=504, detail="Inference endpoint timed out") from exc
    except httpx2.HTTPError as exc:
        await _release_upstream(response, client)
        logger.warning(
            "Inference upstream request failed: service=%s namespace=%s error_type=%s error=%s",
            service_name,
            namespace,
            type(exc).__name__,
            exc,
        )
        raise HTTPException(status_code=502, detail="Inference endpoint is unavailable") from exc
    except BaseException:
        # Anything else, a cancelled request included, releases it too.
        await _release_upstream(response, client)
        raise


async def proxy_inference_root(request: Request, endpoint_name: str) -> StreamingResponse:
    """Proxy an endpoint-root request after platform authentication."""
    return await _proxy(request, endpoint_name)


async def proxy_inference_path(
    request: Request,
    endpoint_name: str,
    upstream_path: str,
) -> StreamingResponse:
    """Proxy an endpoint sub-path after platform authentication."""
    return await _proxy(request, endpoint_name, upstream_path)


# Register one route per method rather than a single multi-method route.
# FastAPI derives an operation's ``operationId`` from ``generate_unique_id``,
# which appends ``list(route.methods)[0]`` — a single arbitrary member of an
# unordered set — and computes it once per route. A route carrying GET, HEAD,
# and POST therefore emits three OpenAPI operations sharing one operationId,
# which violates the spec's uniqueness requirement, makes generated clients
# collide, and raises a UserWarning on every schema build. One method per
# route keeps each generated operationId distinct while leaving request
# handling byte-for-byte identical.
for _path, _endpoint in (
    ("/{endpoint_name}", proxy_inference_root),
    ("/{endpoint_name}/{upstream_path:path}", proxy_inference_path),
):
    for _method in _SUPPORTED_METHODS:
        router.add_api_route(_path, _endpoint, methods=[_method])
