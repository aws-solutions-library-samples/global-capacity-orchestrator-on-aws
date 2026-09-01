"""Regional API proxy for authenticated access to one region's internal ALB.

The Lambda runs in the regional VPC. API Gateway authenticates callers with
IAM, then this function resolves the platform Ingress ALB, signs the exact
backend request with a short-lived HMAC envelope, and forwards it privately.

Environment variables:
    SECRET_ARN: Secrets Manager ARN containing the backend HMAC signing key.
    REGISTRY_REGION: Region containing the project ALB-hostname SSM registry.
    TARGET_REGION: Region served by this regional API.
    PROJECT_NAME: Deployment prefix used by the SSM path and EKS cluster name.
    AWS_ACCOUNT_ID: Account that must own the resolved ALB.
    AWS_URL_SUFFIX: CDK-resolved DNS suffix for the deployment partition.
    ALB_ENDPOINT: Optional literal ALB DNS name for compatibility/isolated use.
    REGIONAL_ENDPOINT_CACHE_TTL_SECONDS: Registry cache TTL, bounded to 0-300
        seconds (default: 60; 0 disables caching).
    PROXY_MAX_RETRIES: Max attempts for safe read-only methods (default: 3).
    PROXY_RETRY_BACKOFF_BASE: Base retry backoff in seconds (default: 0.3).
    SECRET_CACHE_TTL_SECONDS: Signing-key cache TTL in seconds (default: 300).
    BACKEND_TLS_SERVER_NAME: Private certificate identity asserted via SNI.
    BACKEND_TLS_ROOT_CA_PARAMETER: SSM parameter containing public CA roots.
    BACKEND_TLS_ROOT_CA_REGION: Region containing the public trust parameter.
    BACKEND_TLS_CA_CACHE_TTL_SECONDS: Normal trust refresh interval.
    BACKEND_TLS_CA_MAX_STALE_SECONDS: Maximum bounded stale-trust interval.
"""

import json
import logging
import os
import re
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from proxy_utils import (
    build_signed_headers,
    build_target_url,
    forward_request,
    get_secret_token,
    sanitize_request_headers,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/regional-api-proxy/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/regional-api-proxy/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


_LOGGER = logging.getLogger(__name__)
_MAX_FORWARD_REQUEST_SECONDS = 28.0
_LAMBDA_RESPONSE_HEADROOM_SECONDS = 1.0
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")
_DNS_NAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)
_REGIONAL_ENDPOINT_CACHE: dict[tuple[str, str, str, str], tuple[float, str]] = {}
_BACKEND_AUTH_ERRORS = (KeyError, RuntimeError)


def _regional_endpoint_cache_ttl() -> float:
    """Return the bounded registry cache TTL; zero disables caching."""
    try:
        value = float(os.getenv("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", "60"))
    except ValueError:
        return 60.0
    return value if 0 <= value <= 300 else 60.0


def _aws_url_suffix() -> str:
    """Return the CDK-resolved DNS suffix for this deployment partition."""
    suffix = os.getenv("AWS_URL_SUFFIX", "").strip().lower()
    if _DNS_NAME_RE.fullmatch(suffix) is None:
        raise RuntimeError("The AWS URL suffix is not configured")
    return suffix


def _validated_dns_name(value: Any, *, region: str) -> str:
    """Normalize an ELB DNS name and reject non-ELB hostnames."""
    endpoint = str(value or "").strip().rstrip(".")
    expected_suffix = f".elb.{_aws_url_suffix()}"
    if _DNS_NAME_RE.fullmatch(endpoint) is None or not endpoint.lower().endswith(expected_suffix):
        raise RuntimeError(f"The registered backend for {region} is invalid")
    return endpoint


def _validate_regional_endpoint_ownership(endpoint: str, region: str) -> None:
    """Verify that the DNS name is this account's internal GCO platform ALB."""
    expected_account = os.getenv("AWS_ACCOUNT_ID", "").strip()
    project_name = os.getenv("PROJECT_NAME", "").strip()
    if not expected_account or not project_name:
        raise RuntimeError("Regional endpoint ownership validation is not configured")

    client = boto3.client("elbv2", region_name=region)
    marker: str | None = None
    matched: dict[str, Any] | None = None
    for _ in range(20):
        kwargs = {"Marker": marker} if marker else {}
        response = client.describe_load_balancers(**kwargs)
        for load_balancer in response.get("LoadBalancers", []):
            dns_name = str(load_balancer.get("DNSName", "")).rstrip(".")
            if dns_name.lower() == endpoint.lower():
                matched = load_balancer
                break
        if matched is not None:
            break
        marker = response.get("NextMarker")
        if not marker:
            break
    if matched is None:
        raise RuntimeError(f"The registered backend for {region} does not exist")
    if matched.get("Type") != "application" or matched.get("Scheme") != "internal":
        raise RuntimeError(f"The registered backend for {region} is not an internal ALB")

    arn = str(matched.get("LoadBalancerArn", ""))
    arn_parts = arn.split(":", 5)
    if (
        len(arn_parts) != 6
        or arn_parts[2] != "elasticloadbalancing"
        or arn_parts[3] != region
        or arn_parts[4] != expected_account
    ):
        raise RuntimeError(f"The registered backend for {region} has invalid ownership")

    tag_response = client.describe_tags(ResourceArns=[arn])
    descriptions = tag_response.get("TagDescriptions", [])
    tags = {
        str(tag.get("Key")): str(tag.get("Value"))
        for description in descriptions
        for tag in description.get("Tags", [])
    }
    expected_cluster = f"{project_name}-{region}"
    cluster_match = (
        tags.get("eks:eks-cluster-name") == expected_cluster
        or tags.get("elbv2.k8s.aws/cluster") == expected_cluster
    )
    if not cluster_match:
        raise RuntimeError(f"The registered backend for {region} is not owned by the GCO cluster")

    # Accept only the explicit Gateway ownership marker; a cluster tag alone
    # is never sufficient.
    platform_match = tags.get("gco.aws/gateway") == "gco-system/gco-gateway"
    if not platform_match:
        raise RuntimeError(f"The registered backend for {region} is not the GCO Gateway")


def _resolve_registered_endpoint() -> str:
    """Resolve and verify the regional ALB, retaining literal compatibility."""
    target_region = os.getenv("TARGET_REGION", "").strip()
    literal_endpoint = os.getenv("ALB_ENDPOINT", "").strip()
    if literal_endpoint:
        return _validated_dns_name(literal_endpoint, region=target_region or "configured region")

    registry_region = os.getenv("REGISTRY_REGION", "").strip()
    project_name = os.getenv("PROJECT_NAME", "").strip()
    expected_account = os.getenv("AWS_ACCOUNT_ID", "").strip()
    if (
        _REGION_RE.fullmatch(registry_region) is None
        or _REGION_RE.fullmatch(target_region) is None
        or not project_name
        or not expected_account
    ):
        raise RuntimeError("Regional endpoint registry is not configured")

    cache_key = (registry_region, target_region, project_name, expected_account)
    ttl = _regional_endpoint_cache_ttl()
    now = time.monotonic()
    cached = _REGIONAL_ENDPOINT_CACHE.get(cache_key)
    if ttl > 0 and cached is not None and now - cached[0] < ttl:
        return cached[1]

    parameter_name = f"/{project_name}/alb-hostname-{target_region}"
    try:
        response = boto3.client("ssm", region_name=registry_region).get_parameter(
            Name=parameter_name
        )
        endpoint = _validated_dns_name(
            response.get("Parameter", {}).get("Value"), region=target_region
        )
        _validate_regional_endpoint_ownership(endpoint, target_region)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            f"The registered backend for {target_region} could not be verified"
        ) from exc

    _REGIONAL_ENDPOINT_CACHE[cache_key] = (time.monotonic(), endpoint)
    return endpoint


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
    """Proxy one IAM-authenticated API Gateway request to the internal ALB."""
    try:
        signing_key = get_secret_token()
    except _BACKEND_AUTH_ERRORS:
        return _error_response(503, "Backend authentication is temporarily unavailable")

    try:
        alb_endpoint = _resolve_registered_endpoint()
    except RuntimeError as exc:
        _LOGGER.warning("Regional backend resolution failed: %s", exc)
        return _error_response(502, "Regional backend is temporarily unavailable")

    http_method = event["httpMethod"]
    path = event["path"]
    query_string = (
        event.get("multiValueQueryStringParameters") or event.get("queryStringParameters") or {}
    )
    headers = dict(event.get("headers") or {})
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return _error_response(415, "Base64-encoded request bodies are not supported")

    target_url = build_target_url(alb_endpoint, path, query_string)
    headers = sanitize_request_headers(headers)
    headers.update(build_signed_headers(signing_key, http_method, target_url, body))

    return forward_request(
        target_url,
        http_method,
        headers,
        body,
        timeout=_get_request_timeout_seconds(context),
    )
