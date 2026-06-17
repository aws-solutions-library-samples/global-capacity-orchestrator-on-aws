"""
Helm Orchestrator — CloudFormation custom-resource provider for the helm
install Step Functions state machine.

This is a thin **fire-and-forget** provider (CDK ``cr.Provider`` with only an
``onEvent`` handler — no ``isComplete`` waiter). It does no Helm or Kubernetes
work itself; that all happens in the per-chart Step Functions tasks. Its only
job is:

- ``on_event``: on Create/Update, start a state-machine execution whose input
  carries the chart configuration and return success immediately; on Delete,
  no-op (the cluster teardown removes the charts).

Add-on installation is intentionally decoupled from the CloudFormation lifecycle
entirely. Earlier this provider polled the execution to completion via an
``isComplete`` handler, but that re-coupled the cluster's create to the helm
batch: a slow chart (e.g. volcano retrying docker.io image pulls) keeps the
execution ``RUNNING`` past CloudFormation's ~1-hour custom-resource ceiling, at
which point CloudFormation declares "did not receive a response" and rolls back
— destroying the freshly-created EKS cluster over a recoverable add-on problem.

So the custom resource now reports success as soon as the execution is *started*.
The state machine then converges every chart it can in the background (each chart
task catches its own failure and continues, so one broken chart never blocks the
rest). The real per-chart outcome is recorded out-of-band in SSM by the installer
tasks (``/<project>/addons/<region>/<chart>``) and surfaced via
``gco stacks addons status``; a degraded add-on layer is re-converged with
``gco stacks addons install`` rather than by tearing the cluster down.

Keeping the heavy lifting in Step Functions (one task per chart, with per-chart
retry) also means no single Lambda invocation is bound by the 15-minute Lambda
limit.

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


def _sfn() -> Any:
    return boto3.client("stepfunctions")


def on_event(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Start a state-machine execution for Create/Update; no-op for Delete.

    Returns immediately after starting the execution — the custom resource is
    fire-and-forget, so CloudFormation considers the resource created as soon as
    the helm-install state machine has been *kicked off*, never waiting for the
    charts to finish. The started ``ExecutionArn`` is returned in ``Data`` purely
    as an observability attribute (it does not gate resource completion).
    """
    request_type = event["RequestType"]
    logger.info("on_event %s: %s", request_type, json.dumps(event.get("ResourceProperties", {})))

    physical_id = event.get("PhysicalResourceId") or "helm-install-charts"

    if request_type == "Delete":
        # Charts are torn down with the cluster; nothing to do here.
        return {"PhysicalResourceId": physical_id}

    props = event["ResourceProperties"]
    state_machine_arn = os.environ["STATE_MACHINE_ARN"]

    # The execution input carries everything the convergence pipeline's tasks
    # need: chart selection/overrides (chart tasks), ImageReplacements (the base
    # and post-Helm kubectl tasks), and EndpointGroupArn (the GA task).
    execution_input = {
        "ClusterName": props["ClusterName"],
        "Region": props["Region"],
        "EnabledCharts": props.get("EnabledCharts", []),
        "Charts": props.get("Charts", {}),
        "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
        "ImageReplacements": props.get("ImageReplacements", {}),
        "EndpointGroupArn": props.get("EndpointGroupArn"),
    }

    resp = _sfn().start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps(execution_input),
    )
    execution_arn = resp["executionArn"]
    logger.info("Started execution %s (fire-and-forget; not waiting)", execution_arn)

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
