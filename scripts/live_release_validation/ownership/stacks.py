"""Durable CloudFormation stack ownership and change-set authority.

A stack is normally owned through *prepared-change-set* authority: the harness
prepared the change set itself and checkpointed its identity before
CloudFormation executed anything. The upgrade harness cannot do that, because
the ``gco`` it validates deploys the stacks from a separate process. For that
harness alone (``RunSettings.allows_run_tag_adoption``) a stack can instead be
*adopted by run tag*: it carries this run's exact ``GcoLiveValidationRun`` tag,
its name is a checkpointed target, and CloudFormation reports it created after
the adopting phase began. Every destructive boundary still re-checks the exact
stack ID and tag, as it does for prepared stacks.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from ..constants import (
    _RUN_STACK_TAG,
)
from ..inventory import (
    collect_project_stacks,
    describe_stack,
)
from ..models import RunContext, utc_now

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-29T11:35:23Z
# Generated from Git commit: a081fbf678ca3f3fa3f7edb74687de923efe6657
# Flowchart(s) generated from this file:
#   * ``_adopt_run_tagged_stacks`` -> ``diagrams/code_diagrams/scripts/live_release_validation/ownership/stacks._adopt_run_tagged_stacks.html``
#     (PNG: ``diagrams/code_diagrams/scripts/live_release_validation/ownership/stacks._adopt_run_tagged_stacks.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


#: ``authority`` of a stack record adopted by run tag rather than prepared here.
_RUN_TAG_ADOPTION_AUTHORITY = "run-tag-adoption"

#: The adoption window starts at a server ``Date`` header, which has one-second
#: resolution; CloudFormation reports creation times to the millisecond.
_ADOPTION_CLOCK_RESOLUTION = timedelta(seconds=1)


def _run_tag_adoption_allowed(ctx: RunContext) -> bool:
    """Only settings that opt in (the upgrade harness) may adopt stacks by run tag."""
    return getattr(ctx.settings, "allows_run_tag_adoption", False) is True


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


def _is_adopted(record: dict[str, Any] | None) -> bool:
    return record is not None and record.get("authority") == _RUN_TAG_ADOPTION_AUTHORITY


def _require_stack_authority(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    region: str,
    stack_name: str,
) -> None:
    """Require prepared-change-set authority, or run-tag adoption where it is allowed."""
    if not _is_adopted(record):
        _require_prepared_stack_authority(record, region=region, stack_name=stack_name)
        return
    if not _run_tag_adoption_allowed(ctx):
        raise RuntimeError(
            f"Stack {region}:{stack_name} carries run-tag adoption authority, which only the "
            "upgrade validation harness may honor"
        )
    adoption = record.get("adoption")
    if (
        not isinstance(adoption, dict)
        or not adoption.get("phase")
        or not adoption.get("window_started_at")
        or not adoption.get("creation_time")
    ):
        raise RuntimeError(f"Stack {region}:{stack_name} lacks persisted run-tag adoption evidence")


def _owned_stack_ids(ctx: RunContext, region: str, stack_name: str) -> frozenset[str]:
    """Every stack ID this run has owned under one name.

    That is the current generation plus, for an adopted stack, each generation
    an in-place upgrade replaced. A retained resource checkpointed from a
    replaced generation (its EKS key, its log groups) stays attributable to
    the run after that generation is gone.
    """
    record = _owned_stack_record(ctx, region, stack_name)
    if record is None:
        return frozenset()
    identities = {str(record.get("stack_id") or "")}
    generations = record.get("replaced_generations", [])
    if not isinstance(generations, list) or (generations and not _is_adopted(record)):
        raise RuntimeError(f"Replaced stack generations for {region}:{stack_name} are malformed")
    if generations and not _run_tag_adoption_allowed(ctx):
        raise RuntimeError(
            f"Stack {region}:{stack_name} records replaced generations, which only the "
            "upgrade validation harness may honor"
        )
    for generation in generations:
        stack_id = generation.get("stack_id") if isinstance(generation, dict) else None
        if not isinstance(stack_id, str) or not stack_id:
            raise RuntimeError(
                f"Replaced stack generations for {region}:{stack_name} are malformed"
            )
        identities.add(stack_id)
    identities.discard("")
    return frozenset(identities)


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
            _require_stack_authority(
                ctx,
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
            _require_stack_authority(
                ctx,
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
        # Prepared records always carry the latest change set; an adopted record
        # carries none until the harness prepares one on the stack itself.
        if previous is not None and previous.get("change_set_id"):
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
            # A change set the harness prepares on an adopted stack extends its
            # history; the adoption stays the record's basis of ownership.
            "authority": (
                _RUN_TAG_ADOPTION_AUTHORITY if _is_adopted(previous) else "prepared-change-set"
            ),
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
        _require_stack_authority(
            ctx,
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
    _require_stack_authority(
        ctx,
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


def _stack_creation_time(ctx: RunContext, region: str, stack_id: str) -> datetime:
    """CloudFormation's creation time for one exact stack ID."""
    client = ctx.session.client("cloudformation", region_name=region)
    stacks = client.describe_stacks(StackName=stack_id).get("Stacks", [])
    created = stacks[0].get("CreationTime") if len(stacks) == 1 else None
    if not isinstance(created, datetime) or created.tzinfo is None:
        raise RuntimeError(f"CloudFormation omitted the creation time of {stack_id}")
    return created.astimezone(UTC)


def _adopt_run_tagged_stacks(
    ctx: RunContext,
    *,
    phase: str,
    window_started_at: datetime,
    replaceable: Collection[str] = (),
) -> dict[str, Any]:
    """Adopt the target stacks a ``gco`` subprocess created or replaced for this run.

    The deliberate relaxation of prepared-change-set authority, open only to a
    harness whose settings opt in. A live target stack is adopted when it
    carries this run's exact tag and CloudFormation reports it created no
    earlier than ``window_started_at``, the server time checkpointed before
    the phase's command started. A stack this run already owns under the same
    ID is left as it is. One owned under a different ID is a replacement,
    accepted only for a ``replaceable`` name: the previous generation must be
    deleted, and it is kept in ``replaced_generations`` so what it retained
    (its EKS key, its log groups) stays attributable to the run. Anything else
    (a missing tag, an earlier creation time, a replaced control-plane stack)
    fails closed and adopts nothing further.
    """
    if not _run_tag_adoption_allowed(ctx):
        raise RuntimeError("Run-tag stack adoption is not enabled for this harness")
    if window_started_at.tzinfo is None:
        raise ValueError("The adoption window must start at a timezone-aware time")
    target_regions = ctx.checkpoint.state.get("target_stack_regions")
    if not isinstance(target_regions, dict) or not target_regions:
        raise RuntimeError("Checkpoint lacks target stack Regions for run-tag adoption")
    unknown = sorted(set(replaceable) - set(target_regions))
    if unknown:
        raise RuntimeError("Replaceable stacks are not targets: " + ", ".join(unknown))
    window = window_started_at.astimezone(UTC)
    evidence: dict[str, Any] = {
        "phase": phase,
        "window_started_at": window.isoformat(),
        "adopted": [],
        "replaced": [],
        "unchanged": [],
        "absent": [],
    }
    for stack_name, raw_region in sorted(target_regions.items()):
        region = str(raw_region)
        record = _owned_stack_record(ctx, region, stack_name)
        live = describe_stack(ctx.session, region, stack_name)
        if live is None or live.get("status") == "DELETE_COMPLETE":
            evidence["absent"].append(
                {
                    "name": stack_name,
                    "region": region,
                    "owned_stack_id": (record or {}).get("stack_id"),
                }
            )
            continue
        partition = ctx.session.get_partition_for_region(region)
        if not partition:
            raise RuntimeError(f"Could not resolve AWS partition for {region}")
        stack_id = str(live.get("stack_id") or "")
        prefix = (
            f"arn:{partition}:cloudformation:{region}:{ctx.settings.expected_account}:"
            f"stack/{stack_name}/"
        )
        if live.get("name") != stack_name or not stack_id.startswith(prefix):
            raise RuntimeError(
                f"CloudFormation returned an invalid identity for {region}:{stack_name}"
            )
        if (live.get("tags") or {}).get(_RUN_STACK_TAG) != ctx.settings.run_id:
            raise RuntimeError(
                f"Stack {region}:{stack_name} is not tagged for run {ctx.settings.run_id!r}; "
                "refusing adoption"
            )
        summary = {"name": stack_name, "region": region, "stack_id": stack_id}
        if record is not None and record.get("stack_id") == stack_id:
            _require_stack_authority(ctx, record, region=region, stack_name=stack_name)
            evidence["unchanged"].append({**summary, "status": live.get("status")})
            continue
        if record is not None and stack_name not in replaceable:
            raise RuntimeError(
                f"Stack {region}:{stack_name} changed identity during {phase}, which may not "
                "replace it; refusing adoption"
            )
        created = _stack_creation_time(ctx, region, stack_id)
        if created + _ADOPTION_CLOCK_RESOLUTION < window:
            raise RuntimeError(
                f"Stack {region}:{stack_name} was created at {created.isoformat()}, before "
                f"{phase} started at {window.isoformat()}; refusing adoption"
            )
        generations: list[dict[str, Any]] = []
        if record is not None:
            _require_stack_authority(ctx, record, region=region, stack_name=stack_name)
            previous_id = str(record.get("stack_id") or "")
            previous = describe_stack(ctx.session, region, previous_id)
            if previous is not None and previous.get("status") != "DELETE_COMPLETE":
                raise RuntimeError(
                    f"Stack {region}:{stack_name} has two live generations; refusing adoption"
                )
            generations = [
                *copy.deepcopy(record.get("replaced_generations") or []),
                {
                    "stack_id": previous_id,
                    "authority": record.get("authority"),
                    "adoption": copy.deepcopy(record.get("adoption")),
                    "replaced_by": stack_id,
                    "replaced_during": phase,
                    "observed_status": (previous or {}).get("status", "absent"),
                },
            ]
        with ctx.state_lock:
            _owned_stacks(ctx).setdefault(region, {})[stack_name] = {
                **summary,
                "run_tag": ctx.settings.run_id,
                "authority": _RUN_TAG_ADOPTION_AUTHORITY,
                "adoption": {
                    "phase": phase,
                    "window_started_at": window.isoformat(),
                    "creation_time": created.isoformat(),
                    "adopted_at": utc_now(),
                    "status": live.get("status"),
                },
                "replaced_generations": generations,
            }
            ctx.persist_callback(ctx.checkpoint)
        key = "replaced" if record is not None else "adopted"
        evidence[key].append({**summary, "created_at": created.isoformat()})
    return evidence
