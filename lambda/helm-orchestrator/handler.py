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

The execution input is bounded by Step Functions' 256 KiB input limit, checked
before anything is written. Its replay copy for ``gco stacks addons install``
lives at ``/<project>/addons/<region>/_input``: the canonical JSON, compressed
with zlib and base64-encoded because SSM refuses a value containing ``{{``.
An encoding larger than one SSM value (8 KiB, the Advanced-tier limit) is
stored in parts ``_input.<sha256[:16]>.<k>``, and ``_input`` then holds the
header ``chunked:v1:<n>:<sha256>`` (see ``_replay_input_writes``).

Environment Variables:
    STATE_MACHINE_ARN: ARN of the helm-install state machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import zlib
from typing import Any

import boto3
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-19T16:50:06Z
# Generated from Git commit: 682ef1b5797ef18690cfc2a021264ed06b70f252
# Flowchart(s) generated from this file:
#   * ``on_event`` -> ``diagrams/code_diagrams/lambda/helm-orchestrator/handler.on_event.html``
#     (PNG: ``diagrams/code_diagrams/lambda/helm-orchestrator/handler.on_event.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Largest SSM parameter value (the Advanced tier's limit); SSM has no larger tier.
_SSM_PARAMETER_MAX_BYTES = 8 * 1024
# Step Functions refuses an execution input over 256 KiB of UTF-8.
_EXECUTION_INPUT_MAX_BYTES = 256 * 1024
# ``_input`` value naming the parts of a replay input too large for one parameter.
_CHUNKED_INPUT_HEADER = re.compile(r"chunked:v1:(?P<count>[1-9][0-9]*):(?P<sha256>[0-9a-f]{64})")
_CHUNK_NAME_DIGEST_CHARS = 16
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


def _chart_overrides(props: dict[str, Any]) -> dict[str, Any]:
    """Return the ``Charts`` overrides with the JSON types the stack gave them.

    CloudFormation delivers custom-resource properties with every number and
    boolean as a string, so the regional stack sends ``Charts`` as one JSON
    string and it is decoded here. An object (a stack synthesized before
    that change) is taken as it is.
    """
    charts = props.get("Charts", {})
    if isinstance(charts, str):
        try:
            charts = json.loads(charts)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Charts is not valid JSON: {exc}") from exc
    if not isinstance(charts, dict):
        raise ValueError("Charts must be a JSON object of per-chart overrides")
    return charts


def _encode_replay_input(execution_input_json: str) -> str:
    """Encode the replay input as zlib+base64 for SSM Parameter Store.

    The raw execution input embeds ``{{PLACEHOLDER}}`` image-replacement keys,
    and SSM rejects any String value containing ``{{}}`` ("Parameter value
    can't nest another parameter"). Base64 is brace-free by construction and
    zlib shrinks the repetitive JSON, usually into one parameter value.
    Consumers (``gco stacks addons install`` and live release validation)
    reverse this exact encoding before use.
    """
    compressed = zlib.compress(execution_input_json.encode("utf-8"), 9)
    return base64.b64encode(compressed).decode("ascii")


def _replay_input_part_name(input_name: str, digest: str, index: int) -> str:
    """Name of part ``index`` (from 1) of an encoded replay input with SHA-256 ``digest``."""
    return f"{input_name}.{digest[:_CHUNK_NAME_DIGEST_CHARS]}.{index}"


def _replay_input_writes(input_name: str, encoded_input: str) -> list[tuple[str, str]]:
    """Return the ``(name, value)`` String parameters that store ``encoded_input``, in order.

    An encoding that fits one parameter value is written to ``input_name``
    unchanged, the only form earlier releases wrote and read. A larger one is
    split into consecutive parts of at most 8 KiB (base64 is ASCII, so the
    character count is the byte count), each written to
    ``<input_name>.<sha256[:16]>.<k>`` for k = 1..n, and only then is
    ``input_name`` written with the header ``chunked:v1:<n>:<sha256>``, the
    SHA-256 taken over the whole encoded string. The part names come from the
    content, so a write that stops part-way never changes the parts the
    current ``_input`` names. The header cannot be mistaken for an older form:
    raw JSON starts with ``{``, and ``:`` is outside the base64 alphabet.
    """
    if len(encoded_input) <= _SSM_PARAMETER_MAX_BYTES:
        return [(input_name, encoded_input)]
    digest = hashlib.sha256(encoded_input.encode("ascii")).hexdigest()
    parts = [
        encoded_input[start : start + _SSM_PARAMETER_MAX_BYTES]
        for start in range(0, len(encoded_input), _SSM_PARAMETER_MAX_BYTES)
    ]
    writes = [
        (_replay_input_part_name(input_name, digest, index), part)
        for index, part in enumerate(parts, start=1)
    ]
    writes.append((input_name, f"chunked:v1:{len(parts)}:{digest}"))
    return writes


def _replay_input_part_names(input_name: str, stored_value: str | None) -> list[str]:
    """Return the part names a stored chunked header refers to; [] for any other value."""
    header = _CHUNKED_INPUT_HEADER.fullmatch(stored_value or "")
    if header is None:
        return []
    count = int(header["count"])
    return [
        _replay_input_part_name(input_name, header["sha256"], index)
        for index in range(1, count + 1)
    ]


def _is_parameter_not_found(exc: BaseException) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ParameterNotFound"
    )


def _stored_replay_input(ssm_client: Any, input_name: str) -> str | None:
    """Return the current ``_input`` value, or None when there is none or it cannot be read.

    It is read only to find the parts of a previous chunked input, deleted once
    the new value is written, so a failed read is logged and never fails the
    deployment.
    """
    try:
        value = ssm_client.get_parameter(Name=input_name)["Parameter"]["Value"]
    except Exception as exc:  # best-effort: only the stale-part cleanup needs it
        if not _is_parameter_not_found(exc):
            logger.warning(
                "Could not read the previous replay input %s: %s",
                input_name,
                _bounded_error_text(exc),
            )
        return None
    return value if isinstance(value, str) else None


def _delete_replay_input_parts(ssm_client: Any, names: list[str]) -> None:
    """Delete parts no longer named by ``_input``, logging failures instead of raising.

    A part left behind is never read, and teardown removes it with the rest of
    the add-on bookkeeping: ``gco stacks destroy-all``
    (``StackManager._cleanup_addon_parameters`` in cli/stacks.py, called by
    ``destroy_orchestrated``) and the live-validation teardown
    (``_post_absence_sweeps`` in scripts/live_release_validation/actions/
    destroy.py) delete every parameter under ``/<project>/addons/<region>``
    found by a recursive GetParametersByPath, whatever its name.
    """
    for name in names:
        try:
            ssm_client.delete_parameter(Name=name)
        except Exception as exc:  # best-effort: a stale part is harmless
            if not _is_parameter_not_found(exc):
                logger.warning(
                    "Could not delete the stale replay-input part %s: %s",
                    name,
                    _bounded_error_text(exc),
                )


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
        "Charts": _chart_overrides(props),
        "KedaOperatorRoleArn": props.get("KedaOperatorRoleArn"),
        "ImageReplacements": props.get("ImageReplacements", {}),
        "DeploymentToken": deployment_token,
    }
    endpoint_group_arn = props.get("EndpointGroupArn")
    if endpoint_group_arn:
        execution_input["EndpointGroupArn"] = endpoint_group_arn
    execution_input_json = _canonical_json(execution_input)
    execution_input_bytes = execution_input_json.encode("utf-8")
    # The input's real bound is StartExecution's, so it is checked before the
    # fence or any parameter is touched. The SSM replay copy has no bound of
    # its own: an encoding over one parameter value is stored in parts.
    if len(execution_input_bytes) > _EXECUTION_INPUT_MAX_BYTES:
        raise ValueError(
            f"Convergence execution input is {len(execution_input_bytes)} bytes; "
            f"Step Functions accepts at most {_EXECUTION_INPUT_MAX_BYTES} bytes (256 KiB)"
        )

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

    # Persist the exact replay input before convergence can mutate the cluster:
    # zlib+base64 because SSM rejects raw ``{{}}`` tokens, in parts when it
    # exceeds one value (``_replay_input_writes``). ``input_sha256`` below is
    # always computed over the raw canonical JSON. Intelligent-Tiering selects
    # an Advanced parameter only for a value over the Standard tier's 4 KiB.
    input_name = f"{parameter_root}/_input"
    writes = _replay_input_writes(input_name, _encode_replay_input(execution_input_json))
    previous_input = _stored_replay_input(ssm, input_name)
    try:
        for name, value in writes:
            ssm.put_parameter(
                Name=name,
                Value=value,
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
        except Exception as stop_exc:  # surface unsafe rollback state
            raise RuntimeError(
                f"Could not confirm untracked execution {execution_arn} stopped"
            ) from stop_exc
        raise RuntimeError(
            "Could not persist the convergence execution identity: " + _bounded_error_text(exc)
        ) from exc

    logger.info("Started and recorded execution %s (fire-and-forget)", execution_arn)

    # Delete the parts of a previous chunked input that the new value does not
    # reuse (a retry of the same input reuses them all). This runs last, once
    # the execution is started and recorded, so a slow or throttled delete
    # cannot delay either.
    current_parts = {name for name, _value in writes if name != input_name}
    _delete_replay_input_parts(
        ssm,
        [
            name
            for name in _replay_input_part_names(input_name, previous_input)
            if name not in current_parts
        ],
    )

    return {
        "PhysicalResourceId": physical_id,
        "ExecutionArn": execution_arn,
        "Data": {"ExecutionArn": execution_arn},
    }
