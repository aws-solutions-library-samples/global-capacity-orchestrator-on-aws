"""Capacity-driven Global Accelerator traffic-dial controller.

Invoked on a schedule by an EventBridge rule (see
``GCOGlobalStack._create_traffic_dial_controller`` in
``gco/stacks/global_stack.py``). Converges each endpoint group's
``TrafficDialPercentage`` toward the observed health of its region, using the
per-cluster ``ClusterHealthy`` metric the health-monitor service already
publishes to the ``GCO/HealthMonitor`` namespace in every workload region.

This handler is self-contained (boto3 + stdlib only) and does not import the
CLI/gco packages, matching the convention used by the other GCO Lambdas.

Control flow is phased:

    Phase 0 — accelerator readiness. The accelerator must be ``DEPLOYED``
        before any decision is made. Endpoint-group updates submitted while a
        previous change is still converging pile up and extend the window in
        which the served configuration is unknown (the ga-registration Lambda
        learned the equivalent lesson at deploy time), so a mid-deployment
        cycle is skipped entirely.
    Phase 1 — current state. ``ListEndpointGroups`` on the listener yields
        each region's endpoint-group ARN and currently served dial.
    Phase 2 — manual overrides. ``gco capacity traffic-dial set`` records an
        override parameter per region; the controller never touches an
        overridden region until ``gco capacity traffic-dial clear`` removes it.
    Phase 3 — per-region decision. The region's healthy fraction over the
        lookback window maps to a target dial: at or above
        ``FULL_HEALTH_PERCENTage`` the target is 100, below it the target is
        ``max(MIN_DIAL_PERCENTAGE, round(healthy_percent))``. The applied
        change per run is bounded by ``MAX_STEP_PERCENTAGE`` in both
        directions (gradual drain, gradual restore). Missing telemetry holds
        the current dial: an absent signal must never look like ideal health,
        and equally must never trigger a drain.
    Phase 4 — last-healthy-region guard. If every non-overridden decision
        lands below 100, the region with the best health signal is forced
        back to 100 (bypassing the step limit — dialing *up* is safe). The
        dial gates only first-choice traffic and redirects the remainder to
        the next-closest group, and Global Accelerator does not document the
        resulting distribution once *every* group sits below 100 — so the
        guard keeps one fully dialed region as a deterministic absorber of
        redirected traffic at all times.
    Phase 5 — enforcement. In ``enforce`` mode changed dials are applied via
        ``UpdateEndpointGroup`` carrying *only* ``TrafficDialPercentage``.
        The API patches omitted fields, and omitting ``EndpointConfigurations``
        is load-bearing: passing an empty list would detach the region's ALB.
        ``monitor`` mode (the default) computes and publishes but never writes.
    Phase 6 — publication. Every decision is emitted to the
        ``GCO/TrafficDial`` CloudWatch namespace and the full run is stored in
        the ``/{project}/traffic-dial/state`` SSM parameter for
        ``gco capacity traffic-dial show``.

Environment variables:
    LISTENER_ARN             Global Accelerator listener whose endpoint groups
                             are managed (required)
    PROJECT_NAME             deployment prefix for SSM paths and cluster names
                             (required)
    MODE                     "monitor" (default) or "enforce"
    REGIONS                  comma-separated workload regions to evaluate
    LOOKBACK_MINUTES         health window (default 15)
    MIN_DIAL_PERCENTAGE      floor for a degraded region's dial (default 10)
    MAX_STEP_PERCENTAGE      largest change one run may apply (default 20)
    FULL_HEALTH_PERCENTAGE   healthy percent at/above which a region returns
                             to 100 (default 95)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T13:22:56Z
# Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/traffic-dial-controller/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/traffic-dial-controller/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: The Global Accelerator control plane is homed in us-west-2 in the
#: commercial partition (same convention as lambda/ga-registration).
GA_CONTROL_PLANE_REGION = "us-west-2"

#: Namespace the health-monitor service publishes ClusterHealthy to (see
#: gco/services/metrics_publisher.py).
HEALTH_METRIC_NAMESPACE = "GCO/HealthMonitor"

#: Namespace this controller publishes its decisions to.
DIAL_METRIC_NAMESPACE = "GCO/TrafficDial"

#: CloudWatch PutMetricData batch bound (mirrors MetricsPublisher).
METRIC_BATCH_SIZE = 20

DEFAULT_LOOKBACK_MINUTES = 15
DEFAULT_MIN_DIAL_PERCENTAGE = 10
DEFAULT_MAX_STEP_PERCENTAGE = 20
DEFAULT_FULL_HEALTH_PERCENTAGE = 95


def _split_csv(raw: str | None) -> list[str]:
    """Split a comma-separated env value into a clean list."""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _accelerator_arn_from_listener(listener_arn: str) -> str:
    """Derive the accelerator ARN from one of its listener ARNs."""
    return listener_arn.split("/listener/")[0]


def _state_parameter_name(project_name: str) -> str:
    """SSM parameter holding the last run's decisions."""
    return f"/{project_name}/traffic-dial/state"


def _override_prefix(project_name: str) -> str:
    """SSM path under which per-region override parameters live."""
    return f"/{project_name}/traffic-dial/"


def _override_parameter_name(project_name: str, region: str) -> str:
    """SSM parameter naming a manual per-region dial override."""
    return f"/{project_name}/traffic-dial/override-{region}"


def list_endpoint_groups(ga_client: Any, listener_arn: str) -> dict[str, dict[str, Any]]:
    """Return ``{region: {"arn", "traffic_dial"}}`` for the listener."""
    groups: dict[str, dict[str, Any]] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"ListenerArn": listener_arn}
        if token:
            kwargs["NextToken"] = token
        response = ga_client.list_endpoint_groups(**kwargs)
        for group in response.get("EndpointGroups", []):
            region = group.get("EndpointGroupRegion")
            arn = group.get("EndpointGroupArn")
            if not region or not arn:
                continue
            groups[str(region)] = {
                "arn": str(arn),
                "traffic_dial": int(round(float(group.get("TrafficDialPercentage", 100.0)))),
            }
        token = response.get("NextToken")
        if not token:
            return groups


def read_overrides(ssm_client: Any, project_name: str) -> dict[str, str]:
    """Return ``{region: raw_value}`` for every manual override parameter."""
    overrides: dict[str, str] = {}
    prefix = _override_prefix(project_name)
    marker = f"{prefix}override-"
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Path": prefix, "Recursive": False}
        if token:
            kwargs["NextToken"] = token
        response = ssm_client.get_parameters_by_path(**kwargs)
        for parameter in response.get("Parameters", []):
            name = str(parameter.get("Name", ""))
            if name.startswith(marker):
                overrides[name.removeprefix(marker)] = str(parameter.get("Value", ""))
        token = response.get("NextToken")
        if not token:
            return overrides


def healthy_percent(
    region: str,
    cluster_name: str,
    lookback_minutes: int,
    *,
    cloudwatch_client: Any | None = None,
) -> float | None:
    """Average ``ClusterHealthy`` (as a 0-100 percent) over the window.

    Returns ``None`` when the metric produced no datapoints or the regional
    CloudWatch call failed — the caller treats both as "hold the dial".
    """
    try:
        cloudwatch = cloudwatch_client or boto3.client("cloudwatch", region_name=region)
        end = datetime.now(UTC)
        start = end - timedelta(minutes=lookback_minutes)
        values: list[float] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": [
                    {
                        "Id": "healthy",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": HEALTH_METRIC_NAMESPACE,
                                "MetricName": "ClusterHealthy",
                                "Dimensions": [
                                    {"Name": "ClusterName", "Value": cluster_name},
                                    {"Name": "Region", "Value": region},
                                ],
                            },
                            "Period": 60,
                            "Stat": "Average",
                        },
                    }
                ],
                "StartTime": start,
                "EndTime": end,
            }
            if token:
                kwargs["NextToken"] = token
            response = cloudwatch.get_metric_data(**kwargs)
            for result in response.get("MetricDataResults", []):
                values.extend(float(value) for value in result.get("Values", []))
            token = response.get("NextToken")
            if not token:
                break
        if not values:
            return None
        return 100.0 * sum(values) / len(values)
    except Exception as exc:  # noqa: BLE001 - missing telemetry means "hold"
        logger.warning("Health signal unavailable for %s: %s", region, exc)
        return None


def target_dial(healthy: float, min_dial: int, full_health: int) -> int:
    """Map a healthy percent to a target dial percentage."""
    if healthy >= full_health:
        return 100
    return max(min_dial, int(round(healthy)))


def step_limit(current: int, target: int, max_step: int) -> int:
    """Bound the per-run dial change to ``max_step`` in either direction."""
    if target > current:
        return min(target, current + max_step)
    if target < current:
        return max(target, current - max_step)
    return current


def apply_last_healthy_region_guard(decisions: list[dict[str, Any]]) -> str | None:
    """Force the best region to 100 when every computed dial fell below 100.

    Only non-overridden decisions with an endpoint group participate: an
    operator override is explicit intent the controller must not fight. The
    forced restore deliberately bypasses the step limit — dialing up is safe,
    and Global Accelerator health checks still protect against hard-down
    endpoints. Returns the guarded region, or ``None`` when no guard applied.
    """
    candidates = [
        decision
        for decision in decisions
        if decision["reason"] != "override" and decision["new_dial"] is not None
    ]
    if not candidates:
        return None
    if any(decision["new_dial"] >= 100 for decision in candidates):
        return None
    overridden_at_full = any(
        decision["reason"] == "override" and (decision["current_dial"] or 0) >= 100
        for decision in decisions
    )
    if overridden_at_full:
        return None

    best = max(
        candidates,
        key=lambda decision: (
            decision["healthy_percent"] if decision["healthy_percent"] is not None else -1.0,
            decision["region"],
        ),
    )
    best["new_dial"] = 100
    best["reason"] = "guard-last-healthy-region"
    return str(best["region"])


def publish_metrics(cloudwatch_client: Any, decisions: list[dict[str, Any]]) -> None:
    """Emit per-region decision metrics to the GCO/TrafficDial namespace."""
    metric_data: list[dict[str, Any]] = []
    for decision in decisions:
        dimensions = [{"Name": "Region", "Value": decision["region"]}]
        if decision["new_dial"] is not None:
            metric_data.append(
                {
                    "MetricName": "TrafficDialPercentage",
                    "Value": float(decision["new_dial"]),
                    "Unit": "Percent",
                    "Dimensions": dimensions,
                }
            )
        if decision["healthy_percent"] is not None:
            metric_data.append(
                {
                    "MetricName": "HealthyPercent",
                    "Value": float(decision["healthy_percent"]),
                    "Unit": "Percent",
                    "Dimensions": dimensions,
                }
            )
        metric_data.append(
            {
                "MetricName": "HealthDataMissing",
                "Value": 1.0 if decision["reason"] == "no-health-data" else 0.0,
                "Unit": "Count",
                "Dimensions": dimensions,
            }
        )
        metric_data.append(
            {
                "MetricName": "DialApplied",
                "Value": 1.0 if decision["applied"] else 0.0,
                "Unit": "Count",
                "Dimensions": dimensions,
            }
        )
    try:
        for index in range(0, len(metric_data), METRIC_BATCH_SIZE):
            cloudwatch_client.put_metric_data(
                Namespace=DIAL_METRIC_NAMESPACE,
                MetricData=metric_data[index : index + METRIC_BATCH_SIZE],
            )
    except Exception as exc:  # noqa: BLE001 - metrics are advisory
        logger.warning("Failed to publish traffic-dial metrics: %s", exc)


def store_state(ssm_client: Any, project_name: str, state: dict[str, Any]) -> None:
    """Persist the run summary for `gco capacity traffic-dial show`."""
    try:
        ssm_client.put_parameter(
            Name=_state_parameter_name(project_name),
            Value=json.dumps(state, separators=(",", ":")),
            Type="String",
            Overwrite=True,
        )
    except Exception as exc:  # noqa: BLE001 - state is advisory
        logger.warning("Failed to store traffic-dial state: %s", exc)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Evaluate every region's health and converge its traffic dial."""
    listener_arn = os.environ.get("LISTENER_ARN")
    project_name = os.environ.get("PROJECT_NAME")
    if not listener_arn or not project_name:
        raise ValueError("LISTENER_ARN and PROJECT_NAME environment variables are required")

    mode = os.environ.get("MODE", "monitor").strip().lower()
    regions = sorted(_split_csv(os.environ.get("REGIONS")))
    lookback_minutes = int(os.environ.get("LOOKBACK_MINUTES", str(DEFAULT_LOOKBACK_MINUTES)))
    min_dial = int(os.environ.get("MIN_DIAL_PERCENTAGE", str(DEFAULT_MIN_DIAL_PERCENTAGE)))
    max_step = int(os.environ.get("MAX_STEP_PERCENTAGE", str(DEFAULT_MAX_STEP_PERCENTAGE)))
    full_health = int(os.environ.get("FULL_HEALTH_PERCENTAGE", str(DEFAULT_FULL_HEALTH_PERCENTAGE)))
    if not regions:
        logger.warning("REGIONS is empty; nothing to evaluate")

    now = datetime.now(UTC)
    ga_client = boto3.client("globalaccelerator", region_name=GA_CONTROL_PLANE_REGION)
    ssm_client = boto3.client("ssm")

    # Phase 0 — never stack a change onto an accelerator that is still
    # converging a previous one.
    accelerator_arn = _accelerator_arn_from_listener(listener_arn)
    accelerator = ga_client.describe_accelerator(AcceleratorArn=accelerator_arn)
    accelerator_status = str(accelerator.get("Accelerator", {}).get("Status", "UNKNOWN"))
    if accelerator_status != "DEPLOYED":
        logger.info("Accelerator status is %s; skipping this cycle entirely", accelerator_status)
        return {
            "mode": mode,
            "timestamp": now.isoformat(),
            "accelerator_status": accelerator_status,
            "skipped": "accelerator-not-deployed",
            "decisions": [],
            "updates_applied": 0,
            "errors": 0,
        }

    # Phase 1 + 2 — current dials and operator overrides.
    groups = list_endpoint_groups(ga_client, listener_arn)
    overrides = read_overrides(ssm_client, project_name)

    # Phase 3 — per-region decisions.
    decisions: list[dict[str, Any]] = []
    for region in regions:
        group = groups.get(region)
        decision: dict[str, Any] = {
            "region": region,
            "endpoint_group_arn": group["arn"] if group else None,
            "current_dial": group["traffic_dial"] if group else None,
            "healthy_percent": None,
            "target_dial": None,
            "new_dial": None,
            "reason": "no-endpoint-group",
            "applied": False,
        }
        if group is None:
            logger.info("No endpoint group for %s yet; nothing to dial", region)
            decisions.append(decision)
            continue

        current = int(group["traffic_dial"])
        if region in overrides:
            decision.update({"new_dial": current, "target_dial": current, "reason": "override"})
            decisions.append(decision)
            continue

        health = healthy_percent(region, f"{project_name}-{region}", lookback_minutes)
        decision["healthy_percent"] = None if health is None else round(health, 2)
        if health is None:
            # Hold: absent telemetry is neither health nor degradation.
            decision.update(
                {"new_dial": current, "target_dial": current, "reason": "no-health-data"}
            )
            decisions.append(decision)
            continue

        target = target_dial(health, min_dial, full_health)
        decision["target_dial"] = target
        decision["new_dial"] = step_limit(current, target, max_step)
        decision["reason"] = "healthy" if target >= 100 else "degraded"
        decisions.append(decision)

    # Phase 4 — never leave every endpoint group dialed below 100.
    guarded_region = apply_last_healthy_region_guard(decisions)
    if guarded_region:
        logger.warning(
            "Every computed dial fell below 100; holding %s at 100 as the last fully dialed region",
            guarded_region,
        )

    # Phase 5 — enforcement (monitor mode publishes without writing).
    updates_applied = 0
    errors = 0
    for decision in decisions:
        if decision["new_dial"] is None or decision["reason"] == "override":
            continue
        if decision["new_dial"] == decision["current_dial"]:
            continue
        if mode != "enforce":
            continue
        try:
            # Only the dial: UpdateEndpointGroup patches omitted fields, and
            # omitting EndpointConfigurations preserves the registered ALB.
            ga_client.update_endpoint_group(
                EndpointGroupArn=decision["endpoint_group_arn"],
                TrafficDialPercentage=float(decision["new_dial"]),
            )
            decision["applied"] = True
            updates_applied += 1
            logger.info(
                "Dialed %s from %s to %s (%s)",
                decision["region"],
                decision["current_dial"],
                decision["new_dial"],
                decision["reason"],
            )
        except ClientError as exc:
            errors += 1
            decision["error"] = str(exc)
            logger.error("Failed to update traffic dial for %s: %s", decision["region"], exc)

    # Phase 6 — publication.
    summary: dict[str, Any] = {
        "mode": mode,
        "timestamp": now.isoformat(),
        "accelerator_status": accelerator_status,
        "decisions": decisions,
        "updates_applied": updates_applied,
        "errors": errors,
    }
    publish_metrics(boto3.client("cloudwatch"), decisions)
    store_state(ssm_client, project_name, summary)
    logger.info(
        "traffic-dial cycle complete: mode=%s regions=%d updates_applied=%d errors=%d",
        mode,
        len(decisions),
        updates_applied,
        errors,
    )
    return summary
