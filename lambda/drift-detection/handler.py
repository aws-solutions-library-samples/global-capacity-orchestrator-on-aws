"""
CloudFormation Drift Detection Lambda Handler.

This Lambda is invoked on a schedule (default: daily) by an EventBridge rule.
It initiates drift detection on a CloudFormation stack, polls until the
detection completes, and publishes an SNS notification if any resources
have drifted.

Environment Variables:
    STACK_NAME: Name of the CloudFormation stack to check for drift
    SNS_TOPIC_ARN: ARN of the SNS topic to publish drift alerts to
    REGION: AWS region (populated automatically by Lambda)
    POLL_INTERVAL_SECONDS: Seconds between detection status polls (default: 10)
    POLL_MAX_ATTEMPTS: Max poll attempts before giving up (default: 60 = 10 min)

Invocation:
    Triggered by an EventBridge rule. The event payload is ignored — the stack
    name comes from the environment so the Lambda is bound to a specific stack
    at deploy time (one Lambda per stack).

Drift detection is asynchronous in CloudFormation:
    1. DetectStackDrift returns a DriftDetectionId
    2. Poll DescribeStackDriftDetectionStatus until DetectionStatus is COMPLETE or FAILED
    3. If the detection is still running when polling gives up, publish a
       "did not finish" notice instead of reading a drift status that
       CloudFormation has not set yet
    4. If drift is detected (StackDriftStatus == DRIFTED) or the detection
       failed (DETECTION_FAILED: it failed for at least one resource, and the
       results of the others are available), fetch per-resource drifts via
       DescribeStackResourceDrifts and publish a summary to SNS: the drifted
       resources, the resources it could not check (UNKNOWN, with
       CloudFormation's reason) and, apart, those whose type it does not
       support. A failed detection with no drift among the resources it
       checked is published as a failure, drift under the drift subject.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, cast

import boto3

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-19T06:05:34Z
# Generated from Git commit: 1e6de1636d5418ba50126d6619a9bafc70839dad
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/drift-detection/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Poll configuration — kept short to stay well within Lambda's 15-minute max
DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_POLL_MAX_ATTEMPTS = 60  # 10 minutes total at 10-second intervals

# Terminal detection statuses returned by DescribeStackDriftDetectionStatus
TERMINAL_DETECTION_STATUSES = {"DETECTION_COMPLETE", "DETECTION_FAILED"}
# Resource drift statuses that mean the live resource differs from the template.
DRIFTED_RESOURCE_STATUSES = ("MODIFIED", "DELETED")

# Resource drift status of a resource detection could not check (its
# DriftStatusReason says why); a DETECTION_FAILED run has at least one.
FAILED_RESOURCE_STATUS = "UNKNOWN"

# The statuses requested from DescribeStackResourceDrifts: drift, failure, and
# UNSUPPORTED for a resource whose type drift detection does not check. The
# API accepts at most four filters; NOT_CHECKED is left out because the API
# reference says CloudFormation no longer returns it.
RESOURCE_DRIFT_FILTERS = (*DRIFTED_RESOURCE_STATUSES, FAILED_RESOURCE_STATUS, "UNSUPPORTED")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run CloudFormation drift detection and publish SNS alerts on drift.

    Args:
        event: EventBridge scheduled event (payload unused)
        context: Lambda context

    Returns:
        Dict with drift detection results for logging/debugging
    """
    stack_name = os.environ.get("STACK_NAME")
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
    region = os.environ.get("REGION") or os.environ.get("AWS_REGION")

    if not stack_name:
        raise ValueError("STACK_NAME environment variable is required")
    if not sns_topic_arn:
        raise ValueError("SNS_TOPIC_ARN environment variable is required")

    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)))
    poll_max_attempts = int(os.environ.get("POLL_MAX_ATTEMPTS", str(DEFAULT_POLL_MAX_ATTEMPTS)))

    logger.info(
        "Starting drift detection for stack=%s region=%s",
        stack_name,
        region,
    )

    cfn = boto3.client("cloudformation", region_name=region)
    sns = boto3.client("sns", region_name=region)

    # 1. Initiate drift detection
    detection_id = cfn.detect_stack_drift(StackName=stack_name)["StackDriftDetectionId"]
    logger.info("Drift detection initiated: detection_id=%s", detection_id)

    # 2. Poll until detection reaches a terminal state (or we time out)
    status_response = _poll_detection_status(cfn, detection_id, poll_interval, poll_max_attempts)
    detection_status = status_response.get("DetectionStatus")
    stack_drift_status = status_response.get("StackDriftStatus")

    logger.info(
        "Drift detection polling ended: detection_status=%s stack_drift_status=%s",
        detection_status,
        stack_drift_status,
    )

    # 3. Still running when polling gave up: the stack's drift status is not
    # final and the resource drifts may be a previous run's, so report that
    # the run did not finish rather than a drift it cannot vouch for.
    if detection_status not in TERMINAL_DETECTION_STATUSES:
        _publish_alert(
            sns,
            sns_topic_arn,
            subject=f"[GCO] Drift detection did not finish for {stack_name}",
            message={
                "stack_name": stack_name,
                "region": region,
                "detection_id": detection_id,
                "detection_status": detection_status,
                "waited_seconds": poll_interval * poll_max_attempts,
            },
        )
        return {
            "stack_name": stack_name,
            "detection_status": detection_status,
            "stack_drift_status": None,
            "drift_published": True,
        }

    # 4. A completed detection is drift only when the stack is DRIFTED:
    # IN_SYNC is clean, and NOT_CHECKED or UNKNOWN say nothing about the
    # resources. A failed one failed for at least one resource while the
    # results of the others are available, so it is always reported, with
    # whatever drift those results show.
    detection_failed = detection_status == "DETECTION_FAILED"
    if not detection_failed and stack_drift_status != "DRIFTED":
        logger.info("Stack %s is %s — no alert published", stack_name, stack_drift_status)
        return {
            "stack_name": stack_name,
            "detection_status": detection_status,
            "stack_drift_status": stack_drift_status,
            "drift_published": False,
        }

    # 5. List the resource drifts and publish one alert: drift when a checked
    # resource drifted (or the completed stack status says so), otherwise the
    # failure.
    drifted_resources, failed_resources, not_checked_resources = _list_resource_drifts(
        cfn, stack_name
    )
    if detection_failed and not drifted_resources:
        subject = f"[GCO] Drift detection FAILED for {stack_name}"
    else:
        subject = f"[GCO] Drift detected in stack {stack_name}"
    message: dict[str, Any] = {
        "stack_name": stack_name,
        "region": region,
        "detection_status": detection_status,
        "stack_drift_status": stack_drift_status,
        "drifted_resource_count": len(drifted_resources),
        "drifted_resources": drifted_resources,
        "failed_resource_count": len(failed_resources),
        "failed_resources": failed_resources,
        "not_checked_resource_count": len(not_checked_resources),
        "not_checked_resources": not_checked_resources,
    }
    if detection_failed:
        message["reason"] = status_response.get(
            "DetectionStatusReason", "Unknown detection failure"
        )
    _publish_alert(sns, sns_topic_arn, subject=subject, message=message)

    return {
        "stack_name": stack_name,
        "detection_status": detection_status,
        "stack_drift_status": stack_drift_status,
        "drifted_resource_count": len(drifted_resources),
        "failed_resource_count": len(failed_resources),
        "not_checked_resource_count": len(not_checked_resources),
        "drift_published": True,
    }


def _poll_detection_status(
    cfn: Any,
    detection_id: str,
    poll_interval: int,
    max_attempts: int,
) -> dict[str, Any]:
    """Poll DescribeStackDriftDetectionStatus until terminal or timeout."""
    for attempt in range(max_attempts):
        response = cfn.describe_stack_drift_detection_status(StackDriftDetectionId=detection_id)
        status = response.get("DetectionStatus")
        logger.debug("Poll attempt %d: detection_status=%s", attempt + 1, status)
        if status in TERMINAL_DETECTION_STATUSES:
            return cast("dict[str, Any]", response)
        time.sleep(poll_interval)

    # Timed out — return last response so caller can decide what to do
    logger.warning(
        "Drift detection did not complete within %d polls; returning last status",
        max_attempts,
    )
    return cast("dict[str, Any]", response)


def _list_resource_drifts(
    cfn: Any, stack_name: str
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """List the drifted resources, the ones detection failed on, and the unchecked ones.

    A resource whose type drift detection does not support is never checked;
    counting it as drifted put it in every alert, so it is listed apart. A
    resource detection could not check (UNKNOWN) carries CloudFormation's
    DriftStatusReason. Returns trimmed representations suitable for embedding
    in an SNS message.
    """
    drifted: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    not_checked: list[dict[str, str]] = []
    paginator = cfn.get_paginator("describe_stack_resource_drifts")
    # Filter server-side to reduce response size for large stacks
    for page in paginator.paginate(
        StackName=stack_name, StackResourceDriftStatusFilters=list(RESOURCE_DRIFT_FILTERS)
    ):
        for resource in page.get("StackResourceDrifts", []):
            status = resource.get("StackResourceDriftStatus", "")
            identity = {
                "logical_id": resource.get("LogicalResourceId", ""),
                "resource_type": resource.get("ResourceType", ""),
            }
            if status in DRIFTED_RESOURCE_STATUSES:
                drifted.append(
                    {
                        "logical_id": identity["logical_id"],
                        "physical_id": resource.get("PhysicalResourceId", ""),
                        "resource_type": identity["resource_type"],
                        "drift_status": status,
                    }
                )
            elif status == FAILED_RESOURCE_STATUS:
                failed.append({**identity, "reason": resource.get("DriftStatusReason", "")})
            else:
                not_checked.append(identity)
    return drifted, failed, not_checked


def _publish_alert(sns: Any, topic_arn: str, subject: str, message: dict[str, Any]) -> None:
    """Publish a JSON alert to SNS. Subject is truncated to the 100-char limit."""
    # SNS subjects have a 100-char max; truncate defensively
    truncated_subject = subject[:100]
    sns.publish(
        TopicArn=topic_arn,
        Subject=truncated_subject,
        Message=json.dumps(message, indent=2, default=str),
    )
    logger.info("Published drift alert to %s", topic_arn)
