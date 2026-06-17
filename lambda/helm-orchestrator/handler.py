"""
Helm Orchestrator — CloudFormation custom-resource provider for the helm
install Step Functions state machine.

This is a thin async provider (CDK ``cr.Provider`` with both an ``onEvent`` and
an ``isComplete`` handler). It does no Helm or Kubernetes work itself — that all
happens in the per-chart Step Functions tasks. Its only jobs are:

- ``on_event``: on Create/Update, start a state-machine execution whose input
  carries the chart configuration; on Delete, no-op (the cluster teardown
  removes the charts).
- ``is_complete``: poll the execution and report completion to CloudFormation.

Add-on installation is intentionally decoupled from the CloudFormation rollback
path: a chart that fails to install must never roll back (and thereby destroy)
the freshly-created EKS cluster. So ``is_complete`` reports the resource as
complete once the state-machine execution reaches ANY terminal state — success
or failure. The real per-chart outcome is recorded out-of-band (SSM, by the
installer tasks) and surfaced via ``gco stacks addons-status``; a degraded
add-on layer is re-converged with ``gco stacks install-addons`` rather than by
tearing the cluster down. The state machine itself installs every chart it can
(each chart task catches its own failure and continues), so one slow or broken
chart never blocks the rest.

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

    # Persist the execution input so the add-on install can be replayed
    # out-of-band (gco stacks addons install) without the CLI reconstructing
    # chart config or the KEDA role ARN. Best-effort: never block the deploy.
    project = props.get("ProjectName")
    region = props.get("Region")
    if project and region:
        import contextlib

        with contextlib.suppress(Exception):
            boto3.client("ssm").put_parameter(
                Name=f"/{project}/addons/{region}/_input",
                Value=json.dumps(execution_input),
                Type="String",
                Overwrite=True,
            )

    return {
        "PhysicalResourceId": physical_id,
        "ExecutionArn": execution_arn,
        "Data": {"ExecutionArn": execution_arn},
    }


def is_complete(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Poll the started execution; report complete on ANY terminal state.

    Add-on install is decoupled from the CloudFormation rollback path: a failed
    or timed-out execution must NOT fail this custom resource, because that would
    roll back and destroy the EKS cluster over a recoverable add-on problem.
    Terminal-bad states are logged (and the per-chart detail lives in SSM,
    written by the installer tasks) but still report the resource as complete.
    """
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
        # Non-fatal by design: log loudly but let the stack complete. The
        # cluster stays up; re-converge add-ons with `gco stacks install-addons`.
        logger.warning(
            "Helm install execution %s ended %s; reporting add-on resource "
            "complete anyway so the cluster is not rolled back. Inspect "
            "per-chart status in SSM (/<project>/addons/<region>/<chart>).",
            execution_arn,
            status,
        )
        return {"IsComplete": True, "Data": {"ExecutionArn": execution_arn}}
    # RUNNING — keep polling.
    return {"IsComplete": False}
