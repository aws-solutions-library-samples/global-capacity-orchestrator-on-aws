"""
Helm Orchestrator — CloudFormation custom-resource provider for the helm
install Step Functions state machine.

This is a thin **fire-and-forget** provider (CDK ``cr.Provider`` with only an
``onEvent`` handler — no ``isComplete`` waiter). It does no Helm or Kubernetes
work itself; that all happens in the per-chart Step Functions tasks. Its only
job is:

- ``on_event``: on Create/Update, start a state-machine execution whose input
  carries the chart configuration, persist its exact convergence identity, and
  return success immediately; on Delete, no-op (the cluster teardown removes
  the charts).

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

import base64
import hashlib
import json
import logging
import os
import time
import zlib
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SSM_PARAMETER_MAX_BYTES = 8 * 1024
_MAX_EXECUTION_NAME_GENERATIONS = 100
_STOP_CONFIRMATION_SECONDS = 10
_STOP_CONFIRMATION_POLL_SECONDS = 0.2
_BOUNDED_ERROR_CHARS = 512


def _sfn() -> Any:
    return boto3.client("stepfunctions")


def _ssm() -> Any:
    return boto3.client("ssm")


def _canonical_json(value: Any) -> str:
    """Serialize *value* deterministically for execution and persistence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _encode_replay_input(execution_input_json: str) -> str:
    """Encode the replay input as zlib+base64 for SSM Parameter Store.

    The raw execution input embeds ``{{PLACEHOLDER}}`` image-replacement keys,
    and SSM rejects any String value containing ``{{}}`` ("Parameter value
    can't nest another parameter"). Base64 is brace-free by construction and
    zlib keeps the highly repetitive JSON inside the 8 KiB Advanced-tier bound.
    Consumers (``gco stacks addons install`` and live release validation)
    reverse this exact encoding before use.
    """
    compressed = zlib.compress(execution_input_json.encode("utf-8"), 9)
    return base64.b64encode(compressed).decode("ascii")


def _bounded_error_text(exc: BaseException) -> str:
    """Render an exception for a CloudFormation response without overflowing it.

    Botocore validation errors echo the full offending parameter value, and a
    custom-resource response larger than 4 KiB is rejected wholesale with the
    unhelpful "Response object is too long" — masking the real failure.
    """
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > _BOUNDED_ERROR_CHARS:
        text = text[:_BOUNDED_ERROR_CHARS] + " ...[truncated; see provider logs]"
    return text


def _execution_name(request_id: Any, generation: int = 0) -> str:
    """Return a retry-stable, Step-Functions-safe generation name."""
    request_digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:48]
    suffix = "" if generation == 0 else f"-{generation}"
    return f"helm-install-{request_digest}{suffix}"


def _execution_arn(state_machine_arn: str, execution_name: str) -> str:
    """Build the execution ARN corresponding to a state-machine ARN."""
    prefix, separator, state_machine_name = state_machine_arn.partition(":stateMachine:")
    if not separator or not state_machine_name:
        raise ValueError(f"Invalid Step Functions state machine ARN: {state_machine_arn}")
    return f"{prefix}:execution:{state_machine_name}:{execution_name}"


def _start_or_adopt_execution(
    sfn_client: Any,
    *,
    state_machine_arn: str,
    execution_input_json: str,
    request_id: Any,
) -> dict[str, Any]:
    """Start one retry-safe execution or adopt an identical running attempt."""
    if request_id is None:
        return dict(
            sfn_client.start_execution(
                stateMachineArn=state_machine_arn,
                input=execution_input_json,
            )
        )

    for generation in range(_MAX_EXECUTION_NAME_GENERATIONS):
        execution_name = _execution_name(request_id, generation)
        try:
            return dict(
                sfn_client.start_execution(
                    stateMachineArn=state_machine_arn,
                    input=execution_input_json,
                    name=execution_name,
                )
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                raise

        execution_arn = _execution_arn(state_machine_arn, execution_name)
        existing = sfn_client.describe_execution(executionArn=execution_arn)
        if existing.get("status") == "RUNNING":
            if existing.get("input") != execution_input_json:
                raise RuntimeError(
                    f"Running retry execution {execution_arn} has non-identical input"
                )
            return {
                "executionArn": execution_arn,
                "startDate": existing.get("startDate"),
            }

    raise RuntimeError(
        f"Exhausted {_MAX_EXECUTION_NAME_GENERATIONS} retry-safe execution generations"
    )


def _stop_execution_and_wait(sfn_client: Any, execution_arn: str) -> None:
    """Stop an untracked execution and require terminal confirmation."""
    sfn_client.stop_execution(executionArn=execution_arn)
    deadline = time.monotonic() + _STOP_CONFIRMATION_SECONDS
    while time.monotonic() < deadline:
        status = sfn_client.describe_execution(executionArn=execution_arn).get("status")
        if status != "RUNNING":
            return
        time.sleep(_STOP_CONFIRMATION_POLL_SECONDS)
    raise TimeoutError(f"Execution {execution_arn} remained RUNNING after StopExecution")


def _prepare_teardown_fence(ssm_client: Any, *, request_type: str, fence_name: str) -> None:
    """Clear a stale fence on create and reject convergence during deletion."""
    if request_type == "Create":
        try:
            ssm_client.delete_parameter(Name=fence_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
                raise
        return

    try:
        ssm_client.get_parameter(Name=fence_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return
        raise
    raise RuntimeError(f"Regional add-on teardown fence is active: {fence_name}")


def _started_at(response: dict[str, Any]) -> int:
    """Return the execution start time as Unix seconds."""
    start_date = response.get("startDate")
    if start_date is None:
        return int(time.time())
    if isinstance(start_date, int | float):
        return int(start_date)
    return int(start_date.timestamp())


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
    deployment_token = props["DeploymentTimestamp"]

    # The execution input carries everything the convergence pipeline's tasks
    # need, including the registry identity consumed by the unconditional final
    # Gateway endpoint-publication task. EndpointGroupArn is present only where
    # Global Accelerator exists.
    execution_input = {
        "ClusterName": props["ClusterName"],
        "Region": props["Region"],
        "RegistryRegion": props["RegistryRegion"],
        "ProjectName": props["ProjectName"],
        "EnabledCharts": props.get("EnabledCharts", []),
        "Charts": props.get("Charts", {}),
        "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
        "ImageReplacements": props.get("ImageReplacements", {}),
        "DeploymentToken": deployment_token,
    }
    endpoint_group_arn = props.get("EndpointGroupArn")
    if endpoint_group_arn:
        execution_input["EndpointGroupArn"] = endpoint_group_arn
    execution_input_json = _canonical_json(execution_input)
    execution_input_bytes = execution_input_json.encode("utf-8")
    # No raw-JSON size gate here: what SSM stores is the zlib+base64 encoding,
    # whose own bound is enforced below before any write. Gating on the raw
    # bytes rejected deployments whose encoded form fit comfortably — caught
    # live by example-job validation run ex241-edf33111-r2, where enabling
    # every optional chart pushed the raw input to 8771 bytes while its
    # encoded form stayed under half the Advanced-tier limit.

    project = str(props["ProjectName"])
    region = str(props["Region"])
    parameter_root = f"/{project}/addons/{region}"
    input_sha256 = hashlib.sha256(execution_input_bytes).hexdigest()
    ssm = _ssm()
    fence_name = f"{parameter_root}/_teardown"
    _prepare_teardown_fence(
        ssm,
        request_type=request_type,
        fence_name=fence_name,
    )

    # Persist the exact replay input before convergence can mutate the cluster.
    # The value is zlib+base64 encoded because SSM rejects raw ``{{}}`` tokens;
    # ``input_sha256`` below is always computed over the raw canonical JSON.
    # Intelligent-Tiering selects an Advanced parameter only when the payload
    # exceeds the Standard tier's 4 KiB limit, while retaining the 8 KiB bound
    # enforced above.
    encoded_input = _encode_replay_input(execution_input_json)
    if len(encoded_input) > _SSM_PARAMETER_MAX_BYTES:
        raise ValueError(
            f"Encoded convergence replay input is {len(encoded_input)} bytes; "
            f"SSM Parameter Store supports at most {_SSM_PARAMETER_MAX_BYTES} bytes"
        )
    try:
        ssm.put_parameter(
            Name=f"{parameter_root}/_input",
            Value=encoded_input,
            Type="String",
            Tier="Intelligent-Tiering",
            Overwrite=True,
        )
    except Exception as exc:
        logger.error("Replay-input persistence failed", exc_info=True)
        raise RuntimeError(
            "Could not persist the convergence replay input: " + _bounded_error_text(exc)
        ) from exc

    request_id = event.get("RequestId")
    sfn_client = _sfn()
    resp = _start_or_adopt_execution(
        sfn_client,
        state_machine_arn=state_machine_arn,
        execution_input_json=execution_input_json,
        request_id=request_id,
    )
    execution_arn = resp["executionArn"]

    execution_metadata = {
        "execution_arn": execution_arn,
        "state_machine_arn": state_machine_arn,
        "deployment_token": deployment_token,
        "cluster_name": props["ClusterName"],
        "region": region,
        "input_sha256": input_sha256,
        "started_at": _started_at(resp),
    }
    try:
        ssm.put_parameter(
            Name=f"{parameter_root}/_execution",
            Value=_canonical_json(execution_metadata),
            Type="String",
            Overwrite=True,
        )
    except Exception as exc:
        # Do not leave an untracked execution mutating a stack whose provider
        # is about to fail and potentially roll back. The replay input remains
        # available for an explicit retry.
        logger.error("Execution-metadata persistence failed", exc_info=True)
        try:
            _stop_execution_and_wait(sfn_client, execution_arn)
        except Exception as stop_exc:  # noqa: BLE001 - surface unsafe rollback state
            raise RuntimeError(
                f"Could not confirm untracked execution {execution_arn} stopped"
            ) from stop_exc
        raise RuntimeError(
            "Could not persist the convergence execution identity: " + _bounded_error_text(exc)
        ) from exc

    logger.info("Started and recorded execution %s (fire-and-forget)", execution_arn)

    return {
        "PhysicalResourceId": physical_id,
        "ExecutionArn": execution_arn,
        "Data": {"ExecutionArn": execution_arn},
    }
