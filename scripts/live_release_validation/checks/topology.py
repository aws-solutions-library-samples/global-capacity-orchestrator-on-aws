"""Add-on convergence, health stability, and topology evidence validators."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import zlib
from datetime import datetime
from typing import Any

from ..constants import (
    _HEALTHY_STACK_STATUSES,
)
from ..models import RunContext, to_jsonable, utc_now


def _queue_counts(status: dict[str, Any]) -> dict[str, int]:
    return {
        "available": int(status.get("messages_available", 0)),
        "in_flight": int(status.get("messages_in_flight", 0)),
        "delayed": int(status.get("messages_delayed", 0)),
        "dlq": int(status.get("dlq_messages", 0)),
    }


_ADDON_EXECUTION_FIELDS = frozenset(
    {
        "execution_arn",
        "state_machine_arn",
        "deployment_token",
        "cluster_name",
        "region",
        "input_sha256",
        "started_at",
    }
)


_ADDON_REQUIRED_INPUT_FIELDS = frozenset(
    {
        "ClusterName",
        "Region",
        "RegistryRegion",
        "ProjectName",
        "EnabledCharts",
        "Charts",
        "KedaOperatorRoleArn",
        "ImageReplacements",
        "DeploymentToken",
    }
)


_ADDON_OPTIONAL_INPUT_FIELDS = frozenset({"EndpointGroupArn"})


_ADDON_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})


_ADDON_FAILURE_STATUSES = _ADDON_TERMINAL_STATUSES - {"SUCCEEDED"}


_ADDON_CONVERGENCE_TIMEOUT_SECONDS = 2 * 60 * 60


_HEALTH_STABILITY_ROUNDS = 3


_MAX_TOPOLOGY_EVIDENCE_CHARS = 2048


def _bounded_topology_evidence(
    value: Any,
    limit: int = _MAX_TOPOLOGY_EVIDENCE_CHARS,
) -> str:
    """Serialize diagnostic evidence without allowing an unbounded checkpoint."""
    if value is None:
        text = "<absent>"
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(to_jsonable(value), sort_keys=True)
        except TypeError, ValueError:
            text = str(value)
    suffix = "... [truncated]"
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def _topology_json_object(
    value: Any,
    description: str,
    *,
    canonical: bool,
) -> dict[str, Any]:
    """Decode a JSON object, optionally requiring the provider's exact encoding."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{description} is not a non-empty JSON string")

    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-standard JSON constant {constant}")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{description} is invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{description} must be a JSON object")
    if canonical and json.dumps(parsed, sort_keys=True, separators=(",", ":")) != value:
        raise RuntimeError(f"{description} is not exact canonical JSON")
    return parsed


def _decode_replay_input_parameter(stored_value: str, description: str) -> str:
    """Reverse the helm orchestrator's zlib+base64 replay-input encoding.

    The orchestrator stores the convergence execution input encoded because
    SSM rejects raw ``{{PLACEHOLDER}}`` tokens. ``input_sha256`` in the
    companion ``_execution`` parameter is always computed over the decoded
    canonical JSON returned here.
    """
    try:
        compressed = base64.b64decode(stored_value.encode("ascii"), validate=True)
        return zlib.decompress(compressed).decode("utf-8")
    except (ValueError, zlib.error, UnicodeDecodeError) as exc:
        raise RuntimeError(f"{description} is not zlib+base64 replay input: {exc}") from exc


def _ssm_string_parameter(client: Any, name: str) -> str:
    """Read one exact String parameter and reject a malformed SDK response."""
    response = client.get_parameter(Name=name)
    parameter = response.get("Parameter") if isinstance(response, dict) else None
    if not isinstance(parameter, dict):
        raise RuntimeError(f"SSM parameter response is malformed for {name}")
    if parameter.get("Name") != name or parameter.get("Type") != "String":
        raise RuntimeError(f"SSM parameter identity/type is invalid for {name}")
    value = parameter.get("Value")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"SSM parameter has no String value: {name}")
    return value


def _epoch_seconds(value: Any, description: str) -> int:
    """Normalize an SDK timestamp while rejecting booleans and invalid values."""
    if isinstance(value, datetime):
        raw_value: Any = value.timestamp()
    else:
        raw_value = value
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        raise RuntimeError(f"{description} is not a timestamp")
    try:
        result = int(raw_value)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError(f"{description} is not a finite timestamp") from exc
    if result <= 0:
        raise RuntimeError(f"{description} must be positive")
    return result


def _validate_addon_arns(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    stack_id: str,
    state_machine_arn: str,
    execution_arn: str,
) -> None:
    """Require exact account, partition, Region, and parent state-machine ARNs."""
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for {region}")
    escaped_partition = re.escape(str(partition))
    escaped_region = re.escape(region)
    escaped_account = re.escape(ctx.settings.expected_account)
    escaped_stack_name = re.escape(stack_name)

    stack_pattern = (
        rf"arn:{escaped_partition}:cloudformation:{escaped_region}:{escaped_account}:"
        rf"stack/{escaped_stack_name}/[^/:\s]+"
    )
    if re.fullmatch(stack_pattern, stack_id) is None:
        raise RuntimeError(f"Regional stack ID has the wrong ARN identity: {stack_id}")

    state_machine_pattern = (
        rf"arn:{escaped_partition}:states:{escaped_region}:{escaped_account}:"
        r"stateMachine:([^:\s]+)"
    )
    state_machine_match = re.fullmatch(state_machine_pattern, state_machine_arn)
    if state_machine_match is None:
        raise RuntimeError(
            f"Add-on state-machine ARN has the wrong account/partition/Region: {state_machine_arn}"
        )
    execution_pattern = (
        rf"arn:{escaped_partition}:states:{escaped_region}:{escaped_account}:"
        rf"execution:{re.escape(state_machine_match.group(1))}:[^:\s]+"
    )
    if re.fullmatch(execution_pattern, execution_arn) is None:
        raise RuntimeError(
            f"Add-on execution ARN is not an execution of {state_machine_arn}: {execution_arn}"
        )


def _state_machine_stack_resource(
    ctx: RunContext,
    *,
    region: str,
    stack_id: str,
    state_machine_arn: str,
) -> dict[str, str]:
    """Prove the physical state machine belongs to the exact regional stack ARN."""
    cloudformation = ctx.session.client("cloudformation", region_name=region)
    pages = cloudformation.get_paginator("list_stack_resources").paginate(StackName=stack_id)
    matches = [
        resource
        for page in pages
        for resource in page.get("StackResourceSummaries", [])
        if resource.get("ResourceType") == "AWS::StepFunctions::StateMachine"
        and resource.get("PhysicalResourceId") == state_machine_arn
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"State machine {state_machine_arn} is not exactly one "
            f"AWS::StepFunctions::StateMachine resource in stack {stack_id}"
        )
    resource = matches[0]
    logical_id = resource.get("LogicalResourceId")
    resource_status = resource.get("ResourceStatus")
    if not isinstance(logical_id, str) or not logical_id:
        raise RuntimeError(f"State-machine stack resource lacks a logical ID in {stack_id}")
    if resource_status not in _HEALTHY_STACK_STATUSES:
        raise RuntimeError(
            f"State-machine stack resource {logical_id} is not complete: {resource_status}"
        )
    return {
        "logical_id": logical_id,
        "physical_id": state_machine_arn,
        "resource_type": "AWS::StepFunctions::StateMachine",
        "status": str(resource_status),
    }


def _validate_terminal_validator(
    output: dict[str, Any],
    *,
    key: str,
    deployment_token: str,
    count_pairs: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Validate one terminal convergence payload and each expected/actual count pair."""
    validator = output.get(key)
    if not isinstance(validator, dict):
        raise RuntimeError(f"Step Functions output lacks object {key}")
    if validator.get("status") != "validated":
        raise RuntimeError(f"Step Functions output {key}.status is not exactly 'validated'")
    if validator.get("DeploymentToken") != deployment_token:
        raise RuntimeError(f"Step Functions output {key} has a stale deployment token")
    for expected_key, validated_key in count_pairs:
        expected = validator.get(expected_key)
        validated = validator.get(validated_key)
        if (
            isinstance(expected, bool)
            or isinstance(validated, bool)
            or not isinstance(expected, int)
            or not isinstance(validated, int)
            or expected < 0
            or validated < 0
        ):
            raise RuntimeError(
                f"Step Functions output {key} has invalid counts {expected_key}/{validated_key}"
            )
        if expected != validated:
            raise RuntimeError(
                f"Step Functions output {key} did not validate every item: "
                f"{expected_key}={expected}, {validated_key}={validated}"
            )
    return validator


def _validate_addon_execution_input(
    input_value: dict[str, Any],
    *,
    cluster_name: str,
    region: str,
    registry_region: str,
    project_name: str,
    deployment_token: str,
) -> None:
    """Require the exact current orchestrator input schema and regional identity."""
    fields = set(input_value)
    if not _ADDON_REQUIRED_INPUT_FIELDS.issubset(fields) or not fields.issubset(
        _ADDON_REQUIRED_INPUT_FIELDS | _ADDON_OPTIONAL_INPUT_FIELDS
    ):
        raise RuntimeError("Add-on execution input does not use the exact current schema")
    if input_value.get("ClusterName") != cluster_name:
        raise RuntimeError("Add-on execution input has a stale cluster name")
    if input_value.get("Region") != region:
        raise RuntimeError("Add-on execution input has a stale Region")
    if input_value.get("RegistryRegion") != registry_region:
        raise RuntimeError("Add-on execution input has a stale registry Region")
    if input_value.get("ProjectName") != project_name:
        raise RuntimeError("Add-on execution input has a stale project name")
    if input_value.get("DeploymentToken") != deployment_token:
        raise RuntimeError("Add-on execution input has a stale deployment token")
    enabled_charts = input_value.get("EnabledCharts")
    if not isinstance(enabled_charts, list) or not all(
        isinstance(item, str) and item for item in enabled_charts
    ):
        raise RuntimeError("Add-on execution input EnabledCharts must be a string list")
    if not isinstance(input_value.get("Charts"), dict):
        raise RuntimeError("Add-on execution input Charts must be an object")
    if not isinstance(input_value.get("ImageReplacements"), dict):
        raise RuntimeError("Add-on execution input ImageReplacements must be an object")
    keda_role_arn = input_value.get("KedaOperatorRoleArn")
    if not isinstance(keda_role_arn, str | type(None)):
        raise RuntimeError("Add-on execution input KedaOperatorRoleArn must be a string or null")
    if "EndpointGroupArn" in input_value:
        endpoint_group_arn = input_value["EndpointGroupArn"]
        if not isinstance(endpoint_group_arn, str) or not endpoint_group_arn:
            raise RuntimeError("Add-on execution input EndpointGroupArn must be non-empty")


def _poll_addon_execution(
    ctx: RunContext,
    *,
    region: str,
    execution: dict[str, Any],
    input_json: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Poll one exact execution to a bounded terminal result and validate its output."""
    execution_arn = str(execution["execution_arn"])
    state_machine_arn = str(execution["state_machine_arn"])
    deployment_token = str(execution["deployment_token"])
    poll_interval = max(0.0, float(ctx.settings.poll_interval_seconds))
    deadline = time.monotonic() + _ADDON_CONVERGENCE_TIMEOUT_SECONDS + poll_interval
    stepfunctions = ctx.session.client("stepfunctions", region_name=region)

    while True:
        response = stepfunctions.describe_execution(executionArn=execution_arn)
        if not isinstance(response, dict):
            raise RuntimeError(f"DescribeExecution returned a malformed response in {region}")
        if response.get("executionArn") != execution_arn:
            raise RuntimeError(f"DescribeExecution returned a different execution in {region}")
        if response.get("stateMachineArn") != state_machine_arn:
            raise RuntimeError(f"DescribeExecution returned a different state machine in {region}")
        if response.get("input") != input_json:
            raise RuntimeError(f"DescribeExecution returned stale execution input in {region}")
        if (
            _epoch_seconds(response.get("startDate"), "DescribeExecution startDate")
            != execution["started_at"]
        ):
            raise RuntimeError(f"DescribeExecution start time changed in {region}")

        status = response.get("status")
        if not isinstance(status, str):
            raise RuntimeError(f"DescribeExecution returned no status in {region}")
        observation = {
            "observed_at": utc_now(),
            "status": status,
            "execution_arn": execution_arn,
        }
        for field in ("error", "cause", "output"):
            if field in response:
                observation[field] = _bounded_topology_evidence(response.get(field))
        evidence.setdefault("observations", []).append(observation)
        ctx.persist()

        if status == "RUNNING":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Add-on execution {execution_arn} did not finish within "
                    f"{_ADDON_CONVERGENCE_TIMEOUT_SECONDS + poll_interval:.1f} seconds"
                )
            time.sleep(min(poll_interval if poll_interval > 0 else 0.1, remaining))
            continue
        if status not in _ADDON_TERMINAL_STATUSES:
            raise RuntimeError(f"Add-on execution {execution_arn} has unknown status {status}")

        evidence["execution_status"] = status
        if status in _ADDON_FAILURE_STATUSES:
            terminal = {
                field: _bounded_topology_evidence(response.get(field))
                for field in ("error", "cause", "output")
            }
            evidence["terminal"] = {"status": status, **terminal}
            ctx.persist()
            raise RuntimeError(
                f"Add-on execution {execution_arn} ended {status}; "
                f"error={terminal['error']}; cause={terminal['cause']}; "
                f"output={terminal['output']}"
            )

        output = _topology_json_object(
            response.get("output"),
            f"Step Functions output for {region}",
            canonical=False,
        )
        manifest_validation = _validate_terminal_validator(
            output,
            key="manifestValidation",
            deployment_token=deployment_token,
            count_pairs=(("ExpectedCount", "ValidatedCount"),),
        )
        helm_validation = _validate_terminal_validator(
            output,
            key="helmValidation",
            deployment_token=deployment_token,
            count_pairs=(
                ("expected_release_count", "validated_release_count"),
                ("expected_resource_count", "validated_resource_count"),
            ),
        )
        terminal_evidence: dict[str, Any] = {
            "status": status,
            "manifestValidation": to_jsonable(manifest_validation),
            "helmValidation": to_jsonable(helm_validation),
        }
        evidence["terminal"] = terminal_evidence
        ctx.persist()
        return terminal_evidence


def _converge_region_addons(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    stack: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    """Validate persisted identity and wait for exact current add-on convergence."""
    stack_id = str(stack.get("stack_id") or "")
    outputs = stack.get("outputs")
    if not isinstance(outputs, dict):
        raise RuntimeError(f"Regional stack {stack_name} has malformed outputs")
    cluster_name = f"{ctx.config.project_name}-{region}"
    if outputs.get("ClusterName") != cluster_name:
        raise RuntimeError(f"Regional stack {stack_name} has a stale ClusterName output")
    deployment_token = outputs.get("AddonDeploymentToken")
    if not isinstance(deployment_token, str) or not deployment_token:
        raise RuntimeError(f"Regional stack {stack_name} has no AddonDeploymentToken output")

    parameter_root = f"/{ctx.config.project_name}/addons/{region}"
    execution_parameter = f"{parameter_root}/_execution"
    input_parameter = f"{parameter_root}/_input"
    ssm = ctx.session.client("ssm", region_name=region)
    execution_json = _ssm_string_parameter(ssm, execution_parameter)
    input_json = _decode_replay_input_parameter(
        _ssm_string_parameter(ssm, input_parameter),
        f"SSM parameter {input_parameter}",
    )
    execution = _topology_json_object(
        execution_json,
        f"SSM parameter {execution_parameter}",
        canonical=True,
    )
    input_value = _topology_json_object(
        input_json,
        f"SSM parameter {input_parameter}",
        canonical=True,
    )

    if set(execution) != _ADDON_EXECUTION_FIELDS:
        raise RuntimeError(f"SSM parameter {execution_parameter} has an unexpected schema")
    for field in (
        "execution_arn",
        "state_machine_arn",
        "deployment_token",
        "cluster_name",
        "region",
        "input_sha256",
    ):
        if not isinstance(execution.get(field), str) or not execution[field]:
            raise RuntimeError(f"SSM parameter {execution_parameter} has invalid {field}")
    started_at = execution.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, int) or started_at <= 0:
        raise RuntimeError(f"SSM parameter {execution_parameter} has invalid started_at")
    if execution["deployment_token"] != deployment_token:
        raise RuntimeError(f"SSM parameter {execution_parameter} has a stale deployment token")
    if execution["cluster_name"] != cluster_name or execution["region"] != region:
        raise RuntimeError(f"SSM parameter {execution_parameter} has stale regional identity")
    input_sha256 = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
    if execution["input_sha256"] != input_sha256:
        raise RuntimeError(f"SSM parameter {execution_parameter} has a stale input SHA-256")
    _validate_addon_execution_input(
        input_value,
        cluster_name=cluster_name,
        region=region,
        registry_region=ctx.config.global_region,
        project_name=ctx.config.project_name,
        deployment_token=deployment_token,
    )
    _validate_addon_arns(
        ctx,
        region=region,
        stack_name=stack_name,
        stack_id=stack_id,
        state_machine_arn=execution["state_machine_arn"],
        execution_arn=execution["execution_arn"],
    )
    stack_resource = _state_machine_stack_resource(
        ctx,
        region=region,
        stack_id=stack_id,
        state_machine_arn=execution["state_machine_arn"],
    )

    evidence.update(
        {
            "stack_id": stack_id,
            "cluster_name": cluster_name,
            "deployment_token": deployment_token,
            "execution": to_jsonable(execution),
            "input": to_jsonable(input_value),
            "input_sha256": input_sha256,
            "state_machine_resource": stack_resource,
        }
    )
    ctx.persist()
    _poll_addon_execution(
        ctx,
        region=region,
        execution=execution,
        input_json=input_json,
        evidence=evidence,
    )


def _validate_health_payload(
    ctx: RunContext,
    payload: Any,
    *,
    endpoint_region: str | None,
) -> dict[str, Any]:
    """Require a healthy, well-formed response bound to one deployed cluster."""
    if not isinstance(payload, dict):
        raise RuntimeError("health response is not a JSON object")
    if payload.get("status") != "healthy":
        raise RuntimeError("health response status is not exactly 'healthy'")
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or "T" not in timestamp:
        raise RuntimeError("health response timestamp is not an ISO date-time")
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp)
    except ValueError as exc:
        raise RuntimeError("health response timestamp is not an ISO date-time") from exc
    payload_region = payload.get("region")
    if payload_region not in ctx.deployment_regions:
        raise RuntimeError(f"health response Region is not deployed: {payload_region!r}")
    if endpoint_region is not None and payload_region != endpoint_region:
        raise RuntimeError(
            f"regional health response came from {payload_region!r}, expected {endpoint_region!r}"
        )
    expected_cluster_id = f"{ctx.config.project_name}-{payload_region}"
    if payload.get("cluster_id") != expected_cluster_id:
        raise RuntimeError(
            f"health response cluster_id is not {expected_cluster_id!r}: "
            f"{payload.get('cluster_id')!r}"
        )
    return payload


def _health_stability_samples(
    ctx: RunContext,
    *,
    global_url: str,
    regional_urls: dict[str, str],
) -> list[dict[str, Any]]:
    """Collect three fail-fast, single-attempt rounds from every enabled endpoint."""
    probes: list[dict[str, Any]] = [
        {"scope": "global", "region": None, "endpoint": global_url},
        *(
            {"scope": "regional", "region": region, "endpoint": regional_urls[region]}
            for region in ctx.deployment_regions
            if region in regional_urls
        ),
    ]
    samples: list[dict[str, Any]] = []
    ctx.checkpoint.state["topology_health_samples"] = samples
    ctx.persist()
    interval = min(max(0.0, float(ctx.settings.poll_interval_seconds)), 5.0)

    for round_number in range(1, _HEALTH_STABILITY_ROUNDS + 1):
        for probe in probes:
            started = time.monotonic()
            payload: Any = None
            sample: dict[str, Any]
            try:
                payload = ctx.aws_client.call_api(
                    method="GET",
                    path="/api/v1/health",
                    region=probe["region"],
                    max_attempts=1,
                )
            except Exception as exc:
                sample = {
                    **probe,
                    "round": round_number,
                    "timestamp": utc_now(),
                    "latency_seconds": round(max(0.0, time.monotonic() - started), 6),
                    "payload": None,
                    "error": _bounded_topology_evidence(f"{type(exc).__name__}: {exc}"),
                }
                samples.append(sample)
                ctx.persist()
                raise RuntimeError(
                    f"Health stability call failed for {probe['endpoint']} in round "
                    f"{round_number}: {sample['error']}"
                ) from exc

            error: str | None = None
            try:
                _validate_health_payload(ctx, payload, endpoint_region=probe["region"])
            except RuntimeError as exc:
                error = _bounded_topology_evidence(str(exc))
            sample = {
                **probe,
                "round": round_number,
                "timestamp": utc_now(),
                "latency_seconds": round(max(0.0, time.monotonic() - started), 6),
                "payload": to_jsonable(payload),
                "error": error,
            }
            samples.append(sample)
            ctx.persist()
            if error is not None:
                raise RuntimeError(
                    f"Malformed health response from {probe['endpoint']} in round "
                    f"{round_number}: {error}"
                )
        if round_number < _HEALTH_STABILITY_ROUNDS and interval > 0:
            time.sleep(interval)
    return samples
