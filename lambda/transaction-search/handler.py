"""CloudFormation custom resource that turns on CloudWatch Transaction Search.

GCO's API services export OpenTelemetry spans straight to the X-Ray OTLP
endpoint (``https://xray.<region>.amazonaws.com/v1/traces``), and that endpoint
only accepts spans once CloudWatch Transaction Search is on in the Region. Each
regional stack therefore carries one of these resources (behind a CDK
``cr.Provider``) while ``tracing.enabled`` and
``tracing.enable_transaction_search`` are both true.

Transaction Search is an account-level setting, configured per Region and
shared with every other traced workload in the account, so the resource is
strictly additive:

* **Create/Update** read the X-Ray trace segment destination. When it already
  is ``CloudWatchLogs`` (``ACTIVE``, or ``PENDING`` while X-Ray finishes a
  switch someone else started) nothing is changed. Otherwise the handler writes
  the CloudWatch Logs resource policy that lets X-Ray deliver spans into the
  ``aws/spans`` and ``/aws/application-signals/data`` log groups, then switches
  the destination to ``CloudWatchLogs``.
* **Delete** is a no-op. Other workloads may rely on Transaction Search, and
  switching it off changes how every X-Ray trace in the Region is stored and
  billed, so GCO never disables it and never removes the policy.

CloudFormation's native ``AWS::XRay::TransactionSearchConfig`` is not used: it
can only be created while Transaction Search is off, and it would tie a shared
account-level setting to one stack's lifecycle.

Failures raise, which the provider framework reports as a FAILED resource. The
message names the ``tracing.enable_transaction_search`` opt-out for
organizations that manage the setting themselves.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: CloudWatch Logs resource policy GCO writes (account-level, per Region).
#: Deliberately never deleted; the live release harness restores its own
#: baseline instead.
POLICY_NAME = "gco-transaction-search-xray-access"

#: X-Ray trace segment destination that means "Transaction Search is on".
CLOUDWATCH_LOGS_DESTINATION = "CloudWatchLogs"

#: The cdk.json knob an operator flips to leave the setting unmanaged.
OPT_OUT_SETTING = "tracing.enable_transaction_search"

#: Bounded, standard-mode retries: X-Ray and CloudWatch Logs throttle
#: configuration APIs, and a throttled deploy should retry, not fail.
_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 5, "mode": "standard"},
)

#: ResourceProperties the regional stack must send on Create/Update.
_REQUIRED_PROPERTIES = ("Region", "AccountId", "Partition")


def transaction_search_resource_policy(*, partition: str, region: str, account: str) -> str:
    """Render the resource policy that lets X-Ray write spans into CloudWatch Logs.

    Mirrors the policy in the AWS "Enable transaction search" guide: only the
    ``xray.amazonaws.com`` principal, only ``logs:PutLogEvents``, only the two
    Transaction Search log groups of this Region, and only on behalf of this
    account's X-Ray (``aws:SourceArn`` / ``aws:SourceAccount``), so the policy
    cannot be used as a confused deputy by another account.
    """
    log_group_prefix = f"arn:{partition}:logs:{region}:{account}:log-group:"
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    f"{log_group_prefix}aws/spans:*",
                    f"{log_group_prefix}/aws/application-signals/data:*",
                ],
                "Condition": {
                    "ArnLike": {"aws:SourceArn": f"arn:{partition}:xray:{region}:{account}:*"},
                    "StringEquals": {"aws:SourceAccount": account},
                },
            }
        ],
    }
    return json.dumps(document, separators=(",", ":"))


def ensure_transaction_search(
    xray: Any,
    logs: Any,
    *,
    partition: str,
    region: str,
    account: str,
) -> dict[str, str]:
    """Enable Transaction Search unless it is already on, and report the result.

    Returns the custom-resource ``Data``: the trace segment ``Destination`` and
    ``Status`` after the call, and ``Changed`` (``"true"``/``"false"``) —
    whether this invocation switched the destination. Values are strings
    because CloudFormation exposes custom-resource data through ``Fn::GetAtt``
    as strings.
    """
    current = xray.get_trace_segment_destination()
    destination = str(current.get("Destination", ""))
    status = str(current.get("Status", ""))
    if destination == CLOUDWATCH_LOGS_DESTINATION:
        logger.info(
            "Transaction Search already routes spans to CloudWatch Logs in %s (%s); "
            "leaving it unchanged",
            region,
            status,
        )
        return {"Destination": destination, "Status": status, "Changed": "false"}

    logger.info(
        "Enabling Transaction Search in %s (current destination %s, %s)",
        region,
        destination or "unknown",
        status or "unknown",
    )
    # Policy first, as in the AWS procedure: without it X-Ray has no
    # permission to deliver spans into the Transaction Search log groups.
    logs.put_resource_policy(
        policyName=POLICY_NAME,
        policyDocument=transaction_search_resource_policy(
            partition=partition, region=region, account=account
        ),
    )
    updated = xray.update_trace_segment_destination(Destination=CLOUDWATCH_LOGS_DESTINATION)
    return {
        "Destination": str(updated.get("Destination", CLOUDWATCH_LOGS_DESTINATION)),
        "Status": str(updated.get("Status", "")),
        "Changed": "true",
    }


def _required_properties(properties: dict[str, Any]) -> dict[str, str]:
    """Return the non-empty string properties Create/Update need, or raise."""
    values: dict[str, str] = {}
    for name in _REQUIRED_PROPERTIES:
        value = properties.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Transaction Search resource property {name} must be a string")
        values[name] = value.strip()
    return values


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """``cr.Provider`` on-event entrypoint: enable on Create/Update, keep on Delete."""
    request_type = event.get("RequestType")
    properties = event.get("ResourceProperties") or {}
    project_name = str(properties.get("ProjectName") or "gco")
    physical_id = str(
        event.get("PhysicalResourceId")
        or f"{project_name}-transaction-search-{properties.get('Region', 'unknown')}"
    )

    if request_type == "Delete":
        logger.info(
            "Delete requested; leaving Transaction Search and the %s policy in place "
            "because other workloads in the account may depend on them",
            POLICY_NAME,
        )
        return {"PhysicalResourceId": physical_id}
    if request_type not in ("Create", "Update"):
        raise ValueError(f"Unsupported RequestType {request_type!r}")

    values = _required_properties(properties)
    region = values["Region"]
    try:
        data = ensure_transaction_search(
            boto3.client("xray", region_name=region, config=_CLIENT_CONFIG),
            boto3.client("logs", region_name=region, config=_CLIENT_CONFIG),
            partition=values["Partition"],
            region=region,
            account=values["AccountId"],
        )
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            f"Could not enable CloudWatch Transaction Search in {region}: {exc}. "
            "GCO's traces need it; enable it in the CloudWatch console "
            "(Application Signals > Transaction Search), or set "
            f"{OPT_OUT_SETTING} to false in cdk.json if your organization "
            "manages it."
        ) from exc
    logger.info("Transaction Search in %s: %s", region, data)
    return {"PhysicalResourceId": physical_id, "Data": data}
