"""Authenticated reverse proxy for managed inference endpoints.

All public inference traffic terminates at the dedicated inference-proxy
service, whose ``AuthenticationMiddleware`` validates the Lambda proxy's
short-lived HMAC envelope (timestamp, nonce, method, target, and body digest).
The service then forwards to one strictly derived in-cluster Service name. This
keeps model traffic out of the manifest processor and removes the historical
direct ALB target groups that allowed callers to bypass API Gateway through
Global Accelerator.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from gco.services.inference_store import InferenceEndpointStore, get_inference_endpoint_store

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``_resolve_upstream`` -> ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._resolve_upstream.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._resolve_upstream.png``)
#   * ``_proxy`` -> ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._proxy.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/api_routes/inference_proxy._proxy.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


router = APIRouter(prefix="/inference", tags=["Inference"])

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


def _request_headers(request: Request) -> list[tuple[str, str]]:
    """Forward only explicitly supported end-to-end model request headers."""
    return [
        (name.lower(), value)
        for name, value in request.headers.items()
        if name.lower() in _ALLOWED_REQUEST_HEADERS
    ]


def _response_headers(response: httpx.Response) -> dict[str, str]:
    """Copy end-to-end response headers while dropping hop-by-hop framing."""
    blocked = _HOP_BY_HOP_HEADERS | {"content-length"}
    return {name: value for name, value in response.headers.items() if name.lower() not in blocked}


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
    if (
        not normalized
        or normalized == "health"
        or (configured_health and normalized == configured_health)
        or normalized == "info"
        or _V1_MODELS_RE.fullmatch(normalized)
    ) and method in {"GET", "HEAD"}:
        return normalized
    if (
        _V1_GENERATION_RE.fullmatch(normalized) or normalized in {"generate", "generate_stream"}
    ) and method == "POST":
        return normalized
    if _V2_MODELS_RE.fullmatch(normalized) and method in {"GET", "HEAD", "POST"}:
        return normalized

    raise HTTPException(status_code=404, detail="Inference path is not exposed")


async def _close_upstream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


async def _stream_response(
    response: httpx.Response, client: httpx.AsyncClient
) -> AsyncIterator[bytes]:
    """Yield the upstream response and shield connection cleanup on cancellation."""
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        cleanup = asyncio.create_task(_close_upstream(response, client))
        await asyncio.shield(cleanup)


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
        f"http://{service_name}.{namespace}.svc.cluster.local{upstream_path_value}"
    )

    body = await request.body()
    timeout = httpx.Timeout(
        connect=_bounded_timeout("INFERENCE_PROXY_CONNECT_TIMEOUT_SECONDS", 5.0, 0.1, 30.0),
        read=_bounded_timeout("INFERENCE_PROXY_READ_TIMEOUT_SECONDS", 300.0, 1.0, 900.0),
        write=_bounded_timeout("INFERENCE_PROXY_WRITE_TIMEOUT_SECONDS", 30.0, 1.0, 300.0),
        pool=_bounded_timeout("INFERENCE_PROXY_POOL_TIMEOUT_SECONDS", 5.0, 0.1, 30.0),
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            params=list(request.query_params.multi_items()),
            headers=_request_headers(request),
            content=body,
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise HTTPException(status_code=504, detail="Inference endpoint timed out") from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Inference endpoint is unavailable") from exc

    return StreamingResponse(
        _stream_response(response, client),
        status_code=response.status_code,
        headers=_response_headers(response),
        media_type=None,
    )


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
