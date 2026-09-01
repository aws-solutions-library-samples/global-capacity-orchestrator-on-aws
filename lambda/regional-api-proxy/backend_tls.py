"""Strict private-root TLS transport shared by GCO backend callers.

Callers connect to dynamic Global Accelerator or internal-ALB DNS names while
presenting and verifying the stable deployment-local identity configured in
``BACKEND_TLS_SERVER_NAME``. The trust bundle is public material retrieved from
the project-scoped SSM parameter; no root private key is available to callers.
"""

from __future__ import annotations

import logging
import os
import re
import ssl
import threading
import time

import boto3
import urllib3

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T13:22:56Z
# Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4
# Flowchart(s) generated from this file:
#   * ``get_backend_http_pool`` -> ``diagrams/code_diagrams/lambda/tls-shared/backend_tls.get_backend_http_pool.html``
#     (PNG: ``diagrams/code_diagrams/lambda/tls-shared/backend_tls.get_backend_http_pool.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


LOGGER = logging.getLogger(__name__)

_DNS_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)
_pool_lock = threading.Lock()
_cached_pool: urllib3.PoolManager | None = None
_last_successful_refresh = 0.0
_last_refresh_attempt = 0.0


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _tls_settings() -> tuple[str, str, str, float, float, float]:
    server_name = os.getenv("BACKEND_TLS_SERVER_NAME", "").strip().rstrip(".")
    parameter_name = os.getenv("BACKEND_TLS_ROOT_CA_PARAMETER", "").strip()
    parameter_region = os.getenv("BACKEND_TLS_ROOT_CA_REGION", "").strip()
    if _DNS_RE.fullmatch(server_name) is None:
        raise RuntimeError("Backend TLS server identity is not configured")
    if not parameter_name.startswith("/") or not parameter_region:
        raise RuntimeError("Backend TLS trust parameter is not configured")
    ttl = _bounded_env_float("BACKEND_TLS_CA_CACHE_TTL_SECONDS", 300.0, 1.0, 3600.0)
    max_stale = max(
        ttl,
        _bounded_env_float(
            "BACKEND_TLS_CA_MAX_STALE_SECONDS",
            3600.0,
            1.0,
            86400.0,
        ),
    )
    retry = _bounded_env_float("BACKEND_TLS_CA_RETRY_SECONDS", 5.0, 0.1, 60.0)
    return server_name, parameter_name, parameter_region, ttl, max_stale, retry


def _new_pool(server_name: str, trust_bundle: str) -> urllib3.PoolManager:
    if "PRIVATE KEY" in trust_bundle or "-----BEGIN CERTIFICATE-----" not in trust_bundle:
        raise RuntimeError("Backend TLS trust parameter contains invalid public material")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    try:
        context.load_verify_locations(cadata=trust_bundle)
    except ssl.SSLError as exc:
        raise RuntimeError("Backend TLS trust parameter contains malformed certificates") from exc
    return urllib3.PoolManager(
        num_pools=4,
        maxsize=10,
        retries=False,
        ssl_context=context,
        server_hostname=server_name,
        assert_hostname=server_name,
    )


def get_backend_http_pool() -> urllib3.PoolManager:
    """Return a verified HTTPS pool, refreshing its public trust bundle safely."""
    global _cached_pool, _last_successful_refresh, _last_refresh_attempt

    server_name, parameter_name, parameter_region, ttl, max_stale, retry = _tls_settings()
    now = time.monotonic()
    age = now - _last_successful_refresh
    if _cached_pool is not None and age < ttl:
        return _cached_pool
    if _cached_pool is not None and age <= max_stale and now - _last_refresh_attempt < retry:
        return _cached_pool

    with _pool_lock:
        now = time.monotonic()
        age = now - _last_successful_refresh
        if _cached_pool is not None and age < ttl:
            return _cached_pool
        if _cached_pool is not None and age <= max_stale and now - _last_refresh_attempt < retry:
            return _cached_pool

        _last_refresh_attempt = now
        try:
            response = boto3.client("ssm", region_name=parameter_region).get_parameter(
                Name=parameter_name
            )
            trust_bundle = str(response.get("Parameter", {}).get("Value", ""))
            refreshed_pool = _new_pool(server_name, trust_bundle)
        except Exception as exc:
            if _cached_pool is not None and age <= max_stale:
                LOGGER.warning("Backend TLS trust refresh failed; using bounded stale trust bundle")
                return _cached_pool
            raise RuntimeError("Backend TLS trust bundle is unavailable") from exc

        _cached_pool = refreshed_pool
        _last_successful_refresh = now
        return refreshed_pool


def reset_backend_tls_cache() -> None:
    """Clear process-local trust state for deterministic tests and cold-start simulation."""
    global _cached_pool, _last_successful_refresh, _last_refresh_attempt
    with _pool_lock:
        _cached_pool = None
        _last_successful_refresh = 0.0
        _last_refresh_attempt = 0.0
