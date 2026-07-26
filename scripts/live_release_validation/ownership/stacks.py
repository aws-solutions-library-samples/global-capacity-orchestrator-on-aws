"""Durable CloudFormation stack ownership and change-set authority."""

from __future__ import annotations

import copy
import json
from typing import Any

from ..constants import (
    _RUN_STACK_TAG,
)
from ..inventory import (
    collect_project_stacks,
    describe_stack,
)
from ..models import RunContext


def _owned_stacks(ctx: RunContext) -> dict[str, dict[str, dict[str, Any]]]:
    """Return region-qualified stack ownership records for this schema."""
    owned = ctx.checkpoint.state.setdefault("owned_stacks", {})
    if not isinstance(owned, dict):
        raise RuntimeError("Checkpoint owned_stacks must be an object")
    for region, records in owned.items():
        if not isinstance(records, dict):
            raise RuntimeError(f"Checkpoint stack ownership for {region} is malformed")
        for stack_name, record in records.items():
            if not isinstance(record, dict):
                raise RuntimeError(
                    f"Checkpoint stack ownership for {region}:{stack_name} is malformed"
                )
    return owned


def _owned_stack_record(
    ctx: RunContext,
    region: str,
    stack_name: str,
) -> dict[str, Any] | None:
    return _owned_stacks(ctx).get(region, {}).get(stack_name)


def _require_prepared_stack_authority(
    record: dict[str, Any],
    *,
    region: str,
    stack_name: str,
) -> None:
    if (
        record.get("authority") != "prepared-change-set"
        or not record.get("change_set_id")
        or record.get("change_set_type") not in {"CREATE", "UPDATE"}
    ):
        raise RuntimeError(
            f"Stack {region}:{stack_name} lacks persisted prepared-change-set authority"
        )


def _prepared_change_set_authority(
    ctx: RunContext,
) -> dict[str, dict[str, dict[str, str]]]:
    """Return validated per-target preparation history, including legacy checkpoints."""
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict):
        raise RuntimeError("Checkpoint target_stack_regions must be an object")

    authority: dict[str, dict[str, dict[str, str]]] = {}
    for stack_name, region_value in target_regions.items():
        region = str(region_value)
        record = _owned_stack_record(ctx, region, stack_name)
        prepared_records: dict[str, dict[str, str]] = {}
        if record is not None:
            _require_prepared_stack_authority(
                record,
                region=region,
                stack_name=stack_name,
            )
            raw_records = record.get("prepared_change_sets", {})
            if not isinstance(raw_records, dict):
                raise RuntimeError(
                    f"Prepared change-set history for {region}:{stack_name} is malformed"
                )
            for change_set_id, raw_prepared in raw_records.items():
                if not isinstance(change_set_id, str) or not isinstance(raw_prepared, dict):
                    raise RuntimeError(
                        f"Prepared change-set history for {region}:{stack_name} is malformed"
                    )
                prepared = {
                    "change_set_id": str(raw_prepared.get("change_set_id") or ""),
                    "stack_id": str(raw_prepared.get("stack_id") or ""),
                    "change_set_type": str(raw_prepared.get("change_set_type") or ""),
                }
                if (
                    prepared["change_set_id"] != change_set_id
                    or prepared["stack_id"] != str(record.get("stack_id") or "")
                    or prepared["change_set_type"] not in {"CREATE", "UPDATE"}
                ):
                    raise RuntimeError(
                        f"Prepared change-set history for {region}:{stack_name} is inconsistent"
                    )
                prepared_records[change_set_id] = prepared

            # Checkpoints written before per-change-set history retained only
            # the latest preparation. Preserve that exact authority on resume.
            legacy_change_set_id = str(record.get("change_set_id") or "")
            if legacy_change_set_id and legacy_change_set_id not in prepared_records:
                prepared_records[legacy_change_set_id] = {
                    "change_set_id": legacy_change_set_id,
                    "stack_id": str(record.get("stack_id") or ""),
                    "change_set_type": str(record.get("change_set_type") or ""),
                }
        authority[stack_name] = prepared_records
    return authority


def _record_prepared_stack_identity(
    ctx: RunContext,
    stack_name: str,
    region: str,
    stack_id: str,
    change_set_id: str,
    change_set_type: str,
) -> None:
    """Persist causal change-set authority before CloudFormation execution."""
    if not stack_id or not change_set_id or change_set_type not in {"CREATE", "UPDATE"}:
        raise RuntimeError(f"Invalid prepared change-set identity for {region}:{stack_name}")
    with ctx.state_lock:
        records = _owned_stacks(ctx).setdefault(region, {})
        previous = records.get(stack_name)
        core = {"name": stack_name, "region": region, "stack_id": stack_id}
        if previous is not None:
            _require_prepared_stack_authority(
                previous,
                region=region,
                stack_name=stack_name,
            )
            if any(previous.get(key) != value for key, value in core.items()):
                raise RuntimeError(
                    f"Prepared stack identity changed for {region}:{stack_name}; refusing adoption"
                )
        previous_prepared = (previous or {}).get("prepared_change_sets", {})
        if not isinstance(previous_prepared, dict):
            raise RuntimeError(
                f"Prepared change-set history for {region}:{stack_name} is malformed"
            )
        prepared_records = copy.deepcopy(previous_prepared)
        if previous is not None:
            legacy_change_set_id = str(previous.get("change_set_id") or "")
            legacy_record = {
                "change_set_id": legacy_change_set_id,
                "stack_id": stack_id,
                "change_set_type": str(previous.get("change_set_type") or ""),
            }
            persisted_legacy = prepared_records.get(legacy_change_set_id)
            if persisted_legacy is not None and persisted_legacy != legacy_record:
                raise RuntimeError(
                    f"Prepared change-set history for {region}:{stack_name} is inconsistent"
                )
            prepared_records[legacy_change_set_id] = legacy_record
        prepared_record = {
            "change_set_id": change_set_id,
            "stack_id": stack_id,
            "change_set_type": change_set_type,
        }
        existing_prepared = prepared_records.get(change_set_id)
        if existing_prepared is not None and existing_prepared != prepared_record:
            raise RuntimeError(
                f"Prepared change-set identity changed for {region}:{stack_name}; refusing adoption"
            )
        prepared_records[change_set_id] = prepared_record
        records[stack_name] = {
            **(previous or {}),
            **core,
            "run_tag": ctx.settings.run_id,
            "authority": "prepared-change-set",
            "change_set_id": change_set_id,
            "change_set_type": change_set_type,
            "prepared_change_sets": prepared_records,
        }
        ctx.persist_callback(ctx.checkpoint)


def _record_stack_identity(
    ctx: RunContext,
    stack_name: str,
    region: str,
    stack: dict[str, Any],
) -> dict[str, Any]:
    stack_id = str(stack.get("stack_id") or "")
    run_tag = str((stack.get("tags") or {}).get(_RUN_STACK_TAG) or "")
    if stack.get("name") != stack_name or not stack_id:
        raise RuntimeError(f"CloudFormation returned an invalid identity for {region}:{stack_name}")
    if run_tag != ctx.settings.run_id:
        raise RuntimeError(
            f"Stack {region}:{stack_name} is not tagged for run {ctx.settings.run_id!r}"
        )

    with ctx.state_lock:
        records = _owned_stacks(ctx).get(region)
        if records is None:
            raise RuntimeError(
                f"Stack {region}:{stack_name} was observed without prepared-change-set authority"
            )
        previous = records.get(stack_name)
        if previous is None:
            raise RuntimeError(
                f"Stack {region}:{stack_name} was observed without prepared-change-set authority"
            )
        _require_prepared_stack_authority(
            previous,
            region=region,
            stack_name=stack_name,
        )
        core = {
            "name": stack_name,
            "region": region,
            "stack_id": stack_id,
            "run_tag": run_tag,
        }
        if any(previous.get(key) != value for key, value in core.items()):
            raise RuntimeError(
                f"Stack identity changed for {region}:{stack_name}; refusing name-based adoption"
            )
        candidate = {**previous, **core}
        records[stack_name] = candidate
        ctx.persist_callback(ctx.checkpoint)
    return candidate


def _reconcile_stack_ownership(ctx: RunContext) -> dict[str, Any]:
    """Verify every live project stack by ARN and exact run tag."""
    target_regions = ctx.checkpoint.state.get("target_stack_regions") or {}
    enabled_regions = ctx.checkpoint.state.get("enabled_regions") or []
    if not target_regions or not enabled_regions:
        raise RuntimeError("Checkpoint lacks target stack Regions or enabled Regions")

    project_stacks = collect_project_stacks(
        ctx.session,
        enabled_regions,
        ctx.config.project_name,
    )
    expected_targets = {
        (str(region), str(stack_name)) for stack_name, region in target_regions.items()
    }
    unexpected = {
        region: [
            item for item in stacks if (str(region), str(item["name"])) not in expected_targets
        ]
        for region, stacks in project_stacks.items()
        if any((str(region), str(item["name"])) not in expected_targets for item in stacks)
    }
    if unexpected:
        raise RuntimeError(
            "Project stacks outside the checkpoint target set were found: "
            + json.dumps(unexpected, sort_keys=True)
        )

    present: dict[str, dict[str, Any]] = {}
    for stack_name, expected_region in target_regions.items():
        region = str(expected_region)
        stack = describe_stack(ctx.session, region, stack_name)
        if stack is None or stack.get("status") == "DELETE_COMPLETE":
            continue
        present.setdefault(region, {})[stack_name] = _record_stack_identity(
            ctx, stack_name, region, stack
        )

    checkpointed = _owned_stacks(ctx)
    for region, records in checkpointed.items():
        for stack_name, record in records.items():
            if target_regions.get(stack_name) != region:
                raise RuntimeError(
                    f"Checkpoint owns unexpected stack identity {region}:{stack_name}"
                )
            if str(record.get("region")) != region:
                raise RuntimeError(f"Checkpoint Region changed for stack {region}:{stack_name}")
    return present


def _authorize_owned_stack(
    ctx: RunContext,
    stack_name: str,
    region: str,
    stack_id: str,
) -> None:
    """Revalidate checkpoint ARN and run tag at a destructive boundary."""
    record = _owned_stack_record(ctx, region, stack_name)
    if record is None:
        raise RuntimeError(f"No checkpointed ownership exists for {region}:{stack_name}")
    _require_prepared_stack_authority(
        record,
        region=region,
        stack_name=stack_name,
    )
    if str(record.get("region")) != region or str(record.get("stack_id")) != stack_id:
        raise RuntimeError(f"Checkpoint identity changed for {region}:{stack_name}")
    live = describe_stack(ctx.session, region, stack_id)
    if live is None:
        raise RuntimeError(f"Checkpointed stack disappeared before authorization: {stack_id}")
    if live.get("name") != stack_name or live.get("stack_id") != stack_id:
        raise RuntimeError(f"CloudFormation identity changed for {region}:{stack_name}")
    if (live.get("tags") or {}).get(_RUN_STACK_TAG) != ctx.settings.run_id:
        raise RuntimeError(f"Run ownership changed for {region}:{stack_name}")


def _resolve_target_stack(
    ctx: RunContext,
    *,
    region: str,
    stack_name: str,
    expected_stack_id: str,
) -> dict[str, Any]:
    """Resolve live/absent/tombstone/replacement state for one exact target."""
    exact = describe_stack(ctx.session, region, expected_stack_id) if expected_stack_id else None
    if exact is not None and exact.get("status") != "DELETE_COMPLETE":
        if exact.get("name") != stack_name or exact.get("stack_id") != expected_stack_id:
            raise RuntimeError(f"Exact stack identity changed for {region}:{stack_name}")
        return {"state": "live", "stack": exact}

    by_name = describe_stack(ctx.session, region, stack_name)
    if by_name is None or by_name.get("status") == "DELETE_COMPLETE":
        return {
            "state": "absent",
            "tombstone": exact if exact and exact.get("status") == "DELETE_COMPLETE" else None,
        }
    actual_id = str(by_name.get("stack_id") or "")
    if expected_stack_id and actual_id != expected_stack_id:
        return {"state": "replacement", "stack": by_name}
    if not expected_stack_id:
        return {"state": "uncheckpointed", "stack": by_name}
    return {"state": "live", "stack": by_name}


def _verify_target_stack_absence(ctx: RunContext) -> dict[str, Any]:
    """Prove every target is absent while surfacing same-name replacements."""
    targets = ctx.checkpoint.state.get("target_stack_regions") or {}
    if not targets:
        raise RuntimeError("Checkpoint lacks target stack Regions for absence verification")
    residual: list[dict[str, Any]] = []
    absent: list[dict[str, str]] = []
    for stack_name, raw_region in targets.items():
        region = str(raw_region)
        record = _owned_stack_record(ctx, region, stack_name)
        expected_id = str((record or {}).get("stack_id") or "")
        resolution = _resolve_target_stack(
            ctx,
            region=region,
            stack_name=stack_name,
            expected_stack_id=expected_id,
        )
        if resolution["state"] == "absent":
            absent.append({"name": stack_name, "region": region, "stack_id": expected_id})
            continue
        stack = resolution["stack"]
        residual.append(
            {
                "name": stack_name,
                "region": region,
                "expected_stack_id": expected_id or None,
                "actual_stack_id": stack.get("stack_id"),
                "status": stack.get("status"),
                "kind": resolution["state"],
            }
        )
    return {"all_absent": not residual, "absent": absent, "residual": residual}
