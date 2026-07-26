"""KMS key identity, retained-key checkpointing, and pending-deletion accounting."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from ..constants import (
    _EKS_KEY_LOGICAL_ID,
    _HEALTHY_STACK_STATUSES,
    _RUN_STACK_TAG,
)
from ..inventory import (
    describe_stack,
)
from ..models import RunContext
from ..ownership.log_groups import (
    _checkpoint_owned_log_groups,
)
from ..ownership.stacks import (
    _owned_stack_record,
    _owned_stacks,
)


def _kms_tags(client: Any, key_id: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    marker: str | None = None
    while True:
        kwargs = {"KeyId": key_id}
        if marker:
            kwargs["Marker"] = marker
        response = client.list_resource_tags(**kwargs)
        tags.update(
            {
                str(tag["TagKey"]): str(tag.get("TagValue") or "")
                for tag in response.get("Tags", [])
                if tag.get("TagKey") is not None
            }
        )
        marker = response.get("NextMarker") if response.get("Truncated") else None
        if not marker:
            return tags


def _validated_owned_kms_identity(
    ctx: RunContext,
    record: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    """Validate immutable stack-resource authority for one run-owned KMS key."""
    region = str(record.get("region") or "")
    key_id = str(record.get("key_id") or "")
    arn = str(record.get("arn") or "")
    stack_name = str(record.get("stack_name") or "")
    stack_id = str(record.get("stack_id") or "")
    logical_id = str(record.get("logical_id") or "")
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict) or str(target_regions.get(stack_name) or "") != region:
        raise RuntimeError(f"KMS checkpoint target stack is invalid for {arn or key_id}")
    partition = ctx.session.get_partition_for_region(region)
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for KMS key in {region}")
    expected_arn = f"arn:{partition}:kms:{region}:{ctx.settings.expected_account}:key/{key_id}"
    expected_stack_prefix = (
        f"arn:{partition}:cloudformation:{region}:{ctx.settings.expected_account}:"
        f"stack/{stack_name}/"
    )
    owned_stack_record = _owned_stack_record(ctx, region, stack_name)
    expected_stack_id = str((owned_stack_record or {}).get("stack_id") or "")
    if not key_id or arn != expected_arn:
        raise RuntimeError(f"KMS checkpoint ARN is invalid for {arn or key_id}")
    if (
        not stack_name
        or not expected_stack_id.startswith(expected_stack_prefix)
        or stack_id != expected_stack_id
        or (owned_stack_record or {}).get("run_tag") != ctx.settings.run_id
    ):
        raise RuntimeError(f"KMS checkpoint stack identity is invalid for {arn}")
    if (
        record.get("ownership_authority") != "cloudformation-stack-resource"
        or not logical_id
        or record.get("run_tag") != ctx.settings.run_id
    ):
        raise RuntimeError(f"KMS checkpoint authority is incomplete for {arn}")

    retained_identity = (
        stack_name == f"{ctx.config.project_name}-{region}" and logical_id == _EKS_KEY_LOGICAL_ID
    )
    cleanup_policy = str(record.get("cleanup_policy") or "")
    if not cleanup_policy and retained_identity:
        cleanup_policy = "harness-schedule"
    if cleanup_policy == "harness-schedule":
        if not retained_identity:
            raise RuntimeError(f"Retained KMS checkpoint identity is invalid for {arn}")
    elif cleanup_policy == "cloudformation-delete":
        if retained_identity:
            raise RuntimeError(f"Retained EKS key cannot use CloudFormation cleanup: {arn}")
    else:
        raise RuntimeError(f"KMS checkpoint cleanup policy is invalid for {arn}")
    return region, key_id, arn, cleanup_policy


def _validated_retained_kms_identity(
    ctx: RunContext,
    record: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Validate exact retained-EKS authority before harness-scheduled deletion."""
    region, key_id, arn, cleanup_policy = _validated_owned_kms_identity(ctx, record)
    if cleanup_policy != "harness-schedule":
        raise RuntimeError(f"KMS key is not harness-retained: {arn}")
    return region, key_id, arn


def _checkpoint_retained_kms_keys(ctx: RunContext) -> list[dict[str, Any]]:
    """Capture every exact stack-owned KMS key plus teardown log-group candidates."""
    _checkpoint_owned_log_groups(ctx)
    owned_stacks = _owned_stacks(ctx)
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict):
        raise RuntimeError("Checkpoint target_stack_regions must be an object")
    with ctx.state_lock:
        records = ctx.checkpoint.state.setdefault("owned_kms_keys", [])
        if not isinstance(records, list):
            raise RuntimeError("Checkpoint owned_kms_keys must be a list")
        by_arn = {str(item.get("arn") or ""): item for item in records if isinstance(item, dict)}

        for stack_name, raw_region in sorted(target_regions.items()):
            region = str(raw_region)
            stack_record = owned_stacks.get(region, {}).get(str(stack_name))
            if stack_record is None:
                continue
            live_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
            live_source_authority = (
                live_stack is not None
                and not str(live_stack.get("status") or "").startswith("DELETE")
                and live_stack.get("stack_id") == stack_record["stack_id"]
                and (live_stack.get("tags") or {}).get(_RUN_STACK_TAG) == ctx.settings.run_id
            )
            cfn = ctx.session.client("cloudformation", region_name=region)
            try:
                pages = cfn.get_paginator("list_stack_resources").paginate(
                    StackName=stack_record["stack_id"]
                )
                matching_resources = [
                    item
                    for page in pages
                    for item in page.get("StackResourceSummaries", [])
                    if item.get("ResourceType") == "AWS::KMS::Key"
                    and item.get("LogicalResourceId")
                    and item.get("PhysicalResourceId")
                ]
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code") == "ValidationError"
                    and live_stack is None
                ):
                    continue
                raise
            retained_resources = [
                item
                for item in matching_resources
                if str(stack_name) == f"{ctx.config.project_name}-{region}"
                and str(item.get("LogicalResourceId") or "") == _EKS_KEY_LOGICAL_ID
            ]
            if (
                live_stack is not None
                and live_stack.get("status") in _HEALTHY_STACK_STATUSES
                and str(stack_name) == f"{ctx.config.project_name}-{region}"
                and len(retained_resources) != 1
            ):
                raise RuntimeError(
                    f"Expected one retained EKS KMS key in {stack_name}; found "
                    f"{len(retained_resources)}"
                )

            for resource in matching_resources:
                key_id = str(resource["PhysicalResourceId"])
                logical_id = str(resource["LogicalResourceId"])
                partition = ctx.session.get_partition_for_region(region)
                if not partition:
                    raise RuntimeError(f"Could not resolve AWS partition for KMS key in {region}")
                derived_arn = (
                    f"arn:{partition}:kms:{region}:{ctx.settings.expected_account}:key/{key_id}"
                )
                previous = by_arn.get(derived_arn)
                if previous is None and not live_source_authority:
                    # Deleted-stack tombstones may reconcile exact records that
                    # were persisted pre-destroy, but can never create authority.
                    continue
                kms = ctx.session.client("kms", region_name=region)
                try:
                    metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "NotFoundException":
                        raise
                    if previous is not None:
                        previous["scheduled"] = True
                        previous["deleted"] = True
                    continue
                arn = str(metadata.get("Arn") or "")
                tags = _kms_tags(kms, key_id)
                if tags.get(_RUN_STACK_TAG) != ctx.settings.run_id:
                    raise RuntimeError(
                        f"KMS key {arn or key_id} lacks the exact live-validation run tag"
                    )
                cleanup_policy = (
                    "harness-schedule"
                    if str(stack_name) == f"{ctx.config.project_name}-{region}"
                    and logical_id == _EKS_KEY_LOGICAL_ID
                    else "cloudformation-delete"
                )
                deletion_date = metadata.get("DeletionDate")
                state = str(metadata.get("KeyState") or "")
                candidate = {
                    "region": region,
                    "key_id": key_id,
                    "arn": arn,
                    "stack_name": str(stack_name),
                    "stack_id": stack_record["stack_id"],
                    "logical_id": logical_id,
                    "ownership_authority": "cloudformation-stack-resource",
                    "cleanup_policy": cleanup_policy,
                    "run_tag": ctx.settings.run_id,
                    "scheduled": state == "PendingDeletion",
                    "deletion_date": (
                        deletion_date.isoformat() if deletion_date is not None else None
                    ),
                }
                _validated_owned_kms_identity(ctx, candidate)
                if arn != derived_arn:
                    raise RuntimeError(f"KMS returned an unexpected ARN for {key_id}: {arn}")
                previous = by_arn.get(arn)
                if previous is not None:
                    previous.setdefault("cleanup_policy", cleanup_policy)
                    for key in (
                        "region",
                        "key_id",
                        "arn",
                        "stack_name",
                        "stack_id",
                        "logical_id",
                        "ownership_authority",
                        "cleanup_policy",
                        "run_tag",
                    ):
                        if previous.get(key) != candidate[key]:
                            raise RuntimeError(f"KMS ownership changed for {arn}: {key}")
                    if candidate["scheduled"]:
                        previous["scheduled"] = True
                        previous["deletion_date"] = candidate["deletion_date"]
                    continue
                if not arn:
                    raise RuntimeError(f"KMS key {key_id} omitted its ARN")
                refreshed_stack = describe_stack(ctx.session, region, stack_record["stack_id"])
                if not (
                    refreshed_stack is not None
                    and not str(refreshed_stack.get("status") or "").startswith("DELETE")
                    and refreshed_stack.get("stack_id") == stack_record["stack_id"]
                    and (refreshed_stack.get("tags") or {}).get(_RUN_STACK_TAG)
                    == ctx.settings.run_id
                ):
                    continue
                records.append(candidate)
                by_arn[arn] = candidate
                ctx.persist_callback(ctx.checkpoint)
        ctx.persist_callback(ctx.checkpoint)
        return copy.deepcopy(records)


def _strip_expected_pending_kms(
    ctx: RunContext,
    project_inventory: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    inventory = copy.deepcopy(project_inventory)
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    for record in ctx.checkpoint.state.get("owned_kms_keys", []):
        region, key_id, arn, cleanup_policy = _validated_owned_kms_identity(ctx, record)
        identity = (region, arn)
        if not record.get("scheduled"):
            raise RuntimeError(f"Owned KMS key was not scheduled for deletion: {arn}")
        if identity in expected:
            raise RuntimeError(f"Duplicate KMS checkpoint identity: {region}:{arn}")

        kms = ctx.session.client("kms", region_name=region)
        try:
            metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NotFoundException":
                raise
            evidence = {
                "region": region,
                "key_id": key_id,
                "arn": arn,
                "state": "Deleted",
                "already_absent": True,
                "stack_id": record["stack_id"],
                "logical_id": record["logical_id"],
                "ownership_authority": record["ownership_authority"],
                "cleanup_policy": cleanup_policy,
                "run_tag": record["run_tag"],
            }
        else:
            if metadata.get("Arn") != arn:
                raise RuntimeError(f"KMS key ARN changed for {key_id}")
            state = str(metadata.get("KeyState") or "")
            if state != "PendingDeletion":
                raise RuntimeError(
                    f"Expected {cleanup_policy} KMS key {arn} to be PendingDeletion; found {state}"
                )
            tags = _kms_tags(kms, key_id)
            if tags.get(_RUN_STACK_TAG) != record["run_tag"]:
                raise RuntimeError(f"KMS run ownership changed for {arn}")
            deletion_date = metadata.get("DeletionDate")
            observed_deletion_date = (
                deletion_date.isoformat() if deletion_date is not None else None
            )
            if not observed_deletion_date or observed_deletion_date != record.get("deletion_date"):
                raise RuntimeError(f"KMS deletion date changed for {arn}")
            evidence = {
                "region": region,
                "key_id": key_id,
                "arn": arn,
                "state": state,
                "description": str(metadata.get("Description") or ""),
                "deletion_date": observed_deletion_date,
                "tags": tags,
                "stack_id": record["stack_id"],
                "logical_id": record["logical_id"],
                "ownership_authority": record["ownership_authority"],
                "cleanup_policy": cleanup_policy,
                "run_tag": record["run_tag"],
            }
        expected[identity] = evidence
        accepted.append(evidence)

    for region, resources in list(inventory.get("regional", {}).items()):
        resources["kms_keys"] = [
            key
            for key in resources.get("kms_keys", [])
            if (region, str(key.get("arn") or "")) not in expected
        ]
        if not any(resources.values()):
            inventory["regional"].pop(region)
    return inventory, accepted
