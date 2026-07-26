"""Schedule and account for intentionally retained resources."""

from __future__ import annotations

import copy
import json
from typing import Any

from botocore.exceptions import ClientError

from ..cleanup.ecr import _cleanup_new_ecr_images, _cleanup_new_ecr_repositories
from ..cleanup.log_groups import (
    _cleanup_owned_log_groups,
)
from ..constants import (
    _KMS_PENDING_WINDOW_DAYS,
    _RUN_STACK_TAG,
    _LogGroupCleanupError,
)
from ..models import RunContext, utc_now
from ..ownership.kms import (
    _kms_tags,
    _validated_owned_kms_identity,
)


def _schedule_retained_kms_keys(ctx: RunContext) -> dict[str, Any]:
    records = ctx.checkpoint.state.get("owned_kms_keys", [])
    retained_records = [
        record
        for record in records
        if _validated_owned_kms_identity(ctx, record)[3] == "harness-schedule"
    ]
    if retained_records and not ctx.settings.confirm_kms_key_deletion:
        raise RuntimeError("Retained KMS keys exist but this identity did not confirm key deletion")
    results: list[dict[str, Any]] = []
    for record in records:
        region, key_id, arn, cleanup_policy = _validated_owned_kms_identity(ctx, record)
        record.setdefault("cleanup_policy", cleanup_policy)
        kms = ctx.session.client("kms", region_name=region)
        try:
            metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NotFoundException":
                record["scheduled"] = True
                record["deleted"] = True
                results.append(
                    {
                        "arn": arn,
                        "cleanup_policy": cleanup_policy,
                        "already_absent": True,
                    }
                )
                ctx.persist()
                continue
            raise
        if metadata.get("Arn") != arn:
            raise RuntimeError(f"KMS key ARN changed for {key_id}")
        tags = _kms_tags(kms, key_id)
        if tags.get(_RUN_STACK_TAG) != record["run_tag"]:
            raise RuntimeError(f"KMS run ownership changed for {arn}")

        state = str(metadata.get("KeyState") or "")
        if state != "PendingDeletion" and cleanup_policy == "harness-schedule":
            if state not in {"Enabled", "Disabled"}:
                raise RuntimeError(f"KMS key {arn} is {state}; refusing to schedule deletion")
            kms.schedule_key_deletion(
                KeyId=key_id,
                PendingWindowInDays=_KMS_PENDING_WINDOW_DAYS,
            )
            metadata = kms.describe_key(KeyId=key_id).get("KeyMetadata", {})
            state = str(metadata.get("KeyState") or "")
        if state != "PendingDeletion":
            raise RuntimeError(
                f"Expected {cleanup_policy} KMS key {arn} to be PendingDeletion; found {state}"
            )
        deletion_date = metadata.get("DeletionDate")
        record["scheduled"] = True
        record["deletion_date"] = deletion_date.isoformat() if deletion_date is not None else None
        if not record["deletion_date"]:
            raise RuntimeError(f"Pending-deletion KMS key omitted its deletion date: {arn}")
        results.append(
            {
                "arn": arn,
                "state": state,
                "cleanup_policy": cleanup_policy,
                "deletion_date": record["deletion_date"],
            }
        )
        ctx.persist()
    return {
        "keys": results,
        "deletion_window": {
            "harness_schedule_days": _KMS_PENDING_WINDOW_DAYS,
            "cloudformation_delete": "observed per key deletion_date",
        },
    }


def _retained_resource_cleanup(ctx: RunContext) -> dict[str, Any]:
    result: dict[str, Any] = {"started_at": utc_now(), "errors": []}
    try:
        result["cloudwatch_logs"] = _cleanup_owned_log_groups(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        if isinstance(exc, _LogGroupCleanupError):
            result["cloudwatch_logs"] = copy.deepcopy(exc.details)
        result["errors"].append(
            {"phase": "cloudwatch-logs", "error": f"{type(exc).__name__}: {exc}"}
        )
    try:
        result["ecr_images"] = _cleanup_new_ecr_images(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        result["errors"].append({"phase": "ecr-images", "error": f"{type(exc).__name__}: {exc}"})
    try:
        result["ecr_repositories"] = _cleanup_new_ecr_repositories(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        result["errors"].append(
            {"phase": "ecr-repositories", "error": f"{type(exc).__name__}: {exc}"}
        )
    try:
        result["kms"] = _schedule_retained_kms_keys(ctx)
    except Exception as exc:  # noqa: BLE001 - preserve partial evidence
        result["errors"].append({"phase": "kms", "error": f"{type(exc).__name__}: {exc}"})
    result["ended_at"] = utc_now()
    ctx.checkpoint.state.setdefault("retained_cleanup_attempts", []).append(result)
    ctx.persist()
    if result["errors"]:
        raise RuntimeError(
            "Retained resource cleanup failed: " + json.dumps(result["errors"], sort_keys=True)
        )
    return result
