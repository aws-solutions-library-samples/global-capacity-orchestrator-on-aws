"""Delete exactly run-owned CloudWatch log groups."""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from ..constants import (
    _LOG_CLEANUP_TOKEN_TAG,
    _LOG_GROUP_ABSENCE_OBSERVATIONS,
    _LOG_GROUP_CLEANUP_MAX_PASSES,
    _LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS,
    _LOG_GROUP_OBSERVATION_POLL_SECONDS,
    _RUN_STACK_TAG,
    _LogGroupCleanupError,
)
from ..models import RunContext, utc_now
from ..ownership.cleanup_role import (
    TagConditionedLogDeleter,
    _delete_log_cleanup_helper,
)
from ..ownership.log_groups import (
    _log_group_generation,
    _observe_log_group_stability,
    _record_log_group_observation,
    _set_log_group_disposition,
    _validated_owned_log_group_identity,
)
from ..ownership.stacks import (
    _verify_target_stack_absence,
)


def _log_group_adoption_blockers(
    identity: Mapping[str, Any],
    *,
    run_id: str,
    cleanup_token: str,
) -> list[str]:
    """Explain why a regenerated same-name log group cannot be adopted.

    Teardown-time Lambda invocations flush their final events after their log
    groups were tagged or deleted, recreating untagged generations that belong
    to this run. Adoption is refused for any generation carrying another
    owner's markers: a foreign validation run/cleanup token, or CloudFormation
    stack tags (a real deployment's explicit LogGroup resources are always
    stack-tagged, while Lambda-recreated groups start with no tags at all).
    """
    tags_value = identity.get("tags")
    tags: dict[str, str] = dict(tags_value) if isinstance(tags_value, Mapping) else {}
    blockers = []
    if tags.get(_RUN_STACK_TAG) not in (None, run_id):
        blockers.append(f"foreign {_RUN_STACK_TAG}={tags.get(_RUN_STACK_TAG)!r}")
    if tags.get(_LOG_CLEANUP_TOKEN_TAG) not in (None, cleanup_token):
        blockers.append(f"foreign {_LOG_CLEANUP_TOKEN_TAG}")
    stack_tags = sorted(key for key in tags if key.startswith("aws:cloudformation:"))
    if stack_tags:
        blockers.append("cloudformation-owned generation: " + ", ".join(stack_tags))
    return blockers


def _adopt_regenerated_log_group(
    ctx: RunContext,
    record: dict[str, Any],
    logs_client: Any,
    *,
    region: str,
    name: str,
    observed_generation: Mapping[str, Any],
    authority_tags: Mapping[str, str],
) -> dict[str, Any] | None:
    """Tag and take ownership of a self-regenerated log-group generation.

    Callers must already hold the invocation-level proof that every exact
    target stack is absent, so no live deployment can own this name. Returns
    the stabilized post-tag identity, or ``None`` when the generation did not
    stabilize under this run's authority tags.
    """
    stack_absence = _verify_target_stack_absence(ctx)
    if not stack_absence["all_absent"]:
        raise RuntimeError("Log-group adoption requires every exact target stack to be absent")
    arn = str(observed_generation.get("arn") or "")
    if not arn:
        raise RuntimeError(f"Regenerated log group omitted its ARN: {region}:{name}")
    logs_client.tag_resource(resourceArn=arn, tags=dict(authority_tags))
    post_tag = _observe_log_group_stability(
        logs_client,
        region,
        name,
        expected_identity={**observed_generation, "tags": dict(authority_tags)},
        expected_tags=authority_tags,
        required_present=_LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS,
        required_absent=_LOG_GROUP_ABSENCE_OBSERVATIONS,
    )
    _record_log_group_observation(
        ctx,
        record,
        phase="cleanup-adoption-post-tag",
        outcome=post_tag,
    )
    if post_tag["status"] != "present":
        return None
    identity = post_tag.get("identity")
    if not isinstance(identity, dict):
        raise RuntimeError(f"Adopted log group omitted its identity: {region}:{name}")
    with ctx.state_lock:
        record["observed_identity"] = copy.deepcopy(identity)
        adoptions = record.setdefault("adopted_generations", [])
        if not isinstance(adoptions, list):
            raise RuntimeError("Log-group adopted_generations must be a list")
        adoptions.append(
            {
                "adopted_at": utc_now(),
                "generation": _log_group_generation(identity),
                "stack_absence_proof_at": stack_absence.get("verified_at") or utc_now(),
            }
        )
        ctx.persist_callback(ctx.checkpoint)
    return identity


def _blocked_log_group_entry(
    ctx: RunContext,
    record: dict[str, Any],
    region: str,
    name: str,
    *,
    status: str,
    phase: str,
    outcome: dict[str, Any],
    retryable: bool,
    delete_requested: bool = False,
) -> dict[str, Any]:
    """Record why one generation was preserved and build its report entry.

    ``retryable`` distinguishes "observe again on the next sweep" (a log
    delivery landed mid-observation) from "never delete this" (the generation
    carries another owner's markers). Only retryable blockers trigger another
    sweep; the rest are terminal and fail cleanup with their evidence intact.
    """
    disposition = _set_log_group_disposition(
        ctx,
        record,
        status=status,
        phase=phase,
        outcome=outcome,
    )
    return {
        "region": region,
        "name": name,
        "original_identity": copy.deepcopy(record.get("observed_identity")),
        "deleted": False,
        "blocked": True,
        "retryable": retryable,
        "delete_requested": delete_requested,
        "observation": copy.deepcopy(outcome),
        "replacement_evidence": copy.deepcopy(record.get("replacement_evidence", [])),
        "original_generation_disposition": disposition,
    }


def _converge_one_log_group(
    ctx: RunContext,
    record: dict[str, Any],
    region: str,
    name: str,
    *,
    authority_tags: Mapping[str, str],
    cleanup_token: str,
    deleter: TagConditionedLogDeleter,
) -> tuple[str, dict[str, Any]]:
    """Drive one checkpointed log group to confirmed absence, or block it.

    Returns ``("completed", entry)`` once the exact owned generation is gone
    (or was already absent), and ``("blocked", entry)`` when the group must be
    preserved. The four phases each re-establish identity before acting:

    1. **pending-stability** — repeated reads must agree on the checkpointed
       identity and both authority tags. An untagged same-name regeneration
       from teardown-time Lambda logging is adopted here; a generation with a
       foreign owner's markers blocks permanently.
    2. **immediate-pre-delete** — one final exact read with nothing between it
       and the tag-conditioned delete request.
    3. **post-delete-absence** — absence must hold across repeated reads.
    4. **disposition** — the outcome is checkpointed either way.
    """
    observed = record.get("observed_identity")
    if not isinstance(observed, dict):
        raise RuntimeError(f"Log-group checkpoint identity is malformed: {region}:{name}")
    _log_group_generation(observed)
    normal_logs = ctx.session.client("logs", region_name=region)

    initial = _observe_log_group_stability(
        normal_logs,
        region,
        name,
        expected_identity=observed,
        expected_tags=authority_tags,
        required_present=_LOG_GROUP_CLEANUP_STABLE_OBSERVATIONS,
        required_absent=_LOG_GROUP_ABSENCE_OBSERVATIONS,
    )
    _record_log_group_observation(
        ctx,
        record,
        phase="cleanup-pending-stability",
        outcome=initial,
    )
    if initial["status"] == "absent":
        record["deleted"] = True
        disposition = _set_log_group_disposition(
            ctx,
            record,
            status="already-absent-confirmed",
            phase="cleanup-pending-stability",
            outcome=initial,
        )
        return (
            "completed",
            {
                "region": region,
                "name": name,
                "original_identity": copy.deepcopy(observed),
                "already_absent": True,
                "absence_observations": initial["attempt_count"],
                "original_generation_disposition": disposition,
            },
        )

    identity: dict[str, Any] | None = None
    if initial["status"] == "present":
        candidate = initial.get("identity")
        if not isinstance(candidate, dict):
            raise RuntimeError(f"Stable log-group observation omitted identity: {region}:{name}")
        identity = candidate
    elif initial["status"] == "replacement":
        replacement_identity = initial.get("identity")
        if not isinstance(replacement_identity, dict):
            # The regeneration vanished mid-observation; the next
            # pass will see stable absence.
            return (
                "blocked",
                _blocked_log_group_entry(
                    ctx,
                    record,
                    region,
                    name,
                    status="replacement-without-identity",
                    phase="cleanup-pending-stability",
                    outcome=initial,
                    retryable=True,
                ),
            )
        blockers = _log_group_adoption_blockers(
            replacement_identity,
            run_id=ctx.settings.run_id,
            cleanup_token=cleanup_token,
        )
        if blockers:
            return (
                "blocked",
                _blocked_log_group_entry(
                    ctx,
                    record,
                    region,
                    name,
                    status="replacement-observed-before-delete",
                    phase="cleanup-pending-stability",
                    outcome={**initial, "adoption_blockers": blockers},
                    retryable=False,
                ),
            )
        adopted = _adopt_regenerated_log_group(
            ctx,
            record,
            normal_logs,
            region=region,
            name=name,
            observed_generation=replacement_identity,
            authority_tags=authority_tags,
        )
        if adopted is None:
            return (
                "blocked",
                _blocked_log_group_entry(
                    ctx,
                    record,
                    region,
                    name,
                    status="adoption-did-not-stabilize",
                    phase="cleanup-adoption-post-tag",
                    outcome=initial,
                    retryable=True,
                ),
            )
        identity = adopted
    else:
        if initial["status"] == "tag-drift":
            disposition_status = "authority-tag-drift-before-delete"
            retryable = False
        else:
            disposition_status = "identity-not-stable-before-delete"
            retryable = True
        return (
            "blocked",
            _blocked_log_group_entry(
                ctx,
                record,
                region,
                name,
                status=disposition_status,
                phase="cleanup-pending-stability",
                outcome=initial,
                retryable=retryable,
            ),
        )

    restricted_logs = deleter.client(region)
    # No persistence, sleep, or unrelated API call is permitted between
    # this single exact read and the tag-conditioned delete request.
    pre_delete = _observe_log_group_stability(
        normal_logs,
        region,
        name,
        expected_identity=identity,
        expected_tags=authority_tags,
        required_present=1,
        required_absent=1,
    )
    if pre_delete["status"] == "present":
        try:
            restricted_logs.delete_log_group(logGroupName=name)
        except ClientError as exc:
            _record_log_group_observation(
                ctx,
                record,
                phase="cleanup-immediate-pre-delete",
                outcome=pre_delete,
            )
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
        else:
            _record_log_group_observation(
                ctx,
                record,
                phase="cleanup-immediate-pre-delete",
                outcome=pre_delete,
            )
        record["delete_requested_at"] = utc_now()
        ctx.persist()
    else:
        _record_log_group_observation(
            ctx,
            record,
            phase="cleanup-immediate-pre-delete",
            outcome=pre_delete,
        )

    if pre_delete["status"] not in {"present", "absent"}:
        if pre_delete["status"] == "replacement":
            disposition_status = "replacement-observed-immediately-before-delete"
            retryable = not _log_group_adoption_blockers(
                pre_delete.get("identity") or {},
                run_id=ctx.settings.run_id,
                cleanup_token=cleanup_token,
            )
        elif pre_delete["status"] == "tag-drift":
            disposition_status = "authority-tag-drift-immediately-before-delete"
            retryable = False
        else:
            disposition_status = "identity-not-stable-immediately-before-delete"
            retryable = True
        return (
            "blocked",
            _blocked_log_group_entry(
                ctx,
                record,
                region,
                name,
                status=disposition_status,
                phase="cleanup-immediate-pre-delete",
                outcome=pre_delete,
                retryable=retryable,
            ),
        )

    absence = _observe_log_group_stability(
        normal_logs,
        region,
        name,
        expected_identity=identity,
        expected_tags=authority_tags,
        required_present=None,
        required_absent=_LOG_GROUP_ABSENCE_OBSERVATIONS,
    )
    _record_log_group_observation(
        ctx,
        record,
        phase="cleanup-post-delete-absence",
        outcome=absence,
    )
    if absence["status"] != "absent":
        if absence["status"] == "replacement":
            disposition_status = "replacement-observed-before-confirmed-absence"
            retryable = not _log_group_adoption_blockers(
                absence.get("identity") or {},
                run_id=ctx.settings.run_id,
                cleanup_token=cleanup_token,
            )
        elif absence["status"] == "tag-drift":
            disposition_status = "authority-tag-drift-after-delete-request"
            retryable = False
        else:
            disposition_status = "absence-not-stable-after-delete-request"
            retryable = True
        return (
            "blocked",
            _blocked_log_group_entry(
                ctx,
                record,
                region,
                name,
                status=disposition_status,
                phase="cleanup-post-delete-absence",
                outcome=absence,
                retryable=retryable,
                delete_requested=pre_delete["status"] == "present",
            ),
        )

    record["deleted"] = True
    disposition = _set_log_group_disposition(
        ctx,
        record,
        status="deleted-confirmed-absent",
        phase="cleanup-post-delete-absence",
        outcome=absence,
    )
    entry = {
        "region": region,
        "name": name,
        "arn": identity["arn"],
        "creation_time": identity["creation_time"],
        "stack_id": record["stack_id"],
        "source_logical_id": record["source_logical_id"],
        "source_resource_type": record["source_resource_type"],
        "authority_phase": record["authority_phase"],
        "atomic_resource_tag_condition": True,
        "absence_observations": absence["attempt_count"],
        "deleted": True,
        "adopted": bool(record.get("adopted_generations")),
        "original_generation_disposition": disposition,
    }
    ctx.persist()
    return ("completed", entry)


def _validated_log_group_cleanup_records(
    ctx: RunContext,
) -> tuple[list[tuple[dict[str, Any], str, str]], str]:
    """Validate the cleanup preconditions and return records plus the token.

    Cleanup may only proceed once every exact target stack is absent, because
    a live stack can legitimately recreate its own log groups. The token is the
    second half of the deletion authority (paired with the run tag) and is
    checked for shape before it can reach an IAM condition.
    """
    records = ctx.checkpoint.state.get("owned_log_groups", [])
    if not isinstance(records, list):
        raise RuntimeError("Checkpoint owned_log_groups must be a list")
    cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")

    if records:
        stack_absence = _verify_target_stack_absence(ctx)
        if not stack_absence["all_absent"]:
            raise RuntimeError("Log-group cleanup requires every exact target stack to be absent")
    if records and not re.fullmatch(r"[0-9a-f]{32}", cleanup_token):
        raise RuntimeError("Checkpoint log-group cleanup token is malformed")

    validated: list[tuple[dict[str, Any], str, str]] = []
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise RuntimeError("Checkpoint owned_log_groups must contain objects")
        region, name = _validated_owned_log_group_identity(ctx, raw_record)
        validated.append((raw_record, region, name))
    return validated, cleanup_token


def _cleanup_owned_log_groups(ctx: RunContext) -> dict[str, Any]:
    """Converge every checkpointed log group to stable absence.

    Each record is processed independently by :func:`_converge_one_log_group`,
    so one blocked generation never strands the rest. Teardown-time Lambda
    invocations recreate their own groups after tagging, so untagged same-name
    regenerations are adopted (re-tagged under this run's proven stack-absence
    authority) and deleted; generations carrying another owner's markers stay
    strictly preserved. Bounded extra sweeps absorb log deliveries that land
    mid-cleanup, and only retryable blockers earn another sweep.

    The delegated cleanup helper stack is always torn down, even when
    convergence fails, and both independent failures are reported together.
    """
    results: list[dict[str, Any]] = []
    deleter = TagConditionedLogDeleter(ctx)
    helper_cleanup: dict[str, Any] = {"needed": False, "deleted": True}
    cleanup_error: Exception | None = None
    helper_error: Exception | None = None
    authority_tags: dict[str, str] = {}
    try:
        validated, cleanup_token = _validated_log_group_cleanup_records(ctx)
        authority_tags = {
            _RUN_STACK_TAG: ctx.settings.run_id,
            _LOG_CLEANUP_TOKEN_TAG: cleanup_token,
        }

        completed: dict[tuple[str, str], dict[str, Any]] = {}
        blocked: dict[tuple[str, str], dict[str, Any]] = {}
        for sweep in range(1, _LOG_GROUP_CLEANUP_MAX_PASSES + 1):
            if sweep > 1:
                # Absorb straggling teardown log deliveries before re-observing.
                time.sleep(_LOG_GROUP_OBSERVATION_POLL_SECONDS * sweep)
            blocked.clear()
            for record, region, name in validated:
                key = (region, name)
                if key in completed:
                    continue
                outcome, entry = _converge_one_log_group(
                    ctx,
                    record,
                    region,
                    name,
                    authority_tags=authority_tags,
                    cleanup_token=cleanup_token,
                    deleter=deleter,
                )
                if outcome == "completed":
                    completed[key] = entry
                else:
                    blocked[key] = entry

            if not blocked or not any(entry["retryable"] for entry in blocked.values()):
                break

        results = [*completed.values(), *blocked.values()]
        if blocked:
            summary = ", ".join(
                f"{region}:{name} ({entry['original_generation_disposition']['status']})"
                for (region, name), entry in sorted(blocked.items())
            )
            raise RuntimeError(f"Log-group cleanup could not converge for: {summary}")
    except Exception as exc:  # noqa: BLE001 - attach helper cleanup and partial evidence
        cleanup_error = exc
    finally:
        try:
            helper_cleanup = _delete_log_cleanup_helper(ctx)
        except Exception as exc:  # noqa: BLE001 - preserve both independent failures
            helper_error = exc

    errors = []
    if cleanup_error is not None:
        errors.append(
            {"phase": "log-groups", "error": f"{type(cleanup_error).__name__}: {cleanup_error}"}
        )
    if helper_error is not None:
        errors.append(
            {
                "phase": "cleanup-helper",
                "error": f"{type(helper_error).__name__}: {helper_error}",
            }
        )
    details = {
        "log_groups": results,
        "authorization": deleter.authorization,
        "helper_stack_cleanup": helper_cleanup,
        "errors": errors,
    }
    ctx.checkpoint.state["last_log_group_cleanup"] = copy.deepcopy(details)
    ctx.persist()
    if errors:
        message = "Retained CloudWatch log cleanup failed: " + json.dumps(errors, sort_keys=True)
        primary_error = cleanup_error if cleanup_error is not None else helper_error
        raise _LogGroupCleanupError(message, details) from primary_error
    return details
