"""
Helm Orchestrator — CloudFormation custom-resource provider for the helm
install Step Functions state machine.

This is a thin async provider (CDK ``cr.Provider`` with both an ``onEvent`` and
an ``isComplete`` handler). It does no Helm or Kubernetes work itself — that all
happens in the per-chart Step Functions tasks. Its only jobs are:

- ``on_event``: on Create/Update, start a state-machine execution whose input
  carries the chart configuration; on Delete, no-op (the cluster teardown
  removes the charts).
- ``is_complete``: poll the execution and report completion to CloudFormation,
  failing the resource if the execution did not succeed.

Keeping the heavy lifting in Step Functions (one task per chart, with per-chart
retry) means no single Lambda invocation is bound by the 15-minute Lambda limit,
which is the reliability win over the old single-Lambda installer.

Environment Variables:
    STATE_MACHINE_ARN: ARN of the helm-install state machine.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_TERMINAL_OK = "SUCCEEDED"
_TERMINAL_BAD = {"FAILED", "TIMED_OUT", "ABORTED"}


def _sfn() -> Any:
    return boto3.client("stepfunctions")


def on_event(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Start a state-machine execution for Create/Update; no-op for Delete.

    Returns a ``PhysicalResourceId`` and stashes the started ``ExecutionArn`` at
    the top level so the framework passes it through to :func:`is_complete`.
    """
    request_type = event["RequestType"]
    logger.info("on_event %s: %s", request_type, json.dumps(event.get("ResourceProperties", {})))

    physical_id = event.get("PhysicalResourceId") or "helm-install-charts"

    if request_type == "Delete":
        # Charts are torn down with the cluster; nothing to do here.
        return {"PhysicalResourceId": physical_id}

    props = event["ResourceProperties"]
    state_machine_arn = os.environ["STATE_MACHINE_ARN"]

    # The execution input is exactly what the per-chart tasks need.
    execution_input = {
        "ClusterName": props["ClusterName"],
        "Region": props["Region"],
        "EnabledCharts": props.get("EnabledCharts", []),
        "Charts": props.get("Charts", {}),
        "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
    }

    resp = _sfn().start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps(execution_input),
    )
    execution_arn = resp["executionArn"]
    logger.info("Started execution %s", execution_arn)

    return {
        "PhysicalResourceId": physical_id,
        "ExecutionArn": execution_arn,
        "Data": {"ExecutionArn": execution_arn},
    }


def is_complete(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Poll the started execution; raise if it ended in a non-success state."""
    request_type = event["RequestType"]

    if request_type == "Delete":
        return {"IsComplete": True}

    execution_arn = event.get("ExecutionArn") or event.get("Data", {}).get("ExecutionArn")
    if not execution_arn:
        # Nothing was started (should not happen on Create/Update) — treat as
        # incomplete so the provider retries on_event semantics surface clearly.
        raise RuntimeError("is_complete invoked without an ExecutionArn")

    status = _sfn().describe_execution(executionArn=execution_arn)["status"]
    logger.info("Execution %s status=%s", execution_arn, status)

    if status == _TERMINAL_OK:
        return {"IsComplete": True, "Data": {"ExecutionArn": execution_arn}}
    if status in _TERMINAL_BAD:
        raise RuntimeError(f"Helm install execution {execution_arn} ended {status}")
    # RUNNING — keep polling.
    return {"IsComplete": False}
