"""Global API Gateway proxy for the authenticated TLS backend path.

API Gateway authenticates callers with IAM, this Lambda signs each exact
request with the deployment HMAC key, and a strict private-root TLS transport
forwards it through Global Accelerator to a healthy regional ALB. The reusable
HMAC key and the root CA private key are never transmitted.

``X-GCO-Target-Region`` is intentionally rejected here. This Lambda is not
attached to regional VPCs and therefore cannot route directly to private ALBs;
authorized callers that need region pinning must invoke that region's always-
deployed API bridge directly. Direct caller access is an explicit opt-in even
though the bridge itself is required for aggregation.

Environment variables:
    GLOBAL_ACCELERATOR_ENDPOINT: Global Accelerator DNS name.
    SECRET_ARN: Secrets Manager ARN containing the HMAC signing key.
    BACKEND_TLS_SERVER_NAME: Private certificate identity asserted via SNI.
    BACKEND_TLS_ROOT_CA_PARAMETER: SSM parameter containing public CA roots.
    BACKEND_TLS_ROOT_CA_REGION: Region containing the public trust parameter.
    PROXY_MAX_RETRIES: Max attempts for safe read-only methods (default: 3).
    PROXY_RETRY_BACKOFF_BASE: Base retry backoff in seconds (default: 0.3).
    SECRET_CACHE_TTL_SECONDS: Signing-key cache TTL in seconds (default: 300).
"""

import json
import os
from typing import Any

from proxy_utils import (
    build_signed_headers,
    build_target_url,
    forward_request,
    get_secret_token,
    sanitize_request_headers,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/api-gateway-proxy/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/api-gateway-proxy/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


_MAX_FORWARD_REQUEST_SECONDS = 28.0
_LAMBDA_RESPONSE_HEADROOM_SECONDS = 1.0


def _pop_header(headers: dict[str, str], header_name: str) -> str | None:
    """Remove and return one case-insensitive header value."""
    for key in list(headers):
        if key.lower() == header_name.lower():
            value = headers.pop(key)
            return str(value).strip() if value is not None else ""
    return None


def _error_response(status_code: int, message: str) -> dict[str, Any]:
    """Build a bounded API Gateway error response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def _get_request_timeout_seconds(context: Any) -> float:
    """Bound upstream work while retaining time to return a Lambda response."""
    get_remaining_time = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(get_remaining_time):
        return _MAX_FORWARD_REQUEST_SECONDS

    remaining_seconds = float(get_remaining_time()) / 1000.0
    available_seconds = remaining_seconds - _LAMBDA_RESPONSE_HEADROOM_SECONDS
    return max(0.0, min(_MAX_FORWARD_REQUEST_SECONDS, available_seconds))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Proxy one IAM-authenticated request through Global Accelerator."""
    try:
        signing_key = get_secret_token()
    except KeyError, RuntimeError:
        return _error_response(503, "Backend authentication is temporarily unavailable")

    http_method = event["httpMethod"]
    path = event["path"]
    query_string = (
        event.get("multiValueQueryStringParameters") or event.get("queryStringParameters") or {}
    )
    headers = dict(event.get("headers") or {})

    # A non-VPC Lambda cannot reach any region's internal ALB directly. Never
    # pretend region pinning succeeded or silently route the request elsewhere.
    if _pop_header(headers, "X-GCO-Target-Region") is not None:
        return _error_response(
            400,
            "X-GCO-Target-Region is not supported by the global endpoint; "
            "use the target region's regional API endpoint if authorized for direct access",
        )

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return _error_response(415, "Base64-encoded request bodies are not supported")

    try:
        target_url = build_target_url(
            os.environ["GLOBAL_ACCELERATOR_ENDPOINT"],
            path,
            query_string,
        )
    except KeyError, ValueError:
        return _error_response(503, "Global backend routing is temporarily unavailable")

    # Only a short-lived HMAC envelope is transmitted. TLS independently
    # authenticates the ALB certificate and encrypts the complete request.
    headers = sanitize_request_headers(headers)
    headers.update(build_signed_headers(signing_key, http_method, target_url, body))

    return forward_request(
        target_url,
        http_method,
        headers,
        body,
        timeout=_get_request_timeout_seconds(context),
    )
