"""Checks behind the ``tracing`` action: GCO's spans reach CloudWatch Transaction Search.

The four API services (health-monitor, manifest-processor, inference-proxy,
cost-monitor) export OpenTelemetry spans to their Region's X-Ray OTLP
endpoint. With Transaction Search on, X-Ray stores every span as a structured
log event in the Region's ``aws/spans`` log group, and that is where this
check looks. Per regional Region it proves:

1. the X-Ray trace segment destination is ``CloudWatchLogs`` and ``ACTIVE``
   (the regional stack's enabler switched it; spans are searchable only then);
2. authenticated API traffic, sent through the same per-Region transport the
   other API actions use, reaches the health monitor (``/api/v1/metrics``:
   ``/api/v1/health`` is excluded from tracing), the manifest processor
   (``/api/v1/policy``) and, when cost monitoring is on,
   ``/api/v1/cost/status``, which the manifest processor answers by calling
   the cost monitor over verified HTTPS, so the W3C context its client span
   injects puts the cost monitor's server span into the same trace; and
3. CloudWatch Logs Insights, polled against a deadline because X-Ray delivers
   spans minutes late, finds spans for every expected ``service.name``
   resource attribute (``service.namespace`` ``gco``) and at least one trace
   holding both a manifest-processor and a cost-monitor span.

The inference proxy is expected only in the Region where this run's
``inference`` action sent real requests through it, counted from that
action's start; the run has no running endpoint of its own by the time this
action runs, so anywhere else it is skipped with the reason. Evidence is
counts only: no trace, span, or request identifiers.

Every CDK invocation of a release run carries the ``tracing_overrides``
context ``{"sample_ratio": 1.0}`` (:func:`tracing_overrides_json`), so every
request is sampled; with a lower effective ratio the traffic rounds grow so
that each request kind is still sampled with high probability.
"""

from __future__ import annotations

import contextlib
import json
import math
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from ..context import _job_transport_region
from ..models import RunContext
from ..ownership.transaction_search import _read_trace_segment_destination
from .opencost import _cost_monitoring_configured

#: ``service.name`` of every traced GCO service, in manifest order.
TRACED_SERVICES = ("health-monitor", "manifest-processor", "inference-proxy", "cost-monitor")
#: The ``tracing_overrides`` context every release-run CDK invocation carries.
RELEASE_TRACING_OVERRIDES: dict[str, Any] = {"sample_ratio": 1.0}
SPAN_LOG_GROUP = "aws/spans"

#: cdk.json ``tracing`` defaults when a key (or the whole block) is absent.
_DEFAULT_TRACING: dict[str, Any] = {
    "enabled": True,
    "sample_ratio": 0.05,
    "enable_transaction_search": True,
}
_HEALTH_MONITOR_PATH = "/api/v1/metrics"
_MANIFEST_PROCESSOR_PATH = "/api/v1/policy"
_COST_STATUS_PATH = "/api/v1/cost/status"
_MIN_TRAFFIC_ROUNDS = 3
_MAX_TRAFFIC_ROUNDS = 200
#: With a ratio below 1, rounds are sized so each request kind is sampled at
#: least once with this probability.
_SAMPLING_CONFIDENCE = 0.999
#: The enabler switches the destination at deploy; ``PENDING`` can last minutes.
_DESTINATION_WAIT_SECONDS = 900
#: X-Ray delivers spans to ``aws/spans`` minutes after export.
_SPAN_WAIT_SECONDS = 900
_SPAN_POLL_SECONDS = 30
_QUERY_WAIT_SECONDS = 120
_QUERY_POLL_SECONDS = 2
#: Slack on both ends of a query window for clock skew and ingestion time.
_WINDOW_MARGIN_SECONDS = 120
_QUERY_RESULT_LIMIT = 10_000
_TERMINAL_QUERY_STATUSES = frozenset({"Failed", "Cancelled", "Timeout", "Unknown"})
#: ``StartQuery`` errors a later poll may not see again: the span log group is
#: created on first delivery, and Logs Insights caps concurrent queries.
_TRANSIENT_QUERY_ERRORS = frozenset(
    {
        "LimitExceededException",
        "ResourceNotFoundException",
        "ServiceUnavailableException",
        "ThrottlingException",
    }
)
_SERVICE_FIELD = "`resource.attributes.service.name`"
_NAMESPACE_FIELD = "`resource.attributes.service.namespace`"
_CORRELATED_SERVICES = frozenset({"manifest-processor", "cost-monitor"})


class TracingValidationError(RuntimeError):
    """The deployed services' spans did not reach Transaction Search as promised."""


class _TransientQueryError(RuntimeError):
    """One Logs Insights round failed in a way a later round may not repeat."""


def tracing_overrides_json() -> str:
    """Canonical JSON for the ``tracing_overrides`` context and the resume identity."""
    return json.dumps(RELEASE_TRACING_OVERRIDES, sort_keys=True, separators=(",", ":"))


def effective_tracing_config(ctx: RunContext) -> dict[str, Any]:
    """cdk.json ``tracing`` merged with the run's ``tracing_overrides``, validated."""
    block = ctx.cdk_context.get("tracing")
    if block is not None and not isinstance(block, dict):
        raise TracingValidationError("cdk.json context.tracing must be an object")
    raw_overrides = str(getattr(ctx.settings, "tracing_overrides_json", "") or "")
    try:
        overrides = json.loads(raw_overrides) if raw_overrides else {}
    except json.JSONDecodeError as exc:
        raise TracingValidationError(f"tracing_overrides is not valid JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise TracingValidationError("tracing_overrides must be a JSON object")
    merged = {**_DEFAULT_TRACING, **(block or {}), **overrides}
    enabled = merged["enabled"]
    transaction_search = merged["enable_transaction_search"]
    ratio = merged["sample_ratio"]
    if not isinstance(enabled, bool) or not isinstance(transaction_search, bool):
        raise TracingValidationError(
            "tracing.enabled and tracing.enable_transaction_search must be booleans"
        )
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
        raise TracingValidationError("tracing.sample_ratio must be a number in [0, 1]")
    return {
        "enabled": enabled,
        "sample_ratio": float(ratio),
        "enable_transaction_search": transaction_search,
    }


def _traffic_rounds(sample_ratio: float) -> int:
    """Rounds of requests so each kind is sampled at least once with high probability."""
    if sample_ratio >= 1:
        return _MIN_TRAFFIC_ROUNDS
    needed = math.ceil(math.log(1 - _SAMPLING_CONFIDENCE) / math.log(1 - sample_ratio))
    return max(_MIN_TRAFFIC_ROUNDS, min(_MAX_TRAFFIC_ROUNDS, needed))


def _epoch_ms(timestamp: str) -> int:
    try:
        return int(datetime.fromisoformat(timestamp).timestamp() * 1000)
    except (TypeError, ValueError) as exc:
        raise TracingValidationError(f"Invalid action timestamp {timestamp!r}") from exc


def _inference_proxy_expectation(ctx: RunContext, region: str) -> int | str:
    """When the run's inference traffic crossed this Region's proxy (epoch ms), or why not."""
    settings = ctx.settings
    if not getattr(settings, "inference_enabled", False):
        return (
            "the inference action is not in this run's scope, so no inference request "
            "reached the proxy"
        )
    selected = str(getattr(settings, "selected_region", "") or "")
    if region != selected:
        return f"this run's inference requests went through the proxy in {selected}"
    result = ctx.checkpoint.action_results.get("inference")
    if result is None or result.status != "passed":
        return (
            "the inference action has not passed in this run, so no served inference "
            "request is known to have reached the proxy"
        )
    return _epoch_ms(result.started_at)


def _expected_services(
    ctx: RunContext, region: str
) -> tuple[list[str], dict[str, str], int | None]:
    """Return ``(expected services, skipped with reason, inference traffic start ms)``."""
    skipped: dict[str, str] = {}
    if not _cost_monitoring_configured(ctx):
        skipped["cost-monitor"] = (
            "cost monitoring is disabled in cdk.json (cost_monitoring or cluster_observability)"
        )
    inference = _inference_proxy_expectation(ctx, region)
    if isinstance(inference, str):
        skipped["inference-proxy"] = inference
    expected = [service for service in TRACED_SERVICES if service not in skipped]
    return expected, skipped, None if isinstance(inference, str) else inference


def _wait_for_transaction_search(
    ctx: RunContext,
    region: str,
    config: dict[str, Any],
) -> tuple[dict[str, str], str | None]:
    """Return ``(destination, skip reason)`` once spans are searchable in the Region."""
    xray = ctx.session.client("xray", region_name=region)
    deadline = time.monotonic() + _DESTINATION_WAIT_SECONDS
    while True:
        state = _read_trace_segment_destination(xray)
        if state["destination"] == "XRay":
            if not config["enable_transaction_search"]:
                return state, (
                    "tracing.enable_transaction_search is false and the Region's trace "
                    f"segment destination is XRay, so spans are not stored in {SPAN_LOG_GROUP}"
                )
            raise TracingValidationError(
                f"{region}: the trace segment destination is XRay although "
                "tracing.enable_transaction_search is on; the Transaction Search enabler "
                "did not switch it"
            )
        if state["status"] == "ACTIVE":
            return state, None
        if time.monotonic() >= deadline:
            raise TracingValidationError(
                f"{region}: Transaction Search stayed {state['status']} for "
                f"{_DESTINATION_WAIT_SECONDS}s"
            )
        time.sleep(_SPAN_POLL_SECONDS)


def _drive_traffic(
    ctx: RunContext,
    region: str,
    *,
    rounds: int,
    cost: bool,
) -> dict[str, dict[str, Any]]:
    """Send authenticated GETs to each traced API service; return per-path counts."""
    transport = _job_transport_region(ctx, region)
    paths = [_HEALTH_MONITOR_PATH, _MANIFEST_PROCESSOR_PATH]
    if cost:
        paths.append(_COST_STATUS_PATH)
    traffic: dict[str, dict[str, Any]] = {
        path: {"requests": 0, "status_codes": {}} for path in paths
    }
    for _round in range(rounds):
        for path in paths:
            response = ctx.aws_client.make_authenticated_request(
                method="GET",
                path=path,
                target_region=transport,
            )
            entry = traffic[path]
            code = str(response.status_code)
            entry["requests"] += 1
            entry["status_codes"][code] = entry["status_codes"].get(code, 0) + 1
            if not response.ok:
                raise TracingValidationError(
                    f"{region}: GET {path} answered HTTP {response.status_code} while "
                    "driving traced traffic"
                )
    return traffic


def _service_filter(services: Iterable[str]) -> str:
    names = ", ".join(json.dumps(name) for name in services)
    return f'{_NAMESPACE_FIELD} = "gco" and {_SERVICE_FIELD} in [{names}]'


def _span_count_query(services: Iterable[str]) -> str:
    return (
        f"fields {_SERVICE_FIELD} as service\n"
        f"| filter {_service_filter(services)}\n"
        "| stats count(*) as spans by service"
    )


def _correlation_query() -> str:
    return (
        f"fields {_SERVICE_FIELD} as service, traceId\n"
        f"| filter {_service_filter(sorted(_CORRELATED_SERVICES))}\n"
        "| stats count(*) as spans by traceId, service\n"
        f"| limit {_QUERY_RESULT_LIMIT}"
    )


def _run_query(logs: Any, query: str, *, start_s: int, end_s: int) -> list[dict[str, str]]:
    """Run one Logs Insights query on ``aws/spans`` to completion; rows as dicts."""
    try:
        started = logs.start_query(
            logGroupName=SPAN_LOG_GROUP,
            startTime=start_s,
            endTime=end_s,
            queryString=query,
            limit=_QUERY_RESULT_LIMIT,
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if code in _TRANSIENT_QUERY_ERRORS:
            raise _TransientQueryError(f"StartQuery failed with {code}") from exc
        raise
    query_id = str(started.get("queryId") or "")
    if not query_id:
        raise TracingValidationError("CloudWatch Logs Insights returned no query ID")
    deadline = time.monotonic() + _QUERY_WAIT_SECONDS
    while True:
        response = logs.get_query_results(queryId=query_id)
        status = str(response.get("status") or "")
        if status == "Complete":
            return [
                {
                    str(field.get("field")): str(field.get("value") or "")
                    for field in row
                    if isinstance(field, dict)
                }
                for row in response.get("results") or []
                if isinstance(row, list)
            ]
        if status in _TERMINAL_QUERY_STATUSES:
            raise _TransientQueryError(f"Logs Insights query ended {status}")
        if time.monotonic() >= deadline:
            with contextlib.suppress(ClientError):
                logs.stop_query(queryId=query_id)
            raise _TransientQueryError(
                f"Logs Insights query did not complete within {_QUERY_WAIT_SECONDS}s"
            )
        time.sleep(_QUERY_POLL_SECONDS)


def _count(value: str) -> int:
    try:
        return int(float(value))
    except OverflowError, ValueError:
        return 0


def _span_snapshot(
    logs: Any,
    expected: list[str],
    *,
    window_start_ms: int,
    traffic_start_ms: int,
    correlate: bool,
) -> tuple[dict[str, int], int | None]:
    """Return per-service span counts and, when asked, the correlated trace count."""
    end_s = int(time.time()) + _WINDOW_MARGIN_SECONDS
    counts = dict.fromkeys(expected, 0)
    for row in _run_query(
        logs,
        _span_count_query(expected),
        start_s=window_start_ms // 1000 - _WINDOW_MARGIN_SECONDS,
        end_s=end_s,
    ):
        service = row.get("service", "")
        if service in counts:
            counts[service] += _count(row.get("spans", ""))
    if not correlate:
        return counts, None
    services_by_trace: dict[str, set[str]] = {}
    for row in _run_query(
        logs,
        _correlation_query(),
        start_s=traffic_start_ms // 1000 - _WINDOW_MARGIN_SECONDS,
        end_s=end_s,
    ):
        trace_id = row.get("traceId", "")
        service = row.get("service", "")
        if trace_id and service:
            services_by_trace.setdefault(trace_id, set()).add(service)
    correlated = sum(
        1 for services in services_by_trace.values() if services >= _CORRELATED_SERVICES
    )
    return counts, correlated


def _wait_for_spans(
    ctx: RunContext,
    region: str,
    expected: list[str],
    *,
    window_start_ms: int,
    traffic_start_ms: int,
    correlate: bool,
) -> dict[str, Any]:
    """Poll ``aws/spans`` until every expected service (and the cost trace) shows up."""
    logs = ctx.session.client("logs", region_name=region)
    deadline = time.monotonic() + _SPAN_WAIT_SECONDS
    counts = dict.fromkeys(expected, 0)
    correlated: int | None = 0 if correlate else None
    polls = 0
    while True:
        polls += 1
        last_error: str | None = None
        try:
            counts, correlated = _span_snapshot(
                logs,
                expected,
                window_start_ms=window_start_ms,
                traffic_start_ms=traffic_start_ms,
                correlate=correlate,
            )
        except _TransientQueryError as exc:
            last_error = str(exc)
        missing = [service for service in expected if counts.get(service, 0) < 1]
        uncorrelated = correlate and not correlated
        if last_error is None and not missing and not uncorrelated:
            return {"span_counts": counts, "correlated_cost_traces": correlated, "polls": polls}
        if time.monotonic() >= deadline:
            problems = []
            if missing:
                problems.append("no spans for " + ", ".join(missing))
            if uncorrelated:
                problems.append("no trace holds both a manifest-processor and a cost-monitor span")
            if last_error:
                problems.append(f"last query: {last_error}")
            raise TracingValidationError(
                f"{region}: {'; '.join(problems)} in {SPAN_LOG_GROUP} within "
                f"{_SPAN_WAIT_SECONDS}s ({polls} polls)"
            )
        time.sleep(_SPAN_POLL_SECONDS)


def verify_region_tracing(
    ctx: RunContext,
    region: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Drive traced traffic in one Region and require its spans in Transaction Search."""
    with ctx.state_lock:
        record: dict[str, Any] = ctx.checkpoint.state.setdefault("tracing", {}).setdefault(
            region, {}
        )
    try:
        evidence = _verify_region(ctx, region, config, record)
    except TracingValidationError as exc:
        record["failure"] = str(exc)
        ctx.persist()
        raise
    record["evidence"] = evidence
    ctx.persist()
    return evidence


def _verify_region(
    ctx: RunContext,
    region: str,
    config: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    if config["sample_ratio"] <= 0:
        return {"status": "skipped", "reason": "tracing.sample_ratio is 0, so no span is sampled"}
    destination, skip = _wait_for_transaction_search(ctx, region, config)
    if skip is not None:
        return {"status": "skipped", "reason": skip, "transaction_search": destination}
    expected, skipped, inference_start_ms = _expected_services(ctx, region)
    rounds = _traffic_rounds(config["sample_ratio"])
    cost = "cost-monitor" in expected
    traffic_start_ms = int(time.time() * 1000)
    traffic = _drive_traffic(ctx, region, rounds=rounds, cost=cost)
    record["traffic"] = traffic
    ctx.persist()
    window_start_ms = (
        traffic_start_ms
        if inference_start_ms is None
        else min(traffic_start_ms, inference_start_ms)
    )
    spans = _wait_for_spans(
        ctx,
        region,
        expected,
        window_start_ms=window_start_ms,
        traffic_start_ms=traffic_start_ms,
        correlate=cost,
    )
    return {
        "status": "passed",
        "transaction_search": destination,
        "sample_ratio": config["sample_ratio"],
        "expected_services": expected,
        "skipped_services": skipped,
        "traffic_rounds": rounds,
        "traffic": traffic,
        **spans,
    }
