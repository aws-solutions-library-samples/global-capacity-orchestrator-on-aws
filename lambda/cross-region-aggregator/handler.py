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
# Generated at (UTC): 2026-11-18T16:59:04Z
# Generated from Git commit: 3d3ba1381c5c16d0c44942f3b542591f571c3f1f
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
# subsequent ALB connection. urllib3's own retries are off: they re-sent a
# timed-out GET or DELETE up to three more times, each with a fresh timeout,
# which outlasted the function's 29-second timeout (the whole request then
# failed at API Gateway instead of one Region being reported as an error) and
# re-sent a bulk DELETE while the first was still running.
http = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", retries=False)

#: Kept back from the invocation's remaining time to assemble and return the
#: aggregate response. Every regional request must finish before that.
_RESPONSE_HEADROOM_SECONDS = 2.0
#: The regional request budget when the remaining time is unknown: the
#: function's 29-second timeout less the headroom.
_DEFAULT_REQUEST_BUDGET_SECONDS = 27.0
#: The regional jobs API's own ``limit`` ceiling (gco/services/api_routes/jobs.py).
_MAX_JOBS_LIMIT = 1000
#: The longest regional error message an aggregate answer repeats.
_MAX_REGION_ERROR_CHARS = 300
#: Where a regional error body carries its message: the GCO API's ``error``
#: and ``detail``, API Gateway's ``message``, and its ``Message`` when IAM
#: authorization denies the request.
_ERROR_MESSAGE_KEYS = ("error", "message", "Message", "detail")
#: Names an error message may quote that the aggregate answer must not repeat:
#: ARNs (accounts, roles, API IDs) and access key IDs.
_ARN_RE = re.compile(r"\barn:aws[\w-]*:\S*")
_ACCESS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

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
    if not isinstance(configured, list):
        raise RuntimeError("Regional API discovery is not configured")
    # An empty list is a control-plane-only deployment (zero workload Regions):
    # discovery finds no bridges and every aggregation answers with empty
    # results rather than a 503.

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


class _BadRequest(ValueError):
    """Client input the aggregator rejects with 400."""


def _region_error(status: int, data: Any) -> str:
    """``HTTP <status>``, followed by the regional API's message when it sent one.

    The message is the first string under :data:`_ERROR_MESSAGE_KEYS` in a
    JSON body, made safe to repeat. Only its first line is kept, because API
    Gateway's signature-mismatch message goes on to quote the canonical
    request, security token included. Anything from ``Authorization=`` on is
    dropped, because its malformed-signature message ends with that header.
    ARNs and access key IDs are masked, control characters become spaces, and
    the text is cut to :data:`_MAX_REGION_ERROR_CHARS`.
    """
    summary = f"HTTP {status}"
    if not isinstance(data, bytes):
        return summary
    try:
        body = json.loads(data.decode("utf-8"))
    except ValueError:  # not UTF-8 or not JSON: a load balancer's HTML page, say
        return summary
    if not isinstance(body, dict):
        return summary
    message = next((body[key] for key in _ERROR_MESSAGE_KEYS if isinstance(body.get(key), str)), "")
    first_line = (message.strip().splitlines() or [""])[0]
    first_line = first_line.split("Authorization=", 1)[0]
    first_line = _ACCESS_KEY_ID_RE.sub("<access-key-id>", _ARN_RE.sub("<arn>", first_line))
    text = " ".join("".join(char if char.isprintable() else " " for char in first_line).split())
    if not text:
        return summary
    if len(text) > _MAX_REGION_ERROR_CHARS:
        text = text[: _MAX_REGION_ERROR_CHARS - 3] + "..."
    return f"{summary}: {text}"


def _request_deadline(context: Any) -> float:
    """The ``time.monotonic()`` by which every regional request must finish."""
    get_remaining_time = getattr(context, "get_remaining_time_in_millis", None)
    budget = _DEFAULT_REQUEST_BUDGET_SECONDS
    if callable(get_remaining_time):
        budget = float(get_remaining_time()) / 1000.0 - _RESPONSE_HEADROOM_SECONDS
    return time.monotonic() + max(0.0, budget)


def query_region(
    region: str,
    endpoint: str,
    path: str,
    method: str = "GET",
    body: str | None = None,
    query_params: dict[str, str] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Query one regional bridge over AWS-managed TLS with SigV4 authentication.

    The request is sent once and must finish by ``deadline`` (a
    ``time.monotonic()`` value; the default budget from now when omitted), so
    a slow Region is reported as an error while the aggregate still answers.
    """
    remaining = (
        deadline - time.monotonic() if deadline is not None else _DEFAULT_REQUEST_BUDGET_SECONDS
    )
    if remaining <= 0:
        return {
            "_region": region,
            "_status": "error",
            "_error": "No time left to query the region",
        }
    try:
        base_url = _normalize_regional_api_url(endpoint, region)
        query_str = "?" + urlencode(query_params) if query_params else ""
        url = f"{base_url}{path}{query_str}"

        response = http.request(
            method,
            url,
            headers=_sigv4_headers(region, method, url, body),
            body=body.encode("utf-8") if body else None,
            timeout=urllib3.Timeout(total=remaining),
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
            "_error": _region_error(response.status, response.data),
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
    deadline: float | None = None,
) -> dict[str, Any]:
    """Aggregate jobs from all regions."""
    endpoints = get_regional_endpoints()

    # Get more per region, then trim; the regional API caps its own limit.
    query_params: dict[str, str] = {"limit": str(min(limit * 2, _MAX_JOBS_LIMIT))}
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
                query_region,
                region,
                endpoint,
                "/api/v1/jobs",
                "GET",
                None,
                query_params,
                deadline=deadline,
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


def aggregate_metrics(deadline: float | None = None) -> dict[str, Any]:
    """Aggregate cluster metrics from all regions."""
    endpoints = get_regional_endpoints()

    region_metrics: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                query_region, region, endpoint, "/api/v1/status", deadline=deadline
            ): region
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


def aggregate_health(deadline: float | None = None) -> dict[str, Any]:
    """Aggregate health status from all regions."""
    endpoints = get_regional_endpoints()

    region_health: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(
                query_region, region, endpoint, "/api/v1/health", deadline=deadline
            ): region
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
    deadline: float | None = None,
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
                query_region,
                region,
                endpoint,
                "/api/v1/jobs",
                "DELETE",
                body_str,
                deadline=deadline,
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


def _jobs_limit(value: Any) -> int:
    """The ``limit`` query parameter: an integer from 1 to the regional ceiling."""
    if value is None:
        return 50
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise _BadRequest("limit must be an integer") from exc
    if not 1 <= limit <= _MAX_JOBS_LIMIT:
        raise _BadRequest(f"limit must be between 1 and {_MAX_JOBS_LIMIT}")
    return limit


def _json_object_body(body: str | None) -> dict[str, Any]:
    """The request body as a JSON object; an absent body is an empty one."""
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise _BadRequest("Request body must be a JSON object") from exc
    if not isinstance(data, dict):
        raise _BadRequest("Request body must be a JSON object")
    return data


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Handle cross-region aggregation requests.

    Routes:
        GET /global/jobs - List jobs across all regions
        GET /global/health - Health status across all regions
        GET /global/status - Cluster status across all regions
        DELETE /global/jobs - Bulk delete across all regions

    Every regional request must finish ``_RESPONSE_HEADROOM_SECONDS`` before
    the invocation's deadline. A malformed ``limit`` or DELETE body is a 400.
    """
    deadline = _request_deadline(context)
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
                limit=_jobs_limit(query_params.get("limit")),
                deadline=deadline,
            )
        elif path == "/api/v1/global/jobs" and http_method == "DELETE":
            body_data = _json_object_body(body)
            result = bulk_delete_jobs(
                namespace=body_data.get("namespace"),
                status=body_data.get("status"),
                older_than_days=body_data.get("older_than_days"),
                label_selector=body_data.get("label_selector"),
                dry_run=body_data.get("dry_run", True),
                deadline=deadline,
            )
        elif path == "/api/v1/global/health":
            result = aggregate_health(deadline=deadline)
        elif path == "/api/v1/global/status":
            result = aggregate_metrics(deadline=deadline)
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

    except _BadRequest as exc:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": str(exc)}),
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
