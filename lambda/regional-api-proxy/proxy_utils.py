"""Shared, fail-closed utilities for API Gateway backend proxy Lambdas."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import boto3
import urllib3
from backend_tls import get_backend_http_pool

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``build_signed_headers`` -> ``diagrams/code_diagrams/lambda/proxy-shared/proxy_utils.build_signed_headers.html``
#     (PNG: ``diagrams/code_diagrams/lambda/proxy-shared/proxy_utils.build_signed_headers.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)

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
_INTERNAL_SIGNATURE_HEADERS = frozenset(
    {
        "x-gco-signature-version",
        "x-gco-signature",
        "x-gco-timestamp",
        "x-gco-nonce",
        "x-gco-content-sha256",
    }
)
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


_secret_lock = threading.Lock()
_cached_secret: str | None = None
_cache_timestamp = 0.0
_last_successful_refresh = 0.0
_last_refresh_attempt = 0.0
_CACHE_TTL_SECONDS = _bounded_env_float("SECRET_CACHE_TTL_SECONDS", 300.0, 1.0, 3600.0)
_CACHE_MAX_STALE_SECONDS = max(
    _CACHE_TTL_SECONDS,
    _bounded_env_float("SECRET_CACHE_MAX_STALE_SECONDS", 900.0, 1.0, 7200.0),
)
_CACHE_RETRY_SECONDS = _bounded_env_float("SECRET_CACHE_RETRY_SECONDS", 5.0, 0.1, 60.0)


def _secret_region(secret_arn: str) -> str | None:
    """Return the owning region for a Secrets Manager ARN, if present."""
    parts = secret_arn.split(":", 5)
    if len(parts) == 6 and parts[0] == "arn" and parts[2] == "secretsmanager":
        return parts[3] or None
    return None


# The regional VPC proxy runs outside the API Gateway region where the shared
# HMAC key lives. Secrets Manager clients do not route cross-region ARNs to the
# owning endpoint automatically, so bind the client to the ARN's region.
_secrets_client = boto3.client(
    "secretsmanager",
    region_name=_secret_region(os.getenv("SECRET_ARN", "")),
)


def get_secret_token() -> str:
    """Return a cached signing key, with a strictly bounded stale grace period."""
    global _cached_secret, _cache_timestamp, _last_successful_refresh, _last_refresh_attempt

    now = time.monotonic()
    age = now - _last_successful_refresh
    if _cached_secret is not None and age < _CACHE_TTL_SECONDS:
        return _cached_secret
    if (
        _cached_secret is not None
        and age <= _CACHE_MAX_STALE_SECONDS
        and now - _last_refresh_attempt < _CACHE_RETRY_SECONDS
    ):
        return _cached_secret

    with _secret_lock:
        now = time.monotonic()
        age = now - _last_successful_refresh
        if _cached_secret is not None and age < _CACHE_TTL_SECONDS:
            return _cached_secret
        if (
            _cached_secret is not None
            and age <= _CACHE_MAX_STALE_SECONDS
            and now - _last_refresh_attempt < _CACHE_RETRY_SECONDS
        ):
            return _cached_secret

        _last_refresh_attempt = now
        try:
            response = _secrets_client.get_secret_value(SecretId=os.environ["SECRET_ARN"])
            secret_data = json.loads(response["SecretString"])
            token = secret_data.get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("secret token is missing")
        except Exception as error:
            if _cached_secret is not None and age <= _CACHE_MAX_STALE_SECONDS:
                logger.warning("Secrets Manager refresh failed; using bounded stale signing key")
                return _cached_secret
            raise RuntimeError("Authentication signing key is unavailable") from error

        _cached_secret = token
        _last_successful_refresh = now
        _cache_timestamp = now  # Backward-compatible observability/test alias.
        return token


def sanitize_request_headers(headers: dict[str, Any]) -> dict[str, str]:
    """Apply a case-insensitive end-to-end allowlist at the IAM trust boundary."""
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        normalized = str(name).strip().lower()
        if normalized not in _ALLOWED_REQUEST_HEADERS or value is None:
            continue
        sanitized[normalized] = str(value)
    return sanitized


def _request_target(target_url: str) -> str:
    parsed = urlsplit(target_url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def build_signed_headers(
    signing_key: str,
    http_method: str,
    target_url: str,
    body: str | None,
) -> dict[str, str]:
    """Build short-lived request authentication without transmitting the signing key."""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    content_hash = hashlib.sha256((body or "").encode("utf-8")).hexdigest()
    canonical = "\n".join(
        ["v1", timestamp, nonce, http_method.upper(), _request_target(target_url), content_hash]
    )
    signature = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-gco-signature-version": "v1",
        "x-gco-signature": signature,
        "x-gco-timestamp": timestamp,
        "x-gco-nonce": nonce,
        "x-gco-content-sha256": content_hash,
    }


def _outbound_headers(headers: dict[str, Any]) -> dict[str, str]:
    """Re-validate caller headers while retaining only generated auth fields."""
    allowed = _ALLOWED_REQUEST_HEADERS | _INTERNAL_SIGNATURE_HEADERS
    return {
        str(name).lower(): str(value)
        for name, value in headers.items()
        if str(name).lower() in allowed and value is not None
    }


_MAX_RETRIES = _bounded_env_int("PROXY_MAX_RETRIES", 3, 1, 5)
_RETRY_BACKOFF_BASE = _bounded_env_float("PROXY_RETRY_BACKOFF_BASE", 0.3, 0.0, 5.0)
_http: urllib3.PoolManager | None = None


def _tls_failure_response() -> dict[str, Any]:
    """Return a bounded error without exposing certificate or trust details."""
    return {
        "statusCode": 502,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Backend TLS verification failed"}),
    }


def forward_request(
    target_url: str,
    http_method: str,
    headers: dict[str, str],
    body: str | None,
    timeout: float = 29.0,
) -> dict[str, Any]:
    """Forward over authenticated TLS within one deadline.

    Retries are limited to safe, read-only methods. Plaintext, non-443, and
    credential-bearing targets are rejected before any network request.
    """
    parsed_target = urlsplit(target_url)
    try:
        target_port = parsed_target.port
    except ValueError as exc:
        raise ValueError("Backend proxy target has an invalid port") from exc
    if (
        parsed_target.scheme.lower() != "https"
        or not parsed_target.hostname
        or parsed_target.username is not None
        or parsed_target.password is not None
        or target_port not in {None, 443}
    ):
        raise ValueError("Backend proxy targets must use HTTPS on port 443")

    # Anchor the deadline before acquiring transport: the caller computed the
    # budget from the Lambda's remaining time, so a cold-start trust-bundle
    # refresh must consume this budget. Anchoring after it extended the wall
    # clock past the Lambda timeout, killing the function mid-flight instead
    # of returning its bounded 504 when the backend black-holed.
    deadline = time.monotonic() + max(timeout, 0.0)

    try:
        transport = _http or get_backend_http_pool()
    except RuntimeError:
        logger.exception("Backend TLS trust is unavailable")
        return {
            "statusCode": 503,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Backend trust is temporarily unavailable"}),
        }

    method = http_method.upper()
    encoded_body = body.encode("utf-8") if body else None
    max_attempts = _MAX_RETRIES if method in _RETRYABLE_METHODS else 1
    last_exception: Exception | None = None
    last_response: urllib3.BaseHTTPResponse | None = None
    attempts_made = 0

    for attempt in range(max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts_made = attempt + 1
        try:
            response = transport.request(
                method,
                target_url,
                headers=_outbound_headers(headers),
                body=encoded_body,
                timeout=urllib3.Timeout(total=remaining),
            )
            if response.status not in _RETRYABLE_STATUS_CODES:
                return _build_success_response(response)
            last_response = response
            logger.warning(
                "Retryable upstream status %d on attempt %d/%d for %s",
                response.status,
                attempt + 1,
                max_attempts,
                method,
            )
            if attempt == max_attempts - 1:
                return _build_success_response(response)
            response.release_conn()
        except urllib3.exceptions.SSLError:
            logger.exception("Backend TLS verification failed")
            return _tls_failure_response()
        except urllib3.exceptions.MaxRetryError as error:
            if isinstance(getattr(error, "reason", None), urllib3.exceptions.SSLError):
                logger.exception("Backend TLS verification failed")
                return _tls_failure_response()
            last_exception = error
            logger.warning(
                "Upstream %s failed on attempt %d/%d",
                method,
                attempt + 1,
                max_attempts,
            )
        except urllib3.exceptions.TimeoutError as error:
            last_exception = error
            logger.warning(
                "Upstream %s failed on attempt %d/%d",
                method,
                attempt + 1,
                max_attempts,
            )
        except Exception:
            logger.exception("Unexpected proxy forwarding failure")
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Internal server error"}),
            }

        if attempt < max_attempts - 1:
            remaining = deadline - time.monotonic()
            backoff = _RETRY_BACKOFF_BASE * (2**attempt)
            if remaining <= backoff:
                break
            time.sleep(backoff)

    if last_response is not None:
        return _build_success_response(last_response)
    if last_exception is not None:
        status_code = 503 if isinstance(last_exception, urllib3.exceptions.MaxRetryError) else 504
        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "error": "Service unavailable" if status_code == 503 else "Gateway timeout",
                    "message": f"Upstream failed after {attempts_made} attempt(s)",
                }
            ),
        }
    return {
        "statusCode": 504,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Gateway timeout"}),
    }


def _build_success_response(response: urllib3.BaseHTTPResponse) -> dict[str, Any]:
    """Build an API Gateway response without hop-by-hop framing headers."""
    response_headers = {
        str(name): str(value)
        for name, value in response.headers.items()
        if str(name).lower() not in _HOP_BY_HOP_HEADERS
    }
    return {
        "statusCode": response.status,
        "headers": response_headers,
        "body": response.data.decode("utf-8"),
    }


def build_target_url(
    endpoint: str,
    path: str,
    query_params: dict[str, str | list[str]] | None,
) -> str:
    """Build one HTTPS/443 upstream URL without losing repeated query keys."""
    base_url = endpoint if "://" in endpoint else f"https://{endpoint}"
    parsed_endpoint = urlsplit(base_url)
    try:
        endpoint_port = parsed_endpoint.port
    except ValueError as exc:
        raise ValueError(f"Invalid proxy endpoint: {endpoint!r}") from exc
    if (
        parsed_endpoint.scheme.lower() != "https"
        or not parsed_endpoint.hostname
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or endpoint_port not in {None, 443}
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise ValueError(f"Proxy endpoint must use HTTPS on port 443: {endpoint!r}")

    request_path = path if path.startswith("/") else f"/{path}"
    request_path = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", request_path)
    encoded_path = quote(request_path, safe="/:@-._~!$&'()*+,;=%")
    endpoint_path = parsed_endpoint.path.rstrip("/")
    return urlunsplit(
        (
            parsed_endpoint.scheme,
            parsed_endpoint.netloc,
            f"{endpoint_path}{encoded_path}",
            urlencode(query_params or {}, doseq=True),
            "",
        )
    )
