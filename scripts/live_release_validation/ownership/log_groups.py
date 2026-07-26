"""CloudWatch log-group identity, stability observation, and ownership checkpointing."""

from __future__ import annotations

import copy
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from botocore.exceptions import ClientError

from ..constants import (
    _EKS_LOG_GROUP_SUFFIXES,
    _LOG_CLEANUP_TOKEN_TAG,
    _LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
    _LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS,
    _LOG_GROUP_OBSERVATION_ATTEMPTS,
    _LOG_GROUP_OBSERVATION_HISTORY_LIMIT,
    _LOG_GROUP_OBSERVATION_POLL_SECONDS,
    _LOG_GROUP_RETRYABLE_OBSERVATION_CODES,
    _LOG_GROUP_SOURCE_TYPES,
    _RUN_STACK_TAG,
)
from ..inventory import (
    describe_stack,
)
from ..models import RunContext, utc_now
from ..ownership.stacks import (
    _owned_stack_record,
)


def _derived_log_group_names(resource_type: str, physical_id: str) -> tuple[str, ...]:
    if resource_type == "AWS::Logs::LogGroup":
        return (physical_id,)
    if resource_type == "AWS::Lambda::Function":
        return (f"/aws/lambda/{physical_id}",)
    if resource_type == "AWS::EKS::Cluster":
        return (
            f"/aws/eks/{physical_id}/cluster",
            *(
                f"/aws/containerinsights/{physical_id}/{suffix}"
                for suffix in _EKS_LOG_GROUP_SUFFIXES
            ),
        )
    return ()


def _live_eks_cluster_identity(
    ctx: RunContext,
    region: str,
    cluster_name: str,
) -> dict[str, str]:
    """Require the exact ACTIVE service-side cluster before deriving log authority."""
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for EKS cluster in {region}")
    expected_arn = (
        f"arn:{partition}:eks:{region}:{ctx.settings.expected_account}:cluster/{cluster_name}"
    )
    response = ctx.session.client("eks", region_name=region).describe_cluster(name=cluster_name)
    cluster = response.get("cluster")
    if not isinstance(cluster, dict):
        raise RuntimeError(f"EKS omitted cluster identity for {region}:{cluster_name}")
    identity = {
        "name": str(cluster.get("name") or ""),
        "arn": str(cluster.get("arn") or ""),
        "status": str(cluster.get("status") or ""),
    }
    if identity != {"name": cluster_name, "arn": expected_arn, "status": "ACTIVE"}:
        raise RuntimeError(
            f"EKS cluster identity is not exact and ACTIVE for {region}:{cluster_name}"
        )
    return identity


def _eks_cluster_log_authority_identity(
    ctx: RunContext,
    region: str,
    cluster_name: str,
    *,
    allow_deleted: bool,
) -> dict[str, str]:
    """Resolve EKS log authority, tolerating a rolled-back (deleted) cluster.

    A create rollback deletes the cluster itself while its control-plane and
    Container Insights log groups survive. The DELETED tombstone identity is
    only ever derived from this run's own stack resource record.
    """
    if not allow_deleted:
        return _live_eks_cluster_identity(ctx, region, cluster_name)
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for EKS cluster in {region}")
    expected_arn = (
        f"arn:{partition}:eks:{region}:{ctx.settings.expected_account}:cluster/{cluster_name}"
    )
    try:
        return _live_eks_cluster_identity(ctx, region, cluster_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") != "ResourceNotFoundException":
            raise
        return {"name": cluster_name, "arn": expected_arn, "status": "DELETED"}


def _validated_owned_log_group_identity(
    ctx: RunContext,
    record: Mapping[str, Any],
) -> tuple[str, str]:
    """Validate an exact log-group name derived from a checkpointed stack resource."""
    region = str(record.get("region") or "")
    name = str(record.get("name") or "")
    stack_name = str(record.get("stack_name") or "")
    stack_id = str(record.get("stack_id") or "")
    resource_type = str(record.get("source_resource_type") or "")
    logical_id = str(record.get("source_logical_id") or "")
    physical_id = str(record.get("source_physical_id") or "")
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict) or str(target_regions.get(stack_name) or "") != region:
        raise RuntimeError(f"Log-group checkpoint target stack is invalid for {region}:{name}")
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for log group in {region}")
    expected_stack_prefix = (
        f"arn:{partition}:cloudformation:{region}:{ctx.settings.expected_account}:"
        f"stack/{stack_name}/"
    )
    owned_stack_record = _owned_stack_record(ctx, region, stack_name)
    expected_stack_id = str((owned_stack_record or {}).get("stack_id") or "")
    if (
        not name
        or not logical_id
        or resource_type not in _LOG_GROUP_SOURCE_TYPES
        or name not in _derived_log_group_names(resource_type, physical_id)
    ):
        raise RuntimeError(f"Log-group checkpoint source is invalid for {region}:{name}")
    source_service_identity = record.get("source_service_identity")
    if resource_type == "AWS::EKS::Cluster":
        expected_arn = (
            f"arn:{partition}:eks:{region}:{ctx.settings.expected_account}:cluster/{physical_id}"
        )
        # ACTIVE is the pre-destroy authority; DELETED is the exact tombstone
        # recorded when a rolled-back create removed the cluster but left its
        # control-plane and Container Insights log groups behind.
        accepted_source_identities = tuple(
            {"name": physical_id, "arn": expected_arn, "status": status}
            for status in ("ACTIVE", "DELETED")
        )
        if source_service_identity not in accepted_source_identities:
            raise RuntimeError(
                f"Log-group checkpoint lacks exact live EKS identity for {region}:{name}"
            )
    elif source_service_identity not in (None, {}):
        raise RuntimeError(f"Unexpected service identity for log group {region}:{name}")
    cleanup_token = str(record.get("cleanup_token") or "")
    expected_cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
    if (
        not expected_stack_id.startswith(expected_stack_prefix)
        or stack_id != expected_stack_id
        or (owned_stack_record or {}).get("run_tag") != ctx.settings.run_id
        or record.get("run_tag") != ctx.settings.run_id
        or record.get("ownership_authority") != "cloudformation-stack-resource-derived"
        or record.get("authority_phase") != "pre-destroy"
        or not cleanup_token
        or cleanup_token != expected_cleanup_token
    ):
        raise RuntimeError(f"Log-group checkpoint authority is invalid for {region}:{name}")
    return region, name


def _describe_exact_log_group(client: Any, name: str) -> dict[str, Any] | None:
    kwargs: dict[str, Any] = {"logGroupNamePrefix": name, "limit": 50}
    while True:
        response = client.describe_log_groups(**kwargs)
        for log_group in response.get("logGroups", []):
            if not isinstance(log_group, Mapping):
                raise RuntimeError("CloudWatch Logs returned a non-object log-group record")
            candidate = cast(Mapping[str, Any], log_group)
            if str(candidate.get("logGroupName") or "") == name:
                return {str(key): value for key, value in candidate.items()}
        token = response.get("nextToken")
        if not token:
            return None
        kwargs["nextToken"] = token


def _log_group_identity(client: Any, region: str, name: str) -> dict[str, Any] | None:
    log_group = _describe_exact_log_group(client, name)
    if log_group is None:
        return None
    arn = str(log_group.get("logGroupArn") or log_group.get("arn") or "").removesuffix(":*")
    creation_time = log_group.get("creationTime")
    if not arn or not isinstance(creation_time, int):
        raise RuntimeError(f"CloudWatch Logs omitted identity for {region}:{name}")
    tags = client.list_tags_for_resource(resourceArn=arn).get("tags") or {}
    return {
        "arn": arn,
        "creation_time": creation_time,
        "tags": {str(key): str(value) for key, value in tags.items()},
    }


def _log_group_generation(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable fields that distinguish same-name log generations."""
    arn = str(identity.get("arn") or "")
    creation_time = identity.get("creation_time")
    if not arn or not isinstance(creation_time, int):
        raise RuntimeError("Checkpointed CloudWatch log-group identity is malformed")
    return {"arn": arn, "creation_time": creation_time}


def _observe_log_group_stability(
    client: Any,
    region: str,
    name: str,
    *,
    expected_identity: Mapping[str, Any] | None = None,
    expected_tags: Mapping[str, str] | None = None,
    required_present: int | None,
    required_absent: int | None,
    attempts: int = _LOG_GROUP_OBSERVATION_ATTEMPTS,
    poll_seconds: float = _LOG_GROUP_OBSERVATION_POLL_SECONDS,
) -> dict[str, Any]:
    """Bound identity reads until presence/absence is stable or a fence is crossed."""
    for label, value in (
        ("attempts", attempts),
        ("required_present", required_present),
        ("required_absent", required_absent),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{label} must be a positive integer or None")
    if required_present is None and required_absent is None:
        raise ValueError("At least one stable log-group outcome must be requested")
    if poll_seconds < 0:
        raise ValueError("poll_seconds must be non-negative")

    expected_generation = (
        _log_group_generation(expected_identity) if expected_identity is not None else None
    )
    expected_authority_tags = {str(key): str(value) for key, value in (expected_tags or {}).items()}
    observations: list[dict[str, Any]] = []
    seen_generations: list[dict[str, Any]] = []
    present_streak = 0
    absent_streak = 0
    replacement_streak = 0
    replacement_generation: dict[str, Any] | None = None
    last_identity: dict[str, Any] | None = None

    def result(status: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "region": region,
            "name": name,
            "attempt_count": len(observations),
            "observations": observations,
            "identity": copy.deepcopy(last_identity),
            **extra,
        }

    for attempt in range(1, attempts + 1):
        observed_at = utc_now()
        try:
            identity = _log_group_identity(client, region, name)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code not in _LOG_GROUP_RETRYABLE_OBSERVATION_CODES:
                raise
            observations.append(
                {
                    "attempt": attempt,
                    "observed_at": observed_at,
                    "status": "retryable-error",
                    "error_code": code,
                    "error": str(exc),
                }
            )
            present_streak = 0
            absent_streak = 0
            replacement_streak = 0
            replacement_generation = None
        else:
            last_identity = copy.deepcopy(identity)
            if identity is None:
                if expected_generation is None and seen_generations:
                    observations.append(
                        {
                            "attempt": attempt,
                            "observed_at": observed_at,
                            "status": "replacement",
                            "observed_generation": None,
                        }
                    )
                    return result(
                        "replacement",
                        expected_generation=seen_generations[-1],
                        observed_generation=None,
                    )
                replacement_streak = 0
                replacement_generation = None
                absent_streak += 1
                present_streak = 0
                observations.append(
                    {
                        "attempt": attempt,
                        "observed_at": observed_at,
                        "status": "absent",
                        "consecutive": absent_streak,
                    }
                )
                if required_absent is not None and absent_streak >= required_absent:
                    return result("absent", consecutive=absent_streak)
            else:
                generation = _log_group_generation(identity)
                if generation not in seen_generations:
                    seen_generations.append(generation)
                observations.append(
                    {
                        "attempt": attempt,
                        "observed_at": observed_at,
                        "status": "present",
                        "generation": copy.deepcopy(generation),
                    }
                )
                if expected_generation is not None and generation != expected_generation:
                    if generation == replacement_generation:
                        replacement_streak += 1
                    else:
                        replacement_generation = generation
                        replacement_streak = 1
                    observations[-1]["status"] = "replacement-candidate"
                    observations[-1]["consecutive"] = replacement_streak
                    present_streak = 0
                    absent_streak = 0
                    if replacement_streak >= _LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS:
                        observations[-1]["status"] = "replacement"
                        return result(
                            "replacement",
                            expected_generation=expected_generation,
                            observed_generation=generation,
                            consecutive=replacement_streak,
                        )
                    if attempt < attempts:
                        time.sleep(poll_seconds)
                    continue
                replacement_streak = 0
                replacement_generation = None
                if expected_generation is None and len(seen_generations) > 1:
                    observations[-1]["status"] = "replacement"
                    return result(
                        "replacement",
                        expected_generation=seen_generations[0],
                        observed_generation=generation,
                    )
                tags = identity.get("tags") or {}
                tag_drift = {
                    key: {"expected": value, "observed": tags.get(key)}
                    for key, value in expected_authority_tags.items()
                    if tags.get(key) != value
                }
                if tag_drift:
                    observations[-1]["status"] = "tag-drift"
                    observations[-1]["tag_drift"] = copy.deepcopy(tag_drift)
                    return result("tag-drift", tag_drift=tag_drift)
                present_streak += 1
                absent_streak = 0
                observations[-1]["consecutive"] = present_streak
                if required_present is not None and present_streak >= required_present:
                    return result("present", consecutive=present_streak)
        if attempt < attempts:
            time.sleep(poll_seconds)

    return result(
        "unsettled",
        required_present=required_present,
        required_absent=required_absent,
        present_streak=present_streak,
        absent_streak=absent_streak,
    )


def _record_log_group_observation(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    phase: str,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist bounded identity evidence and any confirmed replacement generation."""
    entry = {"phase": phase, "recorded_at": utc_now(), **copy.deepcopy(dict(outcome))}
    with ctx.state_lock:
        history = record.setdefault("identity_observation_history", [])
        if not isinstance(history, list):
            raise RuntimeError("Log-group identity_observation_history must be a list")
        history.append(entry)
        del history[:-_LOG_GROUP_OBSERVATION_HISTORY_LIMIT]
        if outcome.get("status") == "replacement":
            replacements = record.setdefault("replacement_evidence", [])
            if not isinstance(replacements, list):
                raise RuntimeError("Log-group replacement_evidence must be a list")
            replacements.append(copy.deepcopy(entry))
        ctx.persist_callback(ctx.checkpoint)
    return entry


def _record_log_group_checkpoint_incident(
    ctx: RunContext,
    candidate: Mapping[str, Any],
    *,
    phase: str,
    outcome: Mapping[str, Any],
) -> None:
    """Preserve failed pre-authority observations without adopting the generation."""
    with ctx.state_lock:
        incidents = ctx.checkpoint.state.setdefault("log_group_checkpoint_incidents", [])
        if not isinstance(incidents, list):
            raise RuntimeError("Checkpoint log_group_checkpoint_incidents must be a list")
        incidents.append(
            {
                "phase": phase,
                "recorded_at": utc_now(),
                "candidate": copy.deepcopy(dict(candidate)),
                "outcome": copy.deepcopy(dict(outcome)),
            }
        )
        ctx.persist_callback(ctx.checkpoint)


def _set_log_group_disposition(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    status: str,
    phase: str,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    disposition = {
        "status": status,
        "phase": phase,
        "recorded_at": utc_now(),
        "original_identity": copy.deepcopy(record.get("observed_identity")),
        "last_observation_status": str(outcome.get("status") or ""),
    }
    with ctx.state_lock:
        record["original_generation_disposition"] = disposition
        ctx.persist_callback(ctx.checkpoint)
    return disposition


def _checkpoint_owned_log_groups(ctx: RunContext) -> list[dict[str, Any]]:
    """Fence, tag, and checkpoint exact generations while source stacks are live."""
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict):
        raise RuntimeError("Checkpoint target_stack_regions must be an object")
    try:
        run_started_ms = int(datetime.fromisoformat(ctx.checkpoint.created_at).timestamp() * 1000)
    except ValueError as exc:
        raise RuntimeError("Checkpoint created_at is not a valid timestamp") from exc

    with ctx.state_lock:
        cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
        if not cleanup_token:
            cleanup_token = uuid.uuid4().hex
            ctx.checkpoint.state["log_group_cleanup_token"] = cleanup_token
        if not re.fullmatch(r"[0-9a-f]{32}", cleanup_token):
            raise RuntimeError("Checkpoint log-group cleanup token is malformed")
        records = ctx.checkpoint.state.setdefault("owned_log_groups", [])
        if not isinstance(records, list):
            raise RuntimeError("Checkpoint owned_log_groups must be a list")
        by_identity = {
            (str(item.get("region") or ""), str(item.get("name") or "")): item
            for item in records
            if isinstance(item, dict)
        }
        authority_tags = {
            _RUN_STACK_TAG: ctx.settings.run_id,
            _LOG_CLEANUP_TOKEN_TAG: cleanup_token,
        }
        for stack_name, raw_region in sorted(target_regions.items()):
            region = str(raw_region)
            stack_record = _owned_stack_record(ctx, region, str(stack_name))
            if stack_record is None:
                continue
            live_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
            if (
                live_stack is None
                or str(live_stack.get("status") or "").startswith("DELETE")
                or (live_stack.get("tags") or {}).get(_RUN_STACK_TAG) != ctx.settings.run_id
            ):
                # Destructive authority is never created or completed from a
                # deleted-stack tombstone. Existing pre-destroy records remain usable.
                continue
            # A rolled-back create leaves log groups behind: retained LogGroup
            # resources, Lambda-created default groups, and EKS control-plane
            # groups all survive resource deletion. Their stack resources read
            # DELETE_COMPLETE while the stack itself is still describable, so
            # rollback statuses widen the resource filter to those tombstones.
            rolled_back = str(live_stack.get("status") or "") in {
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
                "UPDATE_ROLLBACK_COMPLETE",
                "UPDATE_ROLLBACK_FAILED",
            }
            allowed_resource_statuses = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            if rolled_back:
                allowed_resource_statuses |= {"DELETE_COMPLETE", "DELETE_FAILED", "DELETE_SKIPPED"}
            cfn = ctx.session.client("cloudformation", region_name=region)
            pages = cfn.get_paginator("list_stack_resources").paginate(
                StackName=stack_record["stack_id"]
            )
            resources = [
                item
                for page in pages
                for item in page.get("StackResourceSummaries", [])
                if str(item.get("ResourceType") or "") in _LOG_GROUP_SOURCE_TYPES
                and item.get("LogicalResourceId")
                and item.get("PhysicalResourceId")
                and str(item.get("ResourceStatus") or "") in allowed_resource_statuses
            ]
            logs = ctx.session.client("logs", region_name=region)
            lambda_client = ctx.session.client("lambda", region_name=region)
            for resource in resources:
                resource_type = str(resource["ResourceType"])
                physical_id = str(resource["PhysicalResourceId"])
                source_service_identity = None
                if resource_type == "AWS::EKS::Cluster":
                    source_service_identity = _eks_cluster_log_authority_identity(
                        ctx,
                        region,
                        physical_id,
                        allow_deleted=rolled_back,
                    )
                names = _derived_log_group_names(resource_type, physical_id)
                if resource_type == "AWS::Lambda::Function":
                    default_name = f"/aws/lambda/{physical_id}"
                    try:
                        function = lambda_client.get_function_configuration(
                            FunctionName=physical_id
                        )
                    except ClientError as exc:
                        error_code = exc.response.get("Error", {}).get("Code", "")
                        if not (rolled_back and error_code == "ResourceNotFoundException"):
                            raise
                        # The rolled-back function is gone; only its default
                        # log group can remain.
                        names = (default_name,)
                    else:
                        configured_name = str(
                            (function.get("LoggingConfig") or {}).get("LogGroup") or default_name
                        )
                        names = (default_name,) if configured_name == default_name else ()
                for name in names:
                    key = (region, name)
                    candidate = {
                        "region": region,
                        "name": name,
                        "stack_name": str(stack_name),
                        "stack_id": stack_record["stack_id"],
                        "source_resource_type": resource_type,
                        "source_logical_id": str(resource["LogicalResourceId"]),
                        "source_physical_id": physical_id,
                        "ownership_authority": "cloudformation-stack-resource-derived",
                        "authority_phase": "pre-destroy",
                        "run_tag": ctx.settings.run_id,
                        "cleanup_token": cleanup_token,
                    }
                    if source_service_identity is not None:
                        candidate["source_service_identity"] = source_service_identity
                    _validated_owned_log_group_identity(ctx, candidate)

                    previous = by_identity.get(key)
                    immutable = tuple(candidate)
                    expected_identity: Mapping[str, Any] | None = None
                    if previous is not None:
                        if any(previous.get(field) != candidate[field] for field in immutable):
                            raise RuntimeError(f"Log-group ownership changed for {region}:{name}")
                        observed = previous.get("observed_identity")
                        if not isinstance(observed, dict):
                            raise RuntimeError(
                                f"Log-group checkpoint identity is malformed for {region}:{name}"
                            )
                        expected_identity = observed

                    initial = _observe_log_group_stability(
                        logs,
                        region,
                        name,
                        expected_identity=expected_identity,
                        expected_tags=authority_tags if previous is not None else None,
                        required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                        required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                    )
                    if previous is not None:
                        _record_log_group_observation(
                            ctx,
                            previous,
                            phase="checkpoint-revalidation",
                            outcome=initial,
                        )
                        if initial["status"] != "present":
                            status = (
                                "replacement-observed-during-checkpoint"
                                if initial["status"] == "replacement"
                                else "checkpoint-generation-not-stable"
                            )
                            _set_log_group_disposition(
                                ctx,
                                previous,
                                status=status,
                                phase="checkpoint-revalidation",
                                outcome=initial,
                            )
                            raise RuntimeError(
                                f"Log-group checkpoint generation is not stable for "
                                f"{region}:{name}: {initial['status']}"
                            )
                        continue

                    if initial["status"] == "absent":
                        if resource_type == "AWS::Logs::LogGroup":
                            _record_log_group_checkpoint_incident(
                                ctx,
                                candidate,
                                phase="checkpoint-explicit-group-absence",
                                outcome=initial,
                            )
                            raise RuntimeError(
                                f"CloudFormation log group is absent before teardown: "
                                f"{region}:{name}"
                            )
                        try:
                            logs.create_log_group(logGroupName=name, tags=authority_tags)
                        except ClientError as exc:
                            if (
                                exc.response.get("Error", {}).get("Code")
                                != "ResourceAlreadyExistsException"
                            ):
                                raise
                            raced = _observe_log_group_stability(
                                logs,
                                region,
                                name,
                                expected_identity=None,
                                expected_tags=None,
                                required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                                required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                            )
                            _record_log_group_checkpoint_incident(
                                ctx,
                                candidate,
                                phase="checkpoint-create-race",
                                outcome=raced,
                            )
                            raise RuntimeError(
                                f"Log group appeared during checkpoint creation; refusing to "
                                f"adopt or tag it: {region}:{name}"
                            ) from exc
                        initial = _observe_log_group_stability(
                            logs,
                            region,
                            name,
                            expected_identity=None,
                            expected_tags=authority_tags,
                            required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                            required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                        )
                    if initial["status"] != "present":
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-initial-stability",
                            outcome=initial,
                        )
                        raise RuntimeError(
                            f"Log group could not be stably checkpointed: "
                            f"{region}:{name}: {initial['status']}"
                        )

                    identity = initial.get("identity")
                    if not isinstance(identity, dict):
                        raise RuntimeError(f"Log group omitted identity: {region}:{name}")
                    if identity["creation_time"] < run_started_ms:
                        raise RuntimeError(
                            f"Log group predates this validation run: {region}:{name}"
                        )
                    tags = identity.get("tags") or {}
                    conflicting_tags = {
                        key: {"expected": value, "observed": tags.get(key)}
                        for key, value in authority_tags.items()
                        if key in tags and tags.get(key) != value
                    }
                    if conflicting_tags:
                        conflict = {
                            **copy.deepcopy(initial),
                            "status": "tag-drift",
                            "tag_drift": conflicting_tags,
                        }
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-authority-tag-conflict",
                            outcome=conflict,
                        )
                        raise RuntimeError(f"Log-group authority tags conflict for {region}:{name}")

                    # This read is intentionally adjacent to tag_resource. A generation
                    # change after the stable reads is fenced before any authority tags
                    # can be applied. The post-tag stable reads catch the unavoidable
                    # service-side TOCTOU without ever adopting that replacement.
                    pre_tag = _observe_log_group_stability(
                        logs,
                        region,
                        name,
                        expected_identity=identity,
                        expected_tags=None,
                        required_present=1,
                        required_absent=1,
                    )
                    if pre_tag["status"] != "present":
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-immediate-pre-tag",
                            outcome=pre_tag,
                        )
                        raise RuntimeError(
                            f"Log-group generation changed immediately before tagging: "
                            f"{region}:{name}: {pre_tag['status']}"
                        )
                    if any(tags.get(key) != value for key, value in authority_tags.items()):
                        logs.tag_resource(resourceArn=identity["arn"], tags=authority_tags)

                    post_tag = _observe_log_group_stability(
                        logs,
                        region,
                        name,
                        expected_identity=identity,
                        expected_tags=authority_tags,
                        required_present=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                        required_absent=_LOG_GROUP_CHECKPOINT_STABLE_OBSERVATIONS,
                    )
                    if post_tag["status"] != "present":
                        _record_log_group_checkpoint_incident(
                            ctx,
                            candidate,
                            phase="checkpoint-post-tag-stability",
                            outcome=post_tag,
                        )
                        raise RuntimeError(
                            f"Log-group generation or authority changed while checkpointing: "
                            f"{region}:{name}: {post_tag['status']}"
                        )
                    final_identity = post_tag.get("identity")
                    if not isinstance(final_identity, dict):
                        raise RuntimeError(
                            f"Log group omitted its post-tag identity: {region}:{name}"
                        )
                    candidate["observed_identity"] = final_identity
                    candidate["checkpoint_observations"] = {
                        "initial": initial,
                        "immediate_pre_tag": pre_tag,
                        "post_tag": post_tag,
                    }
                    candidate["original_generation_disposition"] = {
                        "status": "checkpointed-present",
                        "phase": "pre-destroy",
                        "recorded_at": utc_now(),
                        "original_identity": copy.deepcopy(final_identity),
                    }
                    records.append(candidate)
                    by_identity[key] = candidate
        ctx.persist_callback(ctx.checkpoint)
        return copy.deepcopy(records)
