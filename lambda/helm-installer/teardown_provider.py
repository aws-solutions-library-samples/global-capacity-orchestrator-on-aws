"""Delete-only custom-resource provider for ordered Helm teardown.

Create and update events are deliberate no-ops. On delete, the provider starts
an ordered Step Functions execution that invokes the Helm worker once per chart
and then waits for that execution to finish. A failed uninstall therefore fails
the CloudFormation delete instead of allowing EKS access entries or the cluster
to disappear underneath a still-live release.

Environment variables:
    TEARDOWN_STATE_MACHINE_ARN: Ordered uninstall state machine.
    INSTALL_STATE_MACHINE_ARN: Fire-and-forget convergence state machine to
        stop and drain before teardown starts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-14T03:46:22Z
# Flowchart(s) generated from this file:
#   * ``on_event`` -> ``diagrams/code_diagrams/lambda/helm-installer/teardown_provider.on_event.html``
#     (PNG: ``diagrams/code_diagrams/lambda/helm-installer/teardown_provider.on_event.png``)
#   * ``is_complete`` -> ``diagrams/code_diagrams/lambda/helm-installer/teardown_provider.is_complete.html``
#     (PNG: ``diagrams/code_diagrams/lambda/helm-installer/teardown_provider.is_complete.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

_IN_FLIGHT_LAMBDA_DRAIN_SECONDS = 16 * 60


def _sfn() -> Any:
    return boto3.client("stepfunctions")


def _ssm(region: str) -> Any:
    return boto3.client("ssm", region_name=region)


def _execution_name(event: dict[str, Any]) -> str:
    """Return a retry-stable, Step-Functions-safe execution name."""
    identity = "|".join(
        str(event.get(key, "")) for key in ("StackId", "RequestId", "LogicalResourceId")
    )
    return f"helm-delete-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def _execution_arn(state_machine_arn: str, execution_name: str) -> str:
    """Build the execution ARN corresponding to a state-machine ARN."""
    prefix, separator, state_machine_name = state_machine_arn.partition(":stateMachine:")
    if not separator or not state_machine_name:
        raise ValueError(f"Invalid Step Functions state machine ARN: {state_machine_arn}")
    return f"{prefix}:execution:{state_machine_name}:{execution_name}"


def _stop_running_install_executions(sfn_client: Any, state_machine_arn: str) -> int:
    """Stop every visible convergence execution and return the count stopped.

    ``ListExecutions`` is eventually consistent. The delete provider invokes
    this once before the teardown state machine starts its unconditional drain;
    the state machine then invokes ``drain_install_executions`` after each full
    drain interval and loops whenever a late execution is discovered.
    """
    execution_arns: list[str] = []
    request: dict[str, Any] = {
        "stateMachineArn": state_machine_arn,
        "statusFilter": "RUNNING",
        "maxResults": 100,
    }
    while True:
        response = sfn_client.list_executions(**request)
        execution_arns.extend(
            execution["executionArn"] for execution in response.get("executions", [])
        )
        next_token = response.get("nextToken")
        if not next_token:
            break
        request["nextToken"] = next_token

    for execution_arn in execution_arns:
        try:
            sfn_client.stop_execution(executionArn=execution_arn)
        except ClientError as exc:
            # An execution can finish between ListExecutions and StopExecution.
            # Re-read only a ValidationException and suppress it only when the
            # execution is now terminal; every other failure blocks teardown.
            code = exc.response.get("Error", {}).get("Code")
            if code in {"ExecutionDoesNotExist", "ExecutionNotRunning"}:
                continue
            if code == "ValidationException":
                try:
                    status = sfn_client.describe_execution(executionArn=execution_arn).get("status")
                except ClientError as describe_exc:
                    describe_code = describe_exc.response.get("Error", {}).get("Code")
                    if describe_code == "ExecutionDoesNotExist":
                        continue
                    raise
                if status and status != "RUNNING":
                    continue
            raise

    if execution_arns:
        logger.info("Stopped %d install execution(s)", len(execution_arns))
    return len(execution_arns)


def drain_install_executions(
    _event: dict[str, Any],
    _context: Any = None,
) -> dict[str, int]:
    """State-machine task that stops late work and requests another full drain."""
    stopped = _stop_running_install_executions(
        _sfn(),
        os.environ["INSTALL_STATE_MACHINE_ARN"],
    )
    return {"StoppedExecutions": stopped}


def on_event(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Start ordered teardown on Delete; no-op on Create and Update."""
    request_type = event["RequestType"]
    physical_id = event.get("PhysicalResourceId") or "helm-teardown"

    if request_type != "Delete":
        return {"PhysicalResourceId": physical_id}

    props = event["ResourceProperties"]
    state_machine_arn = os.environ["TEARDOWN_STATE_MACHINE_ARN"]
    install_state_machine_arn = os.environ["INSTALL_STATE_MACHINE_ARN"]
    execution_name = _execution_name(event)
    sfn_client = _sfn()
    fence_name = f"/{props['ProjectName']}/addons/{props['Region']}/_teardown"
    _ssm(str(props["Region"])).put_parameter(
        Name=fence_name,
        Value=execution_name,
        Type="String",
        Overwrite=True,
    )
    _stop_running_install_executions(sfn_client, install_state_machine_arn)
    execution_input = {
        "ClusterName": props["ClusterName"],
        "Region": props["Region"],
        "RegistryRegion": props["RegistryRegion"],
        "ProjectName": props["ProjectName"],
        "EnabledCharts": props.get("EnabledCharts", []),
        "Charts": props.get("Charts", {}),
        "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
        # ListExecutions is eventually consistent and StopExecution cannot
        # cancel an in-flight Lambda. Always drain the full invocation bound;
        # the workflow checker loops if late-visible work is stopped.
        "WaitForInFlightSeconds": _IN_FLIGHT_LAMBDA_DRAIN_SECONDS,
    }
    endpoint_group_arn = props.get("EndpointGroupArn")
    if endpoint_group_arn:
        execution_input["EndpointGroupArn"] = endpoint_group_arn

    try:
        sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(execution_input),
        )
    except ClientError as exc:
        # Provider retries can replay the same Delete request. Reusing the
        # deterministic name is idempotent when that execution already exists.
        if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
            raise

    logger.info("Started ordered Helm teardown execution %s", execution_name)
    return {"PhysicalResourceId": physical_id}


def is_complete(event: dict[str, Any], _context: Any = None) -> dict[str, bool]:
    """Surface teardown terminal status; the workflow owns the drain loop."""
    if event["RequestType"] != "Delete":
        return {"IsComplete": True}

    state_machine_arn = os.environ["TEARDOWN_STATE_MACHINE_ARN"]
    execution_name = _execution_name(event)
    execution_arn = _execution_arn(state_machine_arn, execution_name)
    sfn_client = _sfn()

    response = sfn_client.describe_execution(executionArn=execution_arn)
    status = response["status"]

    if status == "RUNNING":
        return {"IsComplete": False}
    if status == "SUCCEEDED":
        return {"IsComplete": True}

    detail = response.get("cause") or response.get("error") or "no failure detail"
    raise RuntimeError(f"Helm teardown execution {status}: {detail}")
