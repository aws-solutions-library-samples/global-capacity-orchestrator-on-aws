"""
Cross-region aggregation through reachable, IAM-authenticated regional APIs.

The centralized Lambda is intentionally not VPC-attached and cannot connect to
private ALBs in other regions. It discovers each deterministic regional API
Gateway stack with CloudFormation, signs each request with its execution-role
credentials, and sends it over the AWS-managed HTTPS endpoint. The regional
API's VPC Lambda then signs the backend request with the deployment HMAC key and
uses private-root authenticated TLS to reach that region's internal ALB.

Regional Endpoint Discovery:
    ``TARGET_REGIONS`` identifies the required regional API stacks. Each stack
    is named ``{PROJECT_NAME}-regional-api-{region}`` and exposes a
    ``RegionalApiEndpoint`` output. Discovery fails closed if any configured
    bridge is absent or invalid.

Environment Variables:
    PROJECT_NAME: Deployment prefix used in regional API stack names.
    TARGET_REGIONS: JSON list of required workload regions.
    AWS_URL_SUFFIX: CDK-resolved DNS suffix for the deployment partition.

API Routes:
    GET /api/v1/global/jobs - List jobs across all regions
    GET /api/v1/global/health - Health status across all regions
    GET /api/v1/global/status - Cluster status across all regions
    DELETE /api/v1/global/jobs - Bulk delete across all regions
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlencode, urlsplit

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T13:22:56Z
# Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/cross-region-aggregator/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/cross-region-aggregator/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


_LOGGER = logging.getLogger(__name__)
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")
_API_ID_RE = re.compile(r"^[a-z0-9]+$")
_DNS_SUFFIX_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)

# Regional API Gateway uses the AWS public trust chain. Certificate validation
# remains mandatory; the private-root pool is used only by each VPC proxy's
# subsequent ALB connection.
http = urllib3.PoolManager(cert_reqs="CERT_REQUIRED")

_cached_endpoints: dict[str, str] | None = None
_endpoints_cache_time: float = 0
_ENDPOINTS_CACHE_TTL = 300.0
_ENDPOINTS_CACHE_MAX_STALE = 3_600.0


def _configured_regions() -> list[str]:
    """Return the validated, de-duplicated regional bridge list."""
    try:
        configured = json.loads(os.environ["TARGET_REGIONS"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("Regional API discovery is not configured") from exc
    if not isinstance(configured, list) or not configured:
        raise RuntimeError("Regional API discovery is not configured")

    regions: list[str] = []
    for value in configured:
        if not isinstance(value, str) or _REGION_RE.fullmatch(value) is None:
            raise RuntimeError("Regional API discovery contains an invalid region")
        if value not in regions:
            regions.append(value)
    return regions


def _aws_url_suffix() -> str:
    """Return the CDK-resolved DNS suffix for this deployment partition."""
    suffix = os.environ.get("AWS_URL_SUFFIX", "").strip().lower()
    if _DNS_SUFFIX_RE.fullmatch(suffix) is None:
        raise RuntimeError("The AWS URL suffix is not configured")
    return suffix


def _normalize_regional_api_url(value: Any, region: str) -> str:
    """Validate one stack output as this region's execute-api ``prod`` URL."""
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    api_id = host.split(".", 1)[0]
    expected_host = f"{api_id}.execute-api.{region}.{_aws_url_suffix()}"
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or host != expected_host
        or _API_ID_RE.fullmatch(api_id) is None
        or parsed.path.rstrip("/") != "/prod"
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"The regional API endpoint for {region} is invalid")
    return f"https://{host}/prod"


def get_regional_endpoints() -> dict[str, str]:
    """Discover every required regional API Gateway endpoint via CloudFormation."""
    global _cached_endpoints, _endpoints_cache_time

    now = time.monotonic()
    cache_age = now - _endpoints_cache_time
    if _cached_endpoints is not None and cache_age < _ENDPOINTS_CACHE_TTL:
        return _cached_endpoints

    project_name = os.environ.get("PROJECT_NAME", "gco").strip()
    if not project_name:
        raise RuntimeError("Regional API discovery is not configured")

    endpoints: dict[str, str] = {}
    failed_regions: list[str] = []
    for region in _configured_regions():
        stack_name = f"{project_name}-regional-api-{region}"
        try:
            response = boto3.client("cloudformation", region_name=region).describe_stacks(
                StackName=stack_name
            )
            stacks = response.get("Stacks", [])
            if len(stacks) != 1:
                raise RuntimeError("Regional API stack was not found")
            outputs = {
                output.get("OutputKey"): output.get("OutputValue")
                for output in stacks[0].get("Outputs", [])
            }
            endpoints[region] = _normalize_regional_api_url(
                outputs.get("RegionalApiEndpoint"), region
            )
        except Exception:
            failed_regions.append(region)
            _LOGGER.exception("Regional API discovery failed for %s", region)

    if failed_regions:
        if _cached_endpoints is not None and cache_age < _ENDPOINTS_CACHE_MAX_STALE:
            _LOGGER.warning(
                "Using bounded stale regional API discovery after failures in %s",
                ", ".join(failed_regions),
            )
            return _cached_endpoints
        raise RuntimeError("One or more regional API bridges are unavailable")

    _cached_endpoints = endpoints
    _endpoints_cache_time = time.monotonic()
    return endpoints


def _sigv4_headers(region: str, method: str, url: str, body: str | None) -> dict[str, str]:
    """Sign one execute-api request with the Lambda execution-role credentials."""
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("Regional API request credentials are unavailable")
    get_frozen = getattr(credentials, "get_frozen_credentials", None)
    signing_credentials = get_frozen() if callable(get_frozen) else credentials
    request = AWSRequest(
        method=method.upper(),
        url=url,
        data=(body or "").encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(signing_credentials, "execute-api", region).add_auth(request)
    return {str(key): str(value) for key, value in request.headers.items()}


def query_region(
    region: str,
    endpoint: str,
    path: str,
    method: str = "GET",
    body: str | None = None,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Query one regional bridge over AWS-managed TLS with SigV4 authentication."""
    try:
        base_url = _normalize_regional_api_url(endpoint, region)
        query_str = "?" + urlencode(query_params) if query_params else ""
        url = f"{base_url}{path}{query_str}"

        response = http.request(
            method,
            url,
            headers=_sigv4_headers(region, method, url, body),
            body=body.encode("utf-8") if body else None,
            timeout=10.0,
        )

        if response.status == 200:
            data: dict[str, Any] = json.loads(response.data.decode("utf-8"))
            data["_region"] = region
            data["_status"] = "success"
            return data
        if response.status == 503 and path == "/api/v1/health":
            # The regional health API may deliberately report degraded state
            # with 503 while still returning a useful authenticated JSON body.
            try:
                data = json.loads(response.data.decode("utf-8"))
                data["_region"] = region
                data["_status"] = "success"
                return data
            except json.JSONDecodeError, UnicodeDecodeError:
                pass
        return {
            "_region": region,
            "_status": "error",
            "_error": f"HTTP {response.status}",
        }
    except Exception:
        # Never expose credentials, certificate state, API identifiers, or
        # network details through the aggregate API response.
        _LOGGER.exception("Authenticated regional API request failed for %s", region)
        return {
            "_region": region,
            "_status": "error",
            "_error": "Authenticated regional API request failed",
        }


def _job_metadata_text(job: dict[str, Any], key: str) -> str:
    """Return a string metadata field for total, deterministic ordering."""
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get(key)
    return value if isinstance(value, str) else ""


def aggregate_jobs(
    namespace: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Aggregate jobs from all regions."""
    endpoints = get_regional_endpoints()

    query_params: dict[str, str] = {"limit": str(limit * 2)}  # Get more per region, then trim
    if namespace:
        query_params["namespace"] = namespace
    if status:
        query_params["status"] = status

    all_jobs: list[dict[str, Any]] = []
    region_summaries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # Query all regions in parallel
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                query_region, region, endpoint, "/api/v1/jobs", "GET", None, query_params
            ): region
            for region, endpoint in endpoints.items()
        }

        for future in as_completed(futures):
            region = futures[future]
            try:
                result = future.result()
                if result.get("_status") == "success":
                    jobs = result.get("jobs", [])
                    # Add region to each job
                    for job in jobs:
                        job["_source_region"] = region
                    all_jobs.extend(jobs)
                    region_summaries.append(
                        {
                            "region": region,
                            "count": result.get("count", len(jobs)),
                            "total": result.get("total", len(jobs)),
                        }
                    )
                else:
                    errors.append(
                        {
                            "region": region,
                            "error": result.get("_error", "Unknown error"),
                        }
                    )
            except Exception:
                _LOGGER.exception("Unexpected aggregate-jobs failure for %s", region)
                errors.append({"region": region, "error": "Regional request failed"})

    # Canonicalize presentation independently of thread completion order.
    region_summaries.sort(key=lambda item: item["region"])
    errors.sort(key=lambda item: item["region"])
    all_jobs.sort(
        key=lambda job: (
            str(job.get("_source_region") or ""),
            _job_metadata_text(job, "namespace"),
            _job_metadata_text(job, "name"),
            _job_metadata_text(job, "uid"),
        )
    )
    # Stable second pass keeps the tie-breakers ascending within each timestamp.
    all_jobs.sort(
        key=lambda job: _job_metadata_text(job, "creationTimestamp"),
        reverse=True,
    )

    # Trim to limit
    all_jobs = all_jobs[:limit]

    return {
        "total": sum(r["total"] for r in region_summaries),
        "count": len(all_jobs),
        "limit": limit,
        "regions_queried": len(endpoints),
        "regions_successful": len(region_summaries),
        "region_summaries": region_summaries,
        "jobs": all_jobs,
        "errors": errors if errors else None,
    }


def aggregate_metrics() -> dict[str, Any]:
    """Aggregate cluster metrics from all regions."""
    endpoints = get_regional_endpoints()

    region_metrics: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(query_region, region, endpoint, "/api/v1/status"): region
            for region, endpoint in endpoints.items()
        }

        for future in as_completed(futures):
            region = futures[future]
            try:
                result = future.result()
                if result.get("_status") == "success":
                    region_metrics.append(
                        {
                            "region": region,
                            "cluster_id": result.get("cluster_id"),
                            "templates_count": result.get("templates_count", 0),
                            "webhooks_count": result.get("webhooks_count", 0),
                            "resource_limits": result.get("resource_limits", {}),
                            "allowed_namespaces": result.get("allowed_namespaces", []),
                        }
                    )
                else:
                    errors.append(
                        {
                            "region": region,
                            "error": result.get("_error", "Unknown error"),
                        }
                    )
            except Exception:
                _LOGGER.exception("Unexpected aggregate-metrics failure for %s", region)
                errors.append({"region": region, "error": "Regional request failed"})

    region_metrics.sort(key=lambda item: item["region"])
    errors.sort(key=lambda item: item["region"])
    return {
        "regions_queried": len(endpoints),
        "regions_successful": len(region_metrics),
        "regions": region_metrics,
        "errors": errors if errors else None,
    }


def aggregate_health() -> dict[str, Any]:
    """Aggregate health status from all regions."""
    endpoints = get_regional_endpoints()

    region_health: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(query_region, region, endpoint, "/api/v1/health"): region
            for region, endpoint in endpoints.items()
        }

        for future in as_completed(futures):
            region = futures[future]
            try:
                result = future.result()
                if result.get("_status") == "success":
                    region_health.append(
                        {
                            "region": region,
                            "status": result.get("status", "unknown"),
                            "cluster_id": result.get("cluster_id"),
                            "kubernetes_api": result.get("kubernetes_api"),
                        }
                    )
                else:
                    region_health.append(
                        {
                            "region": region,
                            "status": "unreachable",
                            "error": result.get("_error"),
                        }
                    )
            except Exception:
                _LOGGER.exception("Unexpected aggregate-health failure for %s", region)
                region_health.append(
                    {
                        "region": region,
                        "status": "error",
                        "error": "Regional request failed",
                    }
                )

    region_health.sort(key=lambda item: item["region"])
    healthy_count = sum(1 for r in region_health if r["status"] == "healthy")
    overall_status = "healthy" if healthy_count == len(endpoints) else "degraded"
    if healthy_count == 0:
        overall_status = "unhealthy"

    return {
        "overall_status": overall_status,
        "healthy_regions": healthy_count,
        "total_regions": len(endpoints),
        "regions": region_health,
    }


def bulk_delete_jobs(
    namespace: str | None = None,
    status: str | None = None,
    older_than_days: int | None = None,
    label_selector: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Bulk delete jobs across all regions."""
    endpoints = get_regional_endpoints()

    request_body: dict[str, Any] = {
        "dry_run": dry_run,
    }
    if namespace:
        request_body["namespace"] = namespace
    if status:
        request_body["status"] = status
    if older_than_days:
        request_body["older_than_days"] = older_than_days
    if label_selector:
        request_body["label_selector"] = label_selector

    body_str = json.dumps(request_body)

    region_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_deleted = 0
    total_matched = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                query_region, region, endpoint, "/api/v1/jobs", "DELETE", body_str
            ): region
            for region, endpoint in endpoints.items()
        }

        for future in as_completed(futures):
            region = futures[future]
            try:
                result = future.result()
                if result.get("_status") == "success":
                    region_results.append(
                        {
                            "region": region,
                            "matched": result.get("total_matched", 0),
                            "deleted": result.get("deleted_count", 0),
                            "failed": result.get("failed_count", 0),
                        }
                    )
                    total_matched += result.get("total_matched", 0)
                    total_deleted += result.get("deleted_count", 0)
                else:
                    errors.append(
                        {
                            "region": region,
                            "error": result.get("_error", "Unknown error"),
                        }
                    )
            except Exception:
                _LOGGER.exception("Unexpected bulk-delete failure for %s", region)
                errors.append({"region": region, "error": "Regional request failed"})

    region_results.sort(key=lambda item: item["region"])
    errors.sort(key=lambda item: item["region"])
    return {
        "dry_run": dry_run,
        "total_matched": total_matched,
        "total_deleted": total_deleted,
        "regions_queried": len(endpoints),
        "region_results": region_results,
        "errors": errors if errors else None,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Handle cross-region aggregation requests.

    Routes:
        GET /global/jobs - List jobs across all regions
        GET /global/health - Health status across all regions
        GET /global/status - Cluster status across all regions
        DELETE /global/jobs - Bulk delete across all regions
    """
    http_method = event.get("httpMethod", "GET")
    path = event.get("path", "")
    query_params = event.get("queryStringParameters") or {}
    body = event.get("body")

    try:
        # Route to appropriate handler
        if path == "/api/v1/global/jobs" and http_method == "GET":
            result = aggregate_jobs(
                namespace=query_params.get("namespace"),
                status=query_params.get("status"),
                limit=int(query_params.get("limit", "50")),
            )
        elif path == "/api/v1/global/jobs" and http_method == "DELETE":
            body_data = json.loads(body) if body else {}
            result = bulk_delete_jobs(
                namespace=body_data.get("namespace"),
                status=body_data.get("status"),
                older_than_days=body_data.get("older_than_days"),
                label_selector=body_data.get("label_selector"),
                dry_run=body_data.get("dry_run", True),
            )
        elif path == "/api/v1/global/health":
            result = aggregate_health()
        elif path == "/api/v1/global/status":
            result = aggregate_metrics()
        else:
            return {
                "statusCode": 404,
                "body": json.dumps({"error": "Not found", "path": path}),
            }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result),
        }

    except RuntimeError:
        _LOGGER.exception("Regional aggregation bridge discovery failed")
        return {
            "statusCode": 503,
            "body": json.dumps({"error": "Regional aggregation is temporarily unavailable"}),
        }
    except Exception:
        _LOGGER.exception("Unhandled regional aggregation request failure")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error"}),
        }
