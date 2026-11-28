"""Every read and action the GCO dashboard performs, as plain Python.

The dashboard's pages never talk to AWS themselves: they call this module,
which reaches the fleet through the same managers and API client the ``gco``
commands use (SigV4-signed API Gateway requests, the CloudFormation-discovered
endpoints, the inference registry, Cost Explorer). Nothing here imports
Streamlit, so every function is testable with plain mocks and reusable from a
script.

Each call builds its own clients. ``boto3`` sessions are not safe to share
between threads, and Streamlit runs every browser session (and every region
fan-out below) on its own thread, so a fresh session per call is the simple
correct choice; the dashboard caches results, not clients.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from cli.aws_client import (
    ApiEndpoint,
    APIRequestError,
    GCOAWSClient,
    ManifestSubmissionError,
    RegionalApiDiscoveryError,
    api_path_segment,
    checked_api_sub_path,
)
from cli.config import GCOConfig, _load_cdk_json, _project_dir
from cli.jobs import JobInfo, get_job_manager, parse_job_info

logger = logging.getLogger(__name__)

# Endpoint discovery is a CloudFormation DescribeStacks call per API, and its
# answer only changes when a stack is redeployed. The dashboard builds a fresh
# client per read (see the module docstring), so the answers are shared here,
# across clients and threads, for the CLI's own cache TTL.
_DISCOVERY_LOCK = threading.Lock()
_GLOBAL_ENDPOINTS: dict[tuple[str, str], tuple[float, ApiEndpoint]] = {}
_REGIONAL_ENDPOINTS: dict[tuple[str, str], tuple[float, ApiEndpoint | None]] = {}


def clear_discovery_cache() -> None:
    """Forget every discovered API endpoint (the next read rediscovers)."""
    with _DISCOVERY_LOCK:
        _GLOBAL_ENDPOINTS.clear()
        _REGIONAL_ENDPOINTS.clear()


class SharedDiscoveryClient(GCOAWSClient):
    """The CLI's signed API client, with endpoint discovery shared between instances."""

    def get_api_endpoint(self, force_refresh: bool = False) -> ApiEndpoint:
        key = (self.config.api_gateway_stack_name, self.config.api_gateway_region)
        now = time.monotonic()
        with _DISCOVERY_LOCK:
            hit = _GLOBAL_ENDPOINTS.get(key)
        if hit is not None and hit[0] > now and not force_refresh:
            return hit[1]
        endpoint = super().get_api_endpoint(force_refresh=force_refresh)
        with _DISCOVERY_LOCK:
            _GLOBAL_ENDPOINTS[key] = (now + self.config.cache_ttl_seconds, endpoint)
        return endpoint

    def get_regional_api_endpoint(
        self, region: str, force_refresh: bool = False
    ) -> ApiEndpoint | None:
        key = (self.config.project_name, region)
        now = time.monotonic()
        with _DISCOVERY_LOCK:
            hit = _REGIONAL_ENDPOINTS.get(key)
        if hit is not None and hit[0] > now and not force_refresh:
            return hit[1]
        endpoint = super().get_regional_api_endpoint(region, force_refresh=force_refresh)
        with _DISCOVERY_LOCK:
            _REGIONAL_ENDPOINTS[key] = (now + self.config.cache_ttl_seconds, endpoint)
        return endpoint


#: Ceiling on concurrent per-Region reads (the CLI's status fan-out uses the same).
MAX_FANOUT = 8

#: The health monitor's CloudWatch namespace and the series it publishes per cluster.
HEALTH_NAMESPACE = "GCO/HealthMonitor"
UTILIZATION_METRICS: dict[str, str] = {
    "cpu": "ClusterCpuUtilization",
    "memory": "ClusterMemoryUtilization",
    "gpu": "ClusterGpuUtilization",
    "active_jobs": "ActiveJobs",
}

#: API routes the console page may read. Each is a GET with no side effects.
CONSOLE_ROUTES: dict[str, str] = {
    "/api/v1/global/health": "Health of every Region (cross-Region aggregator)",
    "/api/v1/global/status": "Limits and namespaces of every Region",
    "/api/v1/global/jobs": "Kubernetes Jobs in every Region",
    "/api/v1/queue/stats": "Central job queue counts by Region and status",
    "/api/v1/queue/jobs": "Central job queue records",
    "/api/v1/jobs": "Kubernetes Jobs in one Region",
    "/api/v1/health": "One Region's health monitor verdict",
    "/api/v1/metrics": "One Region's resource utilization",
    "/api/v1/status": "One Region's manifest processor status",
    "/api/v1/policy": "One Region's enforced job validation policy",
    "/api/v1/templates": "Job templates",
    "/api/v1/webhooks": "Registered webhooks",
    "/api/v1/cost/status": "Cost monitor status",
    "/api/v1/cost/reports": "Cost allocation reports",
}

#: Console routes that answer only from the global API.
GLOBAL_ONLY_ROUTES = frozenset(
    {"/api/v1/global/health", "/api/v1/global/status", "/api/v1/global/jobs"}
)

_EXPIRED_CREDENTIAL_CODES = frozenset(
    {
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "UnauthorizedSSOToken",
        "TokenRefreshRequired",
    }
)

_EXPIRED_HINT = "Your session has probably expired: sign in again and refresh."
_DENIED_HINT = "The signed-in principal lacks a permission this view needs."


class DashboardError(RuntimeError):
    """A read or action failed; ``hint`` tells the operator what to do next."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def describe_error(exc: BaseException) -> DashboardError:
    """Turn any failure from the CLI layers into a message an operator can act on."""
    known = _describe_known(exc)
    if known is not None:
        return known
    # Anything else usually wraps the real failure: the API client reports a
    # failed endpoint discovery as RuntimeError("Failed to get API endpoint:
    # ...") raised from the ClientError. The wrapper says what failed; what it
    # wraps says what to do about it.
    for cause in _chain(exc):
        wrapped = _describe_known(cause)
        if wrapped is not None and wrapped.hint:
            return DashboardError(str(exc) or str(wrapped), hint=wrapped.hint)
    return DashboardError(f"{type(exc).__name__}: {exc}")


def _describe_known(exc: BaseException) -> DashboardError | None:
    """:func:`describe_error` for the failures it recognizes by type, else None."""
    if isinstance(exc, DashboardError):
        return exc
    if isinstance(exc, NoCredentialsError) or (
        isinstance(exc, RuntimeError) and "No AWS credentials" in str(exc)
    ):
        return DashboardError(
            "No AWS credentials were found.",
            hint="Sign in (for example `aws sso login`) and refresh the page.",
        )
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "ClientError"))
        message = str(error.get("Message", "")).strip() or code
        if code in _EXPIRED_CREDENTIAL_CODES:
            return DashboardError(f"AWS rejected the credentials ({code}).", hint=_EXPIRED_HINT)
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            return DashboardError(f"AWS denied the request: {message}", hint=_DENIED_HINT)
        return DashboardError(f"{code}: {message}")
    if isinstance(exc, BotoCoreError):
        return DashboardError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, RegionalApiDiscoveryError):
        if isinstance(exc.__cause__, BotoCoreError | ClientError):
            # CloudFormation could not be asked (credentials, permissions,
            # network), so nothing says the bridge is missing.
            return DashboardError(str(exc), hint=describe_error(exc.__cause__).hint)
        return DashboardError(
            str(exc), hint="Deploy the regional API bridge with `gco stacks deploy-all`."
        )
    if isinstance(exc, ManifestSubmissionError):
        return DashboardError(
            f"The API refused the manifests ({exc.status_code}): {exc}",
            hint=_gateway_hint(exc.status_code, str(exc)),
        )
    if isinstance(exc, APIRequestError):
        hint = (
            "The API Gateway front door timed out; try again."
            if exc.status_code == 504
            else _gateway_hint(exc.status_code, str(exc))
        )
        return DashboardError(f"{exc} (HTTP {exc.status_code})", hint=hint)
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        status = response.status_code if response is not None else "?"
        detail = _response_detail(response) if response is not None else str(exc)
        return DashboardError(
            f"The API answered HTTP {status}: {detail}", hint=_gateway_hint(status, detail)
        )
    if isinstance(exc, requests.RequestException):
        return DashboardError(f"Could not reach the GCO API: {type(exc).__name__}")
    return None


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """What ``exc`` was raised from, or while handling, nearest first."""
    seen = {id(exc)}
    current = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    # A chain built by hand can loop back on itself; each link is visited once.
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def _gateway_hint(status: object, detail: str) -> str | None:
    """The hint for a 403 that API Gateway sent itself, told apart by its message.

    API Gateway answers a request signed by an expired or revoked session
    with "The security token included in the request is expired" (or "is
    invalid"), and a principal without ``execute-api:Invoke`` with "User: ...
    is not authorized to perform: execute-api:Invoke". The GCO API's own 403s
    (a namespace it does not allow, say) say neither.
    """
    if status != 403:
        return None
    text = detail.lower()
    if "security token included in the request" in text:
        return _EXPIRED_HINT
    if "not authorized to perform" in text:
        return _DENIED_HINT
    return None


def _with_hint(error: DashboardError) -> str:
    """The error's message with its hint after it, for a page that prints one line."""
    message = str(error)
    if not error.hint:
        return message
    if not message.endswith((".", "!", "?")):
        message += "."
    return f"{message} {error.hint}"


def _response_detail(response: requests.Response) -> str:
    """The API's own account of a failed request, bounded for display."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or response.reason or "no detail")[:400]
    if isinstance(payload, dict):
        # API Gateway capitalises the key when IAM denies the caller.
        for key in ("detail", "error", "message", "Message"):
            if payload.get(key):
                return str(payload[key])[:400]
    return json.dumps(payload)[:400]


@dataclass(frozen=True)
class Topology:
    """The deployment the dashboard is looking at, as cdk.json describes it."""

    project: str
    global_region: str
    api_gateway_region: str
    monitoring_region: str
    workload_regions: tuple[str, ...]
    configured: bool
    regional_api: bool


@dataclass(frozen=True)
class RegionRead:
    """One Region's answer in a fan-out: the payload, or why there is none.

    ``error`` ends with the failure's hint, if it has one, so a page that
    prints it tells the operator what to do as well as what went wrong.
    """

    region: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class JobsSnapshot:
    """Kubernetes Jobs across Regions, and the Regions that could not be read."""

    jobs: list[JobInfo]
    errors: dict[str, str]
    regions_queried: int
    total: int
    source: str


class UtilizationRows(list[dict[str, Any]]):
    """Utilization samples, one dict each, and the Regions whose series could not be read.

    ``errors`` maps each of those Regions to why. Being a list, the samples go
    wherever a list of rows does, and the failures travel with them.
    """

    def __init__(
        self, rows: Iterable[dict[str, Any]] = (), errors: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(rows)
        self.errors: dict[str, str] = dict(errors or {})


@dataclass(frozen=True)
class ExampleManifest:
    """A manifest file shipped under ``examples/`` in the checkout."""

    name: str
    path: str
    kinds: tuple[str, ...]
    gpu: bool
    title: str = ""
    summary: str = ""
    prerequisites: str = ""


@dataclass(frozen=True)
class ServingDefaults:
    """What an inference example serves: the deploy dialog's preset for its runtime.

    A field the example does not state keeps its default: no image (the
    operator names one), no model, and the port and health path
    ``gco inference deploy`` assumes.
    """

    image: str = ""
    port: int = 8000
    health: str = "/health"
    model: str = ""


#: A bullet (``- item``) or numbered (``1. item``) line in an example's header.
_LIST_ITEM = re.compile(r"-|\d+[.)]\s")


def example_header(text: str) -> tuple[str, str, str]:
    """An example's title, first paragraph and prerequisites, from its comment header.

    Every example opens with a ``#`` block: a title line, a paragraph saying
    what it does, then labelled sections (``Prerequisites:``, ``Usage:``).
    """
    lines: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            if line.strip():
                break
            continue
        lines.append(line[1:].strip())
    if not lines:
        return "", "", ""
    title = lines[0]
    summary: list[str] = []
    for line in lines[1:]:
        if not line:
            if summary:
                break
            continue
        if line.endswith(":") and summary:
            break
        summary.append(line)
    prerequisites: list[str] = []
    collecting = False
    for line in lines:
        if line.lower().startswith("prerequisites"):
            collecting = True
            continue
        if collecting:
            if not line or (line.endswith(":") and not line.startswith("-")):
                break
            if prerequisites and not _LIST_ITEM.match(line):
                # A wrapped line continues the item above it.
                prerequisites[-1] = f"{prerequisites[-1]} {line}".strip()
                continue
            prerequisites.append(line.lstrip("- ").strip())
    return title, " ".join(summary), "; ".join(p for p in prerequisites if p)


def _object(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty dict (API fields can be null)."""
    return dict(value) if isinstance(value, Mapping) else {}


def _objects(value: object) -> list[dict[str, Any]]:
    """The JSON objects in ``value`` when it is a list, else none (API lists can be null)."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: object) -> str | None:
    """``value`` when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _fan_out(regions: Sequence[str], read: Callable[[str], dict[str, Any]]) -> list[RegionRead]:
    """Run ``read`` for every Region in parallel; one Region's failure stays its own."""

    def guarded(region: str) -> RegionRead:
        try:
            return RegionRead(region=region, data=read(region))
        except Exception as exc:
            return RegionRead(region=region, error=_with_hint(describe_error(exc)))

    if not regions:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_FANOUT, len(regions))) as pool:
        return list(pool.map(guarded, regions))


def _by_error(errors: Mapping[str, str]) -> str:
    """``region, region: why; region: why``: Regions that failed alike are named together.

    Every Region usually fails the same way (an expired session, say), and
    each error ends with its hint, so this says it once.
    """
    regions: dict[str, list[str]] = {}
    for region, error in errors.items():
        regions.setdefault(error, []).append(region)
    return "; ".join(f"{', '.join(names)}: {error}" for error, names in regions.items())


def _job_errors(raw: object) -> dict[str, str]:
    """``{region: error}`` from the aggregator's ``errors`` list (null when none)."""
    errors: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("region"):
                errors[str(item["region"])] = str(item.get("error") or "unreadable")
    return errors


def manifest_requests_gpu(document: object) -> bool:
    """Whether a manifest asks for an NVIDIA GPU anywhere in its pod templates."""
    if isinstance(document, dict):
        return any(
            key == "nvidia.com/gpu" or manifest_requests_gpu(value)
            for key, value in document.items()
        )
    if isinstance(document, list):
        return any(manifest_requests_gpu(item) for item in document)
    return False


def load_manifest_text(text: str) -> list[dict[str, Any]]:
    """Parse YAML (one or more documents) into manifests, refusing anything else."""
    from gco.services.manifest_processor import safe_load_all_yaml

    try:
        documents = safe_load_all_yaml(text, allow_aliases=False)
    except Exception as exc:
        raise DashboardError(f"The YAML does not parse: {exc}") from exc
    manifests = [doc for doc in documents if doc is not None]
    if not manifests:
        raise DashboardError("The YAML holds no manifests.")
    if not all(isinstance(doc, dict) and doc.get("kind") for doc in manifests):
        raise DashboardError("Every YAML document must be a Kubernetes object with a kind.")
    return manifests


def serving_defaults(
    documents: Iterable[object], *, source: str = "the manifests"
) -> ServingDefaults:
    """What the first container of the first Deployment in ``documents`` serves.

    Its image, its first ``containerPort``, its readiness probe's
    ``httpGet.path`` and its ``MODEL`` variable. A field the container does
    not state keeps its default; a missing image, port or health path is
    logged at debug, naming ``source``.
    """
    deployment = next(
        (doc for doc in documents if isinstance(doc, dict) and doc.get("kind") == "Deployment"),
        {},
    )
    pod = _object(_object(_object(deployment.get("spec")).get("template")).get("spec"))
    container = next(iter(_objects(pod.get("containers"))), {})
    image = _text(container.get("image"))
    ports = [entry.get("containerPort") for entry in _objects(container.get("ports"))]
    port = next((n for n in ports if type(n) is int and 0 < n < 65536), None)
    health = _text(_object(_object(container.get("readinessProbe")).get("httpGet")).get("path"))
    env = _objects(container.get("env"))
    model = next((_text(var.get("value")) for var in env if var.get("name") == "MODEL"), None)
    stated = (("image", image), ("containerPort", port), ("readiness probe path", health))
    missing = [what for what, value in stated if value is None]
    if missing:
        logger.debug("No %s in %s; the defaults stand in", ", ".join(missing), source)
    fallback = ServingDefaults()
    return ServingDefaults(
        image=image or fallback.image,
        port=port or fallback.port,
        health=health or fallback.health,
        model=model or fallback.model,
    )


class FleetData:
    """The dashboard's single entry point to a GCO deployment."""

    def __init__(self, config: GCOConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    def client(self) -> GCOAWSClient:
        """A fresh signed API client (see the module docstring for why fresh)."""
        return SharedDiscoveryClient(self.config)

    def session(self) -> boto3.Session:
        """A fresh boto3 session for direct AWS reads."""
        return boto3.Session()

    # ------------------------------------------------------------------
    # Topology and identity
    # ------------------------------------------------------------------

    def topology(self) -> Topology:
        """Regions from cdk.json, with the CLI's own defaults where it is silent."""
        regions = _load_cdk_json()
        workload = tuple(r for r in regions.get("regional", []) if isinstance(r, str))
        return Topology(
            project=self.config.project_name,
            global_region=str(regions.get("global", self.config.global_region)),
            api_gateway_region=str(regions.get("api_gateway", self.config.api_gateway_region)),
            monitoring_region=str(regions.get("monitoring", self.config.monitoring_region)),
            workload_regions=workload,
            configured=bool(workload),
            regional_api=bool(self.config.use_regional_api),
        )

    def identity(self) -> dict[str, str]:
        """Who the dashboard is signed in as (STS GetCallerIdentity)."""
        response = self.session().client("sts").get_caller_identity()
        arn = str(response.get("Arn", ""))
        return {
            "arn": arn,
            "account": str(response.get("Account", "")),
            "principal": arn.rsplit("/", 1)[-1] if "/" in arn else arn,
        }

    # ------------------------------------------------------------------
    # Fleet status (the same document `gco status` prints)
    # ------------------------------------------------------------------

    def fleet_status(
        self, *, with_costs: bool = False, with_policy: bool = False
    ) -> dict[str, Any]:
        from cli.status import gather_fleet_status

        document = gather_fleet_status(self.config, with_costs=with_costs, with_policy=with_policy)
        return dataclasses.asdict(document)

    def cluster_metrics(self, regions: Sequence[str]) -> list[RegionRead]:
        """Each Region's live health-monitor reading (``GET /api/v1/metrics``)."""
        return _fan_out(
            regions,
            lambda region: self.client().call_api(
                "GET", "/api/v1/metrics", region=region, max_attempts=1
            ),
        )

    def utilization_history(
        self, regions: Sequence[str], *, hours: int = 3, period: int = 60
    ) -> UtilizationRows:
        """Per-minute utilization series the health monitors publish to CloudWatch.

        A Region that cannot be read is named in the rows' ``errors``; only
        when no Region has a sample is a failure an error.
        """
        end = datetime.now(UTC)
        start = end - timedelta(hours=hours)

        def read(region: str) -> dict[str, Any]:
            cluster = f"{self.config.project_name}-{region}"
            queries = [
                {
                    "Id": metric,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": HEALTH_NAMESPACE,
                            "MetricName": name,
                            "Dimensions": [
                                {"Name": "ClusterName", "Value": cluster},
                                {"Name": "Region", "Value": region},
                            ],
                        },
                        "Period": period,
                        "Stat": "Average",
                    },
                }
                for metric, name in UTILIZATION_METRICS.items()
            ]
            cloudwatch = self.session().client("cloudwatch", region_name=region)
            series: list[dict[str, Any]] = []
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "MetricDataQueries": queries,
                    "StartTime": start,
                    "EndTime": end,
                    "ScanBy": "TimestampAscending",
                }
                if token:
                    kwargs["NextToken"] = token
                response = cloudwatch.get_metric_data(**kwargs)
                for result in response.get("MetricDataResults", []):
                    for stamp, value in zip(
                        result.get("Timestamps", []), result.get("Values", []), strict=False
                    ):
                        series.append(
                            {
                                "region": region,
                                "metric": result.get("Id"),
                                "timestamp": stamp,
                                "value": float(value),
                            }
                        )
                token = response.get("NextToken")
                if not token:
                    break
            return {"series": series}

        reads = _fan_out(regions, read)
        rows = UtilizationRows(
            (row for result in reads if result.ok for row in result.data["series"]),
            {result.region: str(result.error) for result in reads if not result.ok},
        )
        if rows.errors and not rows:
            raise DashboardError("Utilization history is unavailable: " + _by_error(rows.errors))
        return rows

    # ------------------------------------------------------------------
    # Kubernetes Jobs
    # ------------------------------------------------------------------

    def jobs(self, regions: Sequence[str], *, limit: int = 200) -> JobsSnapshot:
        """Jobs in every Region: the cross-Region aggregator, or a per-Region fan-out.

        Regional API mode has no global aggregation route, so each Region's
        bridge is read directly instead; the result has the same shape.
        """
        if self.config.use_regional_api or not regions:
            reads = _fan_out(
                regions,
                lambda region: self.client().get_jobs(region=region, limit=min(limit, 1000)),
            )
            jobs = [
                parse_job_info(job, read.region)
                for read in reads
                if read.ok
                for job in _objects(read.data.get("jobs"))
            ]
            errors = {read.region: str(read.error) for read in reads if not read.ok}
            total = sum(int(read.data.get("total", 0) or 0) for read in reads if read.ok)
            return JobsSnapshot(jobs, errors, len(regions), total, "regional")

        result = self.client().get_global_jobs(limit=min(limit, 1000))
        jobs = [
            parse_job_info(job, str(job.get("_source_region") or ""))
            for job in _objects(result.get("jobs"))
        ]
        return JobsSnapshot(
            jobs=jobs,
            errors=_job_errors(result.get("errors")),
            regions_queried=int(result.get("regions_queried", 0) or 0),
            total=int(result.get("total", len(jobs)) or 0),
            source="global",
        )

    def job_detail(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        return self.client().get_job_details(name, namespace, region=region)

    def job_pods(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        return self.client().get_job_pods(name, namespace, region)

    def job_events(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        return self.client().get_job_events(name, namespace, region)

    def job_metrics(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        return self.client().get_job_metrics(name, namespace, region)

    def job_logs(
        self,
        name: str,
        namespace: str,
        region: str,
        *,
        tail: int = 200,
        container: str | None = None,
    ) -> str:
        """Job logs, with the CLI's TrainJob and CloudWatch fallbacks."""
        return get_job_manager(self.config).get_job_logs(
            name, namespace, region=region, tail_lines=tail, container=container
        )

    def submit_job(
        self,
        manifests: Sequence[Mapping[str, Any]],
        *,
        region: str,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply manifests through ``POST /api/v1/manifests`` (``gco jobs submit``)."""
        return get_job_manager(self.config).submit_job(
            [copy.deepcopy(dict(m)) for m in manifests],
            namespace=namespace,
            target_region=region,
            dry_run=dry_run,
            labels=dict(labels) if labels else None,
        )

    def submit_job_sqs(
        self,
        manifests: Sequence[Mapping[str, Any]],
        *,
        region: str,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """Queue manifests on the Region's SQS queue (``gco jobs submit-sqs``)."""
        return get_job_manager(self.config).submit_job_sqs(
            [copy.deepcopy(dict(m)) for m in manifests],
            region=region,
            namespace=namespace,
            labels=dict(labels) if labels else None,
            priority=priority,
        )

    def recommend_region(
        self,
        manifests: Sequence[Mapping[str, Any]] = (),
        *,
        instance_type: str | None = None,
        gpu_count: int | None = None,
        backend: str | None = None,
    ) -> dict[str, Any]:
        """Where the capacity-aware placement would send this work right now.

        The GPU count and backend come from the manifests (``gco capacity
        recommend-region`` reads them the same way) unless given explicitly.
        """
        from cli.capacity import get_capacity_checker, manifest_placement

        hints = manifest_placement([dict(m) for m in manifests])
        gpus = hints.gpu_count if gpu_count is None else gpu_count
        return get_capacity_checker(self.config).recommend_region_for_job(
            gpu_required=gpus > 0,
            gpu_count=gpus,
            instance_type=instance_type or None,
            backend=backend or hints.backend,
        )

    def delete_job(
        self, name: str, namespace: str, region: str, *, uid: str | None = None
    ) -> dict[str, Any]:
        """Delete one Job; ``uid`` refuses to delete a newer Job of the same name."""
        return get_job_manager(self.config).delete_job(
            name, namespace, region=region, expected_uid=uid
        )

    def retry_job(self, name: str, namespace: str, region: str) -> dict[str, Any]:
        return get_job_manager(self.config).retry_job(name, namespace, region=region)

    def cleanup_jobs(
        self,
        *,
        status: str,
        older_than_days: int | None,
        namespace: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Bulk-delete finished Jobs in every Region (dry run reports matches only).

        The answer has the cross-Region aggregator's shape in both API modes:
        the totals, ``region_results`` (each answering Region's matched,
        deleted and failed counts; a Job that matched but could not be
        deleted is failed) and ``errors`` (the Regions that did not answer).
        Regional API mode has no aggregator, so each Region's bridge is asked
        instead and the answers are summed here.
        """
        if self.config.use_regional_api:
            regions = self.topology().workload_regions
            reads = _fan_out(
                regions,
                lambda region: get_job_manager(self.config).bulk_delete_jobs(
                    namespace=namespace,
                    status=status,
                    older_than_days=older_than_days,
                    region=region,
                    dry_run=dry_run,
                ),
            )
            # A count the bridge answers as null is none.
            results = [
                {
                    "region": read.region,
                    "matched": int(read.data.get("total_matched") or 0),
                    "deleted": int(read.data.get("deleted_count") or 0),
                    "failed": int(read.data.get("failed_count") or 0),
                }
                for read in reads
                if read.ok
            ]
            return {
                "dry_run": dry_run,
                "total_matched": sum(result["matched"] for result in results),
                "total_deleted": sum(result["deleted"] for result in results),
                "regions_queried": len(regions),
                "region_results": results,
                "errors": [{"region": r.region, "error": r.error} for r in reads if not r.ok]
                or None,
            }
        return get_job_manager(self.config).bulk_delete_global(
            namespace=namespace,
            status=status,
            older_than_days=older_than_days,
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------
    # Central job queue (DynamoDB)
    # ------------------------------------------------------------------

    def _queue_region(self) -> str | None:
        return self.config.default_region if self.config.use_regional_api else None

    def queue_jobs(
        self,
        *,
        status: str | None = None,
        target_region: str | None = None,
        namespace: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        params = {"limit": str(min(limit, 1000))}
        if status:
            params["status"] = status
        if target_region:
            params["target_region"] = target_region
        if namespace:
            params["namespace"] = namespace
        return self.client().call_api(
            "GET", "/api/v1/queue/jobs", region=self._queue_region(), params=params
        )

    def queue_job(self, job_id: str) -> dict[str, Any]:
        result = self.client().call_api(
            "GET", f"/api/v1/queue/jobs/{api_path_segment(job_id)}", region=self._queue_region()
        )
        job = result.get("job")
        return job if isinstance(job, dict) else {}

    def queue_stats(self) -> dict[str, Any]:
        return self.client().call_api("GET", "/api/v1/queue/stats", region=self._queue_region())

    def queue_submit(
        self,
        manifest: Mapping[str, Any],
        *,
        region: str,
        namespace: str | None = None,
        priority: int = 0,
        labels: Mapping[str, str] | None = None,
        max_spot_price: float | None = None,
        spot_instance_type: str | None = None,
    ) -> dict[str, Any]:
        """Put one Job manifest on the central queue (``gco queue submit``).

        The record runs in the manifest's own namespace; ``namespace`` names
        it for a manifest that declares none and must match one that does,
        as the queue API requires. The queue is global, so the request rides
        the global API (the target Region's bridge in regional API mode).
        """
        from cli.commands.queue_cmd import _queue_namespace
        from gco.services.spot_price_gate import validate_spot_gate_fields

        gate_error = validate_spot_gate_fields(max_spot_price, spot_instance_type)
        if gate_error:
            raise DashboardError(gate_error)
        document = copy.deepcopy(dict(manifest))
        try:
            envelope_namespace = _queue_namespace(
                document, namespace, self.config.default_namespace
            )
        except ValueError as exc:
            raise DashboardError(str(exc)) from exc
        body: dict[str, Any] = {
            "manifest": document,
            "target_region": region,
            "namespace": envelope_namespace,
            "priority": priority,
            "labels": dict(labels) if labels else None,
        }
        if max_spot_price is not None:
            body["max_spot_price"] = max_spot_price
            body["spot_instance_type"] = spot_instance_type
        return self.client().call_api(
            "POST",
            "/api/v1/queue/jobs",
            region=region if self.config.use_regional_api else None,
            body=body,
        )

    def queue_cancel(self, job_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return self.client().call_api(
            "DELETE",
            f"/api/v1/queue/jobs/{api_path_segment(job_id)}",
            region=self._queue_region(),
            params={"reason": reason} if reason else None,
        )

    # ------------------------------------------------------------------
    # Capacity
    # ------------------------------------------------------------------

    def spot_price_history(
        self, instance_type: str, region: str, *, days: int = 7
    ) -> list[dict[str, Any]]:
        """Every Linux/UNIX Spot price change for the type in the Region's AZs."""
        ec2 = self.session().client("ec2", region_name=region)
        end = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        paginator = ec2.get_paginator("describe_spot_price_history")
        for page in paginator.paginate(
            InstanceTypes=[instance_type],
            ProductDescriptions=["Linux/UNIX"],
            StartTime=end - timedelta(days=days),
            EndTime=end,
        ):
            for item in page.get("SpotPriceHistory", []):
                rows.append(
                    {
                        "region": region,
                        "availability_zone": item["AvailabilityZone"],
                        "timestamp": item["Timestamp"],
                        "price": float(item["SpotPrice"]),
                    }
                )
        return rows

    def instance_report(self, instance_type: str, regions: Sequence[str]) -> list[RegionRead]:
        """What the type is and costs in each Region: offering, hardware, prices."""
        from cli.capacity import get_capacity_checker

        def read(region: str) -> dict[str, Any]:
            checker = get_capacity_checker(self.config)
            offered = checker.check_instance_available_in_region(instance_type, region)
            report: dict[str, Any] = {"offered": offered}
            if not offered:
                return report
            info = checker.get_instance_info(instance_type, region=region)
            if info is not None:
                report.update(
                    vcpus=info.vcpus,
                    memory_gib=info.memory_gib,
                    gpu_count=info.gpu_count,
                    gpu_type=info.gpu_type,
                    gpu_memory_gib=info.gpu_memory_gib,
                    architecture=info.architecture,
                )
            report["on_demand_price"] = checker.get_on_demand_price(instance_type, region)
            spot = checker.get_spot_price_history(instance_type, region)
            if spot:
                report["spot_price"] = min(item.current_price for item in spot)
                report["spot_stability"] = min(item.price_stability for item in spot)
                report["spot_zones"] = len(spot)
            return report

        return _fan_out(regions, read)

    def placement_score(self, instance_type: str, region: str, *, capacity: int = 1) -> int | None:
        """Spot placement score (1-10) for the type's instance pool in the Region."""
        from cli.capacity import get_capacity_checker
        from cli.capacity.checker import SpotPlacementConfigLimitError

        try:
            scores = get_capacity_checker(self.config).get_spot_placement_score(
                instance_type, region, target_capacity=capacity
            )
        except SpotPlacementConfigLimitError as exc:
            raise DashboardError(
                str(exc), hint="AWS limits distinct placement-score queries per day."
            ) from exc
        score = scores.get("regional")
        return int(score) if isinstance(score, int | float) else None

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def endpoints(self) -> list[dict[str, Any]]:
        from cli.inference import get_inference_manager

        return get_inference_manager(self.config).list_endpoints()

    def endpoint(self, name: str) -> dict[str, Any] | None:
        from cli.inference import get_inference_manager

        return get_inference_manager(self.config).get_endpoint(name)

    def _endpoint_or_error(self, name: str) -> dict[str, Any]:
        endpoint = self.endpoint(name)
        if endpoint is None:
            raise DashboardError(f"Endpoint '{name}' does not exist.")
        return endpoint

    def endpoint_health(self, name: str, region: str | None) -> dict[str, Any]:
        """Hit the endpoint's health path through API Gateway (``gco inference health``).

        Without ``region`` the probe rides the global API. Regional API mode
        has none, so there it rides the default Region's bridge, as the CLI's
        does, and the result names that Region. A probe that is not answered
        2xx carries ``error``: the answer's own account, with what to do when
        API Gateway refused the request itself (an expired session's 403, say).
        """
        endpoint = self._endpoint_or_error(name)
        spec = _object(endpoint.get("spec"))
        health_path = str(spec.get("health_check_path") or "/health")
        path = str(endpoint.get("ingress_path") or f"/inference/{name}") + checked_api_sub_path(
            health_path
        )
        target = region or (self.config.default_region if self.config.use_regional_api else None)
        client = self.client()
        # Resolve the endpoint first so the latency below is the request's own.
        if target:
            client.get_regional_api_endpoint(target)
        else:
            client.get_api_endpoint()
        started = time.perf_counter()
        response = client.make_authenticated_request(
            "GET", path, target_region=target, max_attempts=1
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        result: dict[str, Any] = {
            "region": target or "global",
            "healthy": response.ok,
            "http_status": response.status_code,
            "latency_ms": latency_ms,
        }
        if not response.ok:
            detail = _response_detail(response)
            result["error"] = _with_hint(
                DashboardError(detail, hint=_gateway_hint(response.status_code, detail))
            )
        return result

    def served_model(self, name: str, region: str | None = None) -> str:
        """The model id the endpoint serves, from its OpenAI ``/v1/models`` listing."""
        endpoint = self._endpoint_or_error(name)
        base = str(endpoint.get("ingress_path") or f"/inference/{name}")
        try:
            response = self.client().make_authenticated_request(
                "GET", f"{base}/v1/models", target_region=region, max_attempts=1
            )
            if response.ok:
                models = response.json().get("data") or []
                if models and isinstance(models[0], dict) and models[0].get("id"):
                    return str(models[0]["id"])
        except Exception as exc:
            logger.debug("Model discovery for %s failed: %s", name, exc)
        spec = _object(endpoint.get("spec"))
        env = _object(spec.get("env"))
        return str(env.get("MODEL") or name)

    def chat(
        self,
        name: str,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        region: str | None = None,
    ) -> Iterator[str]:
        """Stream an OpenAI-compatible chat completion from the endpoint, token by token."""
        endpoint = self._endpoint_or_error(name)
        path = str(endpoint.get("ingress_path") or f"/inference/{name}") + "/v1/chat/completions"
        body = {
            "model": model,
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        response = self.client().make_authenticated_request(
            "POST", path, body=body, target_region=region, stream=True
        )
        try:
            if not response.ok:
                detail = _response_detail(response)
                raise DashboardError(
                    f"The endpoint answered HTTP {response.status_code}: {detail}",
                    hint=_gateway_hint(response.status_code, detail),
                )
            for raw in response.iter_lines(decode_unicode=False):
                line = raw.decode("utf-8", errors="replace").strip() if raw else ""
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                # Only an object carries choices: other JSON is skipped like
                # an unparseable line, and so are malformed choices.
                for choice in _objects(_object(chunk).get("choices")):
                    text = _object(choice.get("delta")).get("content")
                    if isinstance(text, str) and text:
                        yield text
        finally:
            response.close()

    def deploy_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        """Register an endpoint in every target Region (``gco inference deploy``)."""
        from cli.inference import get_inference_manager

        return get_inference_manager(self.config).deploy(**kwargs)

    def scale_endpoint(self, name: str, replicas: int) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).scale(name, replicas), name)

    def stop_endpoint(self, name: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).stop(name), name)

    def start_endpoint(self, name: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).start(name), name)

    def delete_endpoint(self, name: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).delete(name), name)

    def update_endpoint_image(self, name: str, image: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).update_image(name, image), name)

    def add_endpoint_region(self, name: str, region: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).add_region(name, region), name)

    def remove_endpoint_region(self, name: str, region: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).remove_region(name, region), name)

    def start_canary(
        self, name: str, image: str, *, weight: int, replicas: int = 1
    ) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(
            get_inference_manager(self.config).canary_deploy(
                name, image, weight=weight, replicas=replicas
            ),
            name,
        )

    def promote_canary(self, name: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).promote_canary(name), name)

    def rollback_canary(self, name: str) -> dict[str, Any]:
        from cli.inference import get_inference_manager

        return self._require(get_inference_manager(self.config).rollback_canary(name), name)

    def endpoint_health_all(self, name: str, regions: Sequence[str]) -> list[dict[str, Any]]:
        """Probe the endpoint through the global API and in each of ``regions`` at once.

        Regional API mode has no global API, so there only ``regions`` are probed.
        """
        targets: list[str | None] = [*regions] if self.config.use_regional_api else [None, *regions]
        if not targets:
            return []

        def probe(target: str | None) -> dict[str, Any]:
            try:
                return self.endpoint_health(name, target)
            except Exception as exc:
                return {
                    "region": target or "global",
                    "healthy": False,
                    "http_status": None,
                    "latency_ms": None,
                    "error": _with_hint(describe_error(exc)),
                }

        with ThreadPoolExecutor(max_workers=min(MAX_FANOUT, len(targets))) as pool:
            return list(pool.map(probe, targets))

    @staticmethod
    def _require(result: dict[str, Any] | None, name: str) -> dict[str, Any]:
        if result is None:
            raise DashboardError(
                f"Endpoint '{name}' was not changed: it does not exist or is not in a "
                "state that allows this."
            )
        return result

    # ------------------------------------------------------------------
    # Costs (Cost Explorer bills each request)
    # ------------------------------------------------------------------

    def costs(self, *, days: int = 30) -> dict[str, Any]:
        """Spend by service, by Region and by day, plus the forecast and tag status."""
        from cli.costs import get_cost_tracker

        tracker = get_cost_tracker(self.config)
        summary = tracker.get_cost_summary(days=days)
        report: dict[str, Any] = {
            "total": summary.total,
            "currency": summary.currency,
            "period_start": summary.period_start,
            "period_end": summary.period_end,
            "by_service": [{"service": i.service, "amount": i.amount} for i in summary.by_service],
            "by_region": tracker.get_cost_by_region(days=days),
            "daily": tracker.get_daily_trend(days=days),
            "errors": [],
        }
        try:
            report["forecast"] = tracker.get_forecast(days_ahead=30)
        except Exception as exc:
            report["forecast"] = None
            report["errors"].append(f"forecast: {_with_hint(describe_error(exc))}")
        try:
            report["allocation_tags"] = tracker.get_cost_allocation_tag_status()
        except Exception as exc:
            report["allocation_tags"] = []
            report["errors"].append(f"allocation tags: {_with_hint(describe_error(exc))}")
        return report

    def allocation_costs(self, *, days: int = 7, granularity: str = "daily") -> dict[str, Any]:
        """Kubernetes allocation cost (OpenCost reports, queried through Athena).

        The same three canned queries ``gco costs k8s namespaces``, ``regions``
        and ``trend`` run, in parallel. Athena bills by data scanned and these
        reports are small, so this is the cheap, hourly view of spend.
        """
        from cli.cost_analytics import get_cost_analytics

        queries: dict[str, Callable[[], Any]] = {
            "namespaces": lambda: get_cost_analytics(self.config).cost_by_namespace(days=days),
            "regions": lambda: get_cost_analytics(self.config).cost_by_region(days=days),
            "trend": lambda: get_cost_analytics(self.config).cost_over_time(
                days=days, granularity=granularity
            ),
        }
        report: dict[str, Any] = {}
        failures: dict[str, DashboardError] = {}
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            futures = {name: pool.submit(query) for name, query in queries.items()}
        for name, future in futures.items():
            try:
                report[name] = list(future.result().rows)
            except Exception as exc:
                report[name] = []
                failures[name] = describe_error(exc)
        errors = {name: _with_hint(error) for name, error in failures.items()}
        report["errors"] = [f"{name}: {error}" for name, error in errors.items()]
        if len(errors) == len(queries):
            raise DashboardError(
                "Kubernetes allocation costs are unavailable: " + _by_error(errors),
                # Failures that say what to do (sign in again, say) do not
                # point at a missing feature; any other failure may.
                hint=None
                if all(error.hint for error in failures.values())
                else "They need cost_monitoring.enabled in cdk.json and the monitoring stack.",
            )
        return report

    def cost_monitors(self, regions: Sequence[str]) -> list[RegionRead]:
        """Each Region's cost monitor: OpenCost health and its last scheduled report."""
        return _fan_out(
            regions,
            lambda region: self.client().call_api(
                "GET", "/api/v1/cost/status", region=region, max_attempts=1
            ),
        )

    # ------------------------------------------------------------------
    # Platform objects
    # ------------------------------------------------------------------

    def global_health(self) -> dict[str, Any]:
        return self.client().get_global_health()

    def regional_policy(self, regions: Sequence[str]) -> list[RegionRead]:
        """The job validation policy each Region's manifest processor enforces."""
        return _fan_out(
            regions,
            lambda region: self.client().call_api(
                "GET", "/api/v1/policy", region=region, max_attempts=1
            ),
        )

    def regional_status(self, regions: Sequence[str]) -> list[RegionRead]:
        """Each Region's manifest processor status (central-queue worker included)."""
        return _fan_out(
            regions,
            lambda region: self.client().call_api(
                "GET", "/api/v1/status", region=region, max_attempts=1
            ),
        )

    def templates(self) -> list[dict[str, Any]]:
        result = self.client().call_api("GET", "/api/v1/templates", region=self._queue_region())
        return _objects(result.get("templates"))

    def webhooks(self) -> list[dict[str, Any]]:
        result = self.client().call_api("GET", "/api/v1/webhooks", region=self._queue_region())
        return _objects(result.get("webhooks"))

    def create_webhook(
        self,
        url: str,
        events: Sequence[str],
        *,
        namespace: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        """Register a webhook for job events (``gco webhooks create``).

        The API checks the URL before storing it: HTTPS only, and never a
        private, loopback or link-local address.
        """
        if not events:
            raise DashboardError("Pick at least one event to subscribe to.")
        body: dict[str, Any] = {"url": url.strip(), "events": list(events)}
        if namespace:
            body["namespace"] = namespace
        if secret:
            body["secret"] = secret
        return self.client().call_api(
            "POST", "/api/v1/webhooks", region=self._queue_region(), body=body
        )

    def delete_webhook(self, webhook_id: str) -> dict[str, Any]:
        return self.client().call_api(
            "DELETE",
            f"/api/v1/webhooks/{api_path_segment(webhook_id)}",
            region=self._queue_region(),
        )

    def create_template(
        self,
        name: str,
        manifest: Mapping[str, Any],
        *,
        description: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a job template (``gco templates create``)."""
        body: dict[str, Any] = {"name": name, "manifest": copy.deepcopy(dict(manifest))}
        if description:
            body["description"] = description
        if parameters:
            body["parameters"] = dict(parameters)
        return self.client().call_api(
            "POST", "/api/v1/templates", region=self._queue_region(), body=body
        )

    def delete_template(self, name: str) -> dict[str, Any]:
        return self.client().call_api(
            "DELETE",
            f"/api/v1/templates/{api_path_segment(name)}",
            region=self._queue_region(),
        )

    def run_template(
        self,
        name: str,
        *,
        job_name: str,
        region: str,
        namespace: str = "gco-jobs",
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Job from a template in one Region (``gco templates run``)."""
        body: dict[str, Any] = {"name": job_name, "namespace": namespace}
        if parameters:
            body["parameters"] = dict(parameters)
        return self.client().call_api(
            "POST",
            f"/api/v1/jobs/from-template/{api_path_segment(name)}",
            region=region,
            body=body,
        )

    def api_get(
        self, path: str, *, region: str | None = None, params: Mapping[str, str] | None = None
    ) -> Any:
        """A console read of one known route (GET only)."""
        if path not in CONSOLE_ROUTES:
            raise DashboardError(f"{path} is not a console route.")
        if path in GLOBAL_ONLY_ROUTES and region:
            raise DashboardError(f"{path} answers only from the global API.")
        return self.client().call_api(
            "GET", path, region=region, params=dict(params) if params else None
        )

    # ------------------------------------------------------------------
    # Manifests in the checkout
    # ------------------------------------------------------------------

    def examples(self, root: Path | None = None) -> list[ExampleManifest]:
        """The checkout's example Job manifests, GPU users flagged."""
        from gco.services.manifest_processor import safe_load_all_yaml

        directory = (root or _project_dir()) / "examples"
        found: list[ExampleManifest] = []
        for path in sorted(directory.glob("*.yaml")):
            try:
                text = path.read_text(encoding="utf-8")
                documents = [d for d in safe_load_all_yaml(text) if isinstance(d, dict)]
            except Exception as exc:
                logger.debug("Skipping %s: %s", path, exc)
                continue
            kinds = tuple(str(d.get("kind", "")) for d in documents)
            if "Job" not in kinds:
                continue
            title, summary, prerequisites = example_header(text)
            found.append(
                ExampleManifest(
                    name=path.stem,
                    path=str(path),
                    kinds=kinds,
                    gpu=manifest_requests_gpu(documents),
                    title=title,
                    summary=summary,
                    prerequisites=prerequisites,
                )
            )
        return found

    def inference_example(self, name: str, root: Path | None = None) -> ServingDefaults:
        """What the checkout's ``examples/<name>.yaml`` serves, for the deploy dialog to preset.

        An example that is missing or does not parse gives the defaults, so
        the dialog still opens and the operator names the image.
        """
        from gco.services.manifest_processor import safe_load_all_yaml

        path = (root or _project_dir()) / "examples" / f"{name}.yaml"
        try:
            documents = safe_load_all_yaml(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("No serving defaults from %s: %s", path, exc)
            return ServingDefaults()
        return serving_defaults(documents, source=str(path))
