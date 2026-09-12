"""Cost monitoring (OpenCost) health, data, and report-pipeline checks.

Validates through the same authenticated API surface operators use: each
Region's ``/api/v1/cost/status`` must report a healthy OpenCost that is
returning allocation data, and an ad-hoc ``/api/v1/cost/reports`` request
must produce a Parquet object that is then confirmed present in the central
cost report bucket. The bucket the service reports is compared against the
identity the monitoring stack published to SSM (the bucket carries a
CloudFormation-generated name; nothing reconstructs it), which also proves
the runtime discovery the regional cost-monitor performs resolved the same
bucket the operator surface does. Data readiness is polled with a bounded
deadline because a freshly-deployed Prometheus needs a few scrape cycles
before OpenCost can answer with non-empty allocations.
"""

from __future__ import annotations

import re
import time
from typing import Any

from gco.stacks.constants import COST_REPORT_ADHOC_PREFIX, cost_report_ssm_parameter_prefix

from ..checks.jobs import _response_json
from ..context import _job_transport_region
from ..json_utils import loads_without_duplicate_keys
from ..models import RunContext, utc_now

#: Ceiling for the OpenCost data-readiness poll. A fresh deploy needs
#: Prometheus up, OpenCost scraped, and at least one allocation window
#: resolvable; measured cold-start readiness sits well inside this bound.
_OPENCOST_READY_TIMEOUT_SECONDS = 1_200

#: Trailing window requested for the validation ad-hoc report.
_VALIDATION_REPORT_WINDOW_HOURS = 1

#: The API bridge can time out while the backend finishes a cold OpenCost/
#: Parquet/S3 operation. Live validation runs in an exclusive disposable
#: account, so tolerate at most one ambiguous duplicate for this exact 504.
_REPORT_MAX_ATTEMPTS = 2
_REPORT_RETRY_DELAY_SECONDS = 15
_MAX_REPORT_RESPONSE_EVIDENCE_CHARS = 512
_SUCCESSFUL_REPORT_STATUS_CODES = {200, 201}
_EXACT_BRIDGE_TIMEOUT_BODY = {
    "error": "Gateway timeout",
    "message": "Upstream failed after 1 attempt(s)",
}
_STARTED_ATTEMPT_FIELDS = {"attempt", "state", "started_at"}
_COMPLETED_ATTEMPT_FIELDS = _STARTED_ATTEMPT_FIELDS | {
    "ended_at",
    "status_code",
    "exact_bridge_timeout",
    "response_text",
    "retry_scheduled",
}
_JOURNAL_FIELDS = {"attempts", "duplicate_possible", "completed_report"}


def _bounded_response_text(value: Any) -> str:
    text = str(value or "")
    return text[:_MAX_REPORT_RESPONSE_EVIDENCE_CHARS]


def _is_exact_bridge_timeout_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and payload == _EXACT_BRIDGE_TIMEOUT_BODY


def _is_exact_bridge_timeout(response: Any) -> bool:
    if response.status_code != 504:
        return False
    try:
        payload = loads_without_duplicate_keys(response.text)
    except TypeError, ValueError:
        return False
    return _is_exact_bridge_timeout_payload(payload)


def _is_exact_bridge_timeout_evidence(status_code: int, response_text: str) -> bool:
    if status_code != 504:
        return False
    try:
        payload = loads_without_duplicate_keys(response_text)
    except TypeError, ValueError:
        return False
    return _is_exact_bridge_timeout_payload(payload)


def _expected_report_bucket(ctx: RunContext) -> str:
    """The cost bucket the monitoring stack published, read from SSM.

    The bucket's physical name is CloudFormation-generated, so the published
    ``<prefix>/name`` parameter in the monitoring region is the only
    authority for it — the same parameter the regional cost-monitor services
    resolve at runtime. Any failure to read it fails validation: a report
    whose bucket cannot be matched against the published identity is not
    evidence.
    """
    region = _monitoring_region(ctx)
    parameter_name = f"{cost_report_ssm_parameter_prefix(ctx.config.project_name)}/name"
    ssm = ctx.session.client("ssm", region_name=region)
    try:
        response = ssm.get_parameter(Name=parameter_name)
    except Exception as exc:  # noqa: BLE001 - absence and access failures both fail validation
        raise RuntimeError(
            f"Cost report bucket parameter {parameter_name} in {region} is not readable: {exc}"
        ) from exc
    parameter = response.get("Parameter") if isinstance(response, dict) else None
    value = str((parameter or {}).get("Value") or "").strip()
    if not value:
        raise RuntimeError(f"Cost report bucket parameter {parameter_name} in {region} is empty")
    return value


def _validated_completed_report(
    ctx: RunContext,
    report: dict[str, Any],
    region: str,
) -> dict[str, Any]:
    observed_region = report.get("region")
    if observed_region != region:
        raise RuntimeError(
            f"Ad-hoc cost report returned Region {observed_region!r}; expected {region!r}"
        )
    s3_key = report.get("s3_key")
    if not isinstance(s3_key, str) or not s3_key.strip():
        raise RuntimeError(f"Ad-hoc cost report for {region} omitted its S3 key")
    expected_prefix = f"{COST_REPORT_ADHOC_PREFIX}/region={region}/"
    key_pattern = (
        rf"{re.escape(expected_prefix)}date=\d{{4}}-\d{{2}}-\d{{2}}/"
        r"allocation-\d{8}T\d{6}Z-\d{8}T\d{6}Z-[0-9a-f]{8}\.parquet"
    )
    if re.fullmatch(key_pattern, s3_key) is None:
        raise RuntimeError(f"Ad-hoc cost report for {region} used unexpected S3 key {s3_key!r}")
    row_count = report.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        raise RuntimeError(f"Ad-hoc cost report for {region} contained zero allocation rows")
    bucket = report.get("bucket")
    if not isinstance(bucket, str) or not bucket.strip():
        raise RuntimeError(f"Ad-hoc cost report for {region} omitted its bucket")
    expected_bucket = _expected_report_bucket(ctx)
    if bucket != expected_bucket:
        raise RuntimeError(
            f"Ad-hoc cost report for {region} used unexpected bucket {bucket!r}; "
            f"expected {expected_bucket!r}"
        )
    return dict(report)


def _validate_report_attempt(
    attempt: dict[str, Any],
    expected_number: int,
    total_attempts: int,
) -> None:
    attempt_number = attempt.get("attempt")
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number != expected_number
    ):
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid ordering")
    state = attempt.get("state")
    started_at = attempt.get("started_at")
    if state not in {"started", "completed"} or not isinstance(started_at, str) or not started_at:
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid fields")
    if state == "started":
        if set(attempt) != _STARTED_ATTEMPT_FIELDS:
            raise RuntimeError("OpenCost started-attempt checkpoint has invalid fields")
        if expected_number != total_attempts:
            raise RuntimeError("OpenCost report-attempt checkpoint has an interior start")
        return

    if set(attempt) != _COMPLETED_ATTEMPT_FIELDS:
        raise RuntimeError("OpenCost completed-attempt checkpoint has invalid fields")
    ended_at = attempt.get("ended_at")
    status_code = attempt.get("status_code")
    exact_bridge_timeout = attempt.get("exact_bridge_timeout")
    response_text = attempt.get("response_text")
    retry_scheduled = attempt.get("retry_scheduled")
    if (
        not isinstance(ended_at, str)
        or not ended_at
        or isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
        or not isinstance(exact_bridge_timeout, bool)
        or not isinstance(response_text, str)
        or len(response_text) > _MAX_REPORT_RESPONSE_EVIDENCE_CHARS
        or not isinstance(retry_scheduled, bool)
    ):
        raise RuntimeError("OpenCost completed-attempt checkpoint has invalid fields")
    evidenced_timeout = _is_exact_bridge_timeout_evidence(status_code, response_text)
    if exact_bridge_timeout is not evidenced_timeout:
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid timeout evidence")
    expected_retry = expected_number == 1 and evidenced_timeout
    if retry_scheduled is not expected_retry:
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid retry transition")
    if status_code in _SUCCESSFUL_REPORT_STATUS_CODES and response_text:
        raise RuntimeError(
            "OpenCost successful-attempt checkpoint has unexpected response evidence"
        )


def _validated_report_journal(
    ctx: RunContext,
    raw: dict[str, Any],
    region: str,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
    if set(raw) != _JOURNAL_FIELDS:
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid fields")
    raw_attempts = raw.get("attempts")
    duplicate_possible = raw.get("duplicate_possible")
    completed_report = raw.get("completed_report")
    if (
        not isinstance(raw_attempts, list)
        or not raw_attempts
        or len(raw_attempts) > _REPORT_MAX_ATTEMPTS
        or not all(isinstance(item, dict) for item in raw_attempts)
        or not isinstance(duplicate_possible, bool)
        or not isinstance(completed_report, (dict, type(None)))
    ):
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid fields")

    attempts = [dict(item) for item in raw_attempts]
    for expected_number, attempt in enumerate(attempts, start=1):
        _validate_report_attempt(attempt, expected_number, len(attempts))
    if len(attempts) == 2 and (
        attempts[0].get("state") != "completed"
        or attempts[0].get("exact_bridge_timeout") is not True
        or attempts[0].get("retry_scheduled") is not True
    ):
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid retry ancestry")

    evidenced_duplicate = any(attempt.get("exact_bridge_timeout") is True for attempt in attempts)
    if duplicate_possible is not evidenced_duplicate:
        raise RuntimeError("OpenCost report-attempt checkpoint has invalid duplicate evidence")

    validated_report: dict[str, Any] | None = None
    if completed_report is not None:
        if (
            not attempts
            or attempts[-1].get("state") != "completed"
            or attempts[-1].get("status_code") not in _SUCCESSFUL_REPORT_STATUS_CODES
        ):
            raise RuntimeError("OpenCost completed-report checkpoint has no successful attempt")
        validated_report = _validated_completed_report(ctx, completed_report, region)
    return attempts, duplicate_possible, validated_report


def _validated_report_journal_root(
    ctx: RunContext,
    root: Any,
    *,
    allow_empty: bool = False,
) -> dict[str, tuple[list[dict[str, Any]], bool, dict[str, Any] | None]]:
    if not isinstance(root, dict) or (not root and not allow_empty):
        raise RuntimeError("OpenCost report-attempt checkpoint root is malformed")
    expected_regions = set(ctx.deployment_regions)
    validated: dict[
        str,
        tuple[list[dict[str, Any]], bool, dict[str, Any] | None],
    ] = {}
    for sibling_region, raw in root.items():
        if not isinstance(sibling_region, str) or sibling_region not in expected_regions:
            raise RuntimeError(
                f"OpenCost report-attempt checkpoint has unexpected Region {sibling_region!r}"
            )
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"OpenCost report-attempt checkpoint for {sibling_region} is malformed"
            )
        validated[sibling_region] = _validated_report_journal(
            ctx,
            raw,
            sibling_region,
        )
    return validated


def _load_report_journal(
    ctx: RunContext,
    region: str,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
    """Load and validate every durable per-Region non-idempotent journal."""
    if region not in ctx.deployment_regions:
        raise RuntimeError(f"OpenCost report requested unexpected Region {region!r}")
    with ctx.state_lock:
        state = ctx.checkpoint.state
        if "opencost_report_attempts" not in state:
            return [], False, None
        validated = _validated_report_journal_root(
            ctx,
            state["opencost_report_attempts"],
        )
        return validated.get(region, ([], False, None))


def _persist_report_attempts(
    ctx: RunContext,
    region: str,
    attempts: list[dict[str, Any]],
    *,
    duplicate_possible: bool,
    completed_report: dict[str, Any] | None = None,
) -> None:
    if region not in ctx.deployment_regions:
        raise RuntimeError(f"OpenCost report requested unexpected Region {region!r}")
    record = {
        "attempts": [dict(item) for item in attempts],
        "duplicate_possible": duplicate_possible,
        "completed_report": (dict(completed_report) if completed_report is not None else None),
    }
    _validated_report_journal(ctx, record, region)
    with ctx.state_lock:
        state = ctx.checkpoint.state.setdefault("opencost_report_attempts", {})
        _validated_report_journal_root(ctx, state, allow_empty=True)
        state[region] = record
        ctx.persist_callback(ctx.checkpoint)


def _cost_monitoring_configured(ctx: RunContext) -> bool:
    """Return whether the checked-in cdk.json enables the cost pipeline."""
    cost_block = ctx.cdk_context.get("cost_monitoring")
    cost_enabled = True
    if isinstance(cost_block, dict) and "enabled" in cost_block:
        cost_enabled = bool(cost_block["enabled"])
    observability_block = ctx.cdk_context.get("cluster_observability")
    observability_enabled = True
    if isinstance(observability_block, dict) and "enabled" in observability_block:
        observability_enabled = bool(observability_block["enabled"])
    return cost_enabled and observability_enabled


def _monitoring_region(ctx: RunContext) -> str:
    regions = ctx.cdk_context.get("deployment_regions") or {}
    monitoring = regions.get("monitoring") if isinstance(regions, dict) else None
    return str(monitoring or ctx.config.global_region)


def _get_cost_status(ctx: RunContext, region: str) -> dict[str, Any]:
    """Fetch one Region's /api/v1/cost/status through its authorized transport."""
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path="/api/v1/cost/status",
        target_region=_job_transport_region(ctx, region),
    )
    if not response.ok:
        raise RuntimeError(
            f"Cost status for {region} failed: {response.status_code} {response.text}"
        )
    status = _response_json(response, f"Cost status for {region}")
    observed_region = str(status.get("region") or "")
    if observed_region and observed_region != region:
        raise RuntimeError(
            f"Cost status transport returned Region {observed_region!r}; expected {region!r}"
        )
    return status


def _wait_for_opencost_data(ctx: RunContext, region: str) -> dict[str, Any]:
    """Poll until OpenCost is healthy and returning allocation data.

    Fails the action when the bounded deadline passes with OpenCost either
    unhealthy or answering with empty allocations — both mean the deployed
    cost pipeline cannot produce trustworthy reports.
    """
    deadline = time.monotonic() + _OPENCOST_READY_TIMEOUT_SECONDS
    last_status: dict[str, Any] = {}
    while True:
        last_status = _get_cost_status(ctx, region)
        if bool(last_status.get("opencost_healthy")) and bool(
            last_status.get("opencost_returning_data")
        ):
            return last_status
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"OpenCost in {region} did not become healthy with allocation data "
                f"within {_OPENCOST_READY_TIMEOUT_SECONDS}s: "
                f"healthy={last_status.get('opencost_healthy')} "
                f"returning_data={last_status.get('opencost_returning_data')} "
                f"last_error={last_status.get('last_error')}"
            )
        time.sleep(ctx.settings.poll_interval_seconds)


def _generate_validation_report(ctx: RunContext, region: str) -> dict[str, Any]:
    """Request one report under a crash-safe two-attempt durable journal."""
    attempts, duplicate_possible, completed_report = _load_report_journal(ctx, region)
    if completed_report is not None:
        return {
            **completed_report,
            "request_attempts": attempts,
            "duplicate_possible": duplicate_possible,
        }

    if attempts:
        previous = attempts[-1]
        if previous.get("state") == "started":
            raise RuntimeError(
                f"OpenCost report attempt {previous.get('attempt')} for {region} has an "
                "ambiguous in-flight outcome; automatic replay is forbidden"
            )
        if previous.get("status_code") in _SUCCESSFUL_REPORT_STATUS_CODES:
            raise RuntimeError(
                f"OpenCost report attempt {previous.get('attempt')} for {region} has a "
                "successful HTTP outcome but no validated report; automatic replay is forbidden"
            )
        if len(attempts) >= _REPORT_MAX_ATTEMPTS:
            raise RuntimeError(
                f"OpenCost report retry budget for {region} is exhausted: "
                f"{previous.get('status_code')} {previous.get('response_text', '')}"
            )
        if (
            previous.get("exact_bridge_timeout") is not True
            or previous.get("retry_scheduled") is not True
        ):
            raise RuntimeError(
                f"Prior OpenCost report attempt for {region} is not safely retryable: "
                f"{previous.get('status_code')} {previous.get('response_text', '')}"
            )
        time.sleep(_REPORT_RETRY_DELAY_SECONDS)

    for attempt_number in range(len(attempts) + 1, _REPORT_MAX_ATTEMPTS + 1):
        attempt: dict[str, Any] = {
            "attempt": attempt_number,
            "state": "started",
            "started_at": utc_now(),
        }
        attempts.append(attempt)
        # Persist the non-idempotent boundary before the network call. A crash
        # with this state is ambiguous and deliberately blocks automatic replay.
        _persist_report_attempts(
            ctx,
            region,
            attempts,
            duplicate_possible=duplicate_possible,
        )
        response = ctx.aws_client.make_authenticated_request(
            method="POST",
            path="/api/v1/cost/reports",
            body={"window_hours": _VALIDATION_REPORT_WINDOW_HOURS, "include_rows": False},
            target_region=_job_transport_region(ctx, region),
        )
        exact_bridge_timeout = _is_exact_bridge_timeout(response)
        retry_scheduled = attempt_number == 1 and exact_bridge_timeout
        response_text = (
            ""
            if response.status_code in _SUCCESSFUL_REPORT_STATUS_CODES
            else _bounded_response_text(response.text)
        )
        attempt.update(
            {
                "state": "completed",
                "ended_at": utc_now(),
                "status_code": response.status_code,
                "exact_bridge_timeout": exact_bridge_timeout,
                "response_text": response_text,
                "retry_scheduled": retry_scheduled,
            }
        )
        duplicate_possible = duplicate_possible or exact_bridge_timeout
        # Persist every HTTP outcome before parsing or validating its body. A
        # successful response without completed_report is terminal on resume:
        # it proves the POST returned but cannot safely authorize a replay.
        _persist_report_attempts(
            ctx,
            region,
            attempts,
            duplicate_possible=duplicate_possible,
        )

        if retry_scheduled:
            # The upstream may finish after the bridge's 28-second deadline.
            # A second request can therefore create one additional ad-hoc
            # object; preserve that ambiguity explicitly instead of pretending
            # this validation-only retry is idempotent.
            time.sleep(_REPORT_RETRY_DELAY_SECONDS)
            continue

        if response.status_code not in _SUCCESSFUL_REPORT_STATUS_CODES:
            raise RuntimeError(
                f"Ad-hoc cost report for {region} failed: {response.status_code} {response.text}"
            )
        payload = _response_json(response, f"Ad-hoc cost report for {region}")
        report = payload.get("report")
        if not isinstance(report, dict):
            raise RuntimeError(f"Ad-hoc cost report for {region} omitted its S3 key")
        completed_report = _validated_completed_report(
            ctx,
            {
                **report,
                "region": payload.get("region"),
                "bucket": payload.get("bucket"),
            },
            region,
        )
        _persist_report_attempts(
            ctx,
            region,
            attempts,
            duplicate_possible=duplicate_possible,
            completed_report=completed_report,
        )
        return {
            **completed_report,
            "request_attempts": attempts,
            "duplicate_possible": duplicate_possible,
        }

    raise AssertionError("OpenCost report attempt loop exhausted without returning")


def _verify_report_object(ctx: RunContext, report: dict[str, Any]) -> dict[str, Any]:
    """Confirm the provenance-validated Parquet object actually exists in S3."""
    region = report.get("region")
    if not isinstance(region, str) or region not in ctx.deployment_regions:
        raise RuntimeError(f"Ad-hoc cost report has invalid Region {region!r}")
    validated_report = _validated_completed_report(ctx, report, region)
    s3 = ctx.session.client("s3", region_name=_monitoring_region(ctx))
    key = str(validated_report["s3_key"])
    bucket = str(validated_report["bucket"])
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - absence and access failures both fail validation
        raise RuntimeError(
            f"Cost report object s3://{bucket}/{key} is not readable: {exc}"
        ) from exc
    size = int(head.get("ContentLength") or 0)
    if size <= 0:
        raise RuntimeError(f"Cost report object s3://{bucket}/{key} is empty")
    return {"bucket": bucket, "key": key, "size_bytes": size}
