"""destroy: remove all run-owned infrastructure in dependency order."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any

from ..cleanup.retained import _retained_resource_cleanup
from ..cleanup.workloads import cleanup_workloads
from ..models import RunContext, to_jsonable, utc_now
from ..ownership.cleanup_role import (
    _delete_log_cleanup_helper,
    _ensure_log_cleanup_helper,
)
from ..ownership.ecr import (
    _checkpoint_new_ecr_images,
    _checkpoint_new_ecr_repositories,
    _record_ecr_repository_creation,
)
from ..ownership.kms import (
    _checkpoint_retained_kms_keys,
)
from ..ownership.stacks import (
    _authorize_owned_stack,
    _owned_stack_record,
    _prepared_change_set_authority,
    _reconcile_stack_ownership,
    _record_prepared_stack_identity,
    _verify_target_stack_absence,
)


def _workload_cleanup_snapshot_sha256(ctx: RunContext) -> str:
    payload = to_jsonable(
        {
            "jobs": copy.deepcopy(ctx.checkpoint.state.get("jobs", [])),
            "central_jobs": copy.deepcopy(ctx.checkpoint.state.get("central_jobs", [])),
        }
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_workload_cleanup_barrier(
    ctx: RunContext,
    cleanup_result: dict[str, Any],
) -> dict[str, Any]:
    if (
        cleanup_result.get("complete") is not True
        or cleanup_result.get("errors")
        or cleanup_result.get("unresolved")
    ):
        raise RuntimeError("Cannot checkpoint an incomplete workload cleanup barrier")
    barrier = {
        "complete": True,
        "completed_at": str(cleanup_result.get("ended_at") or utc_now()),
        "snapshot_sha256": _workload_cleanup_snapshot_sha256(ctx),
        "job_count": len(ctx.checkpoint.state.get("jobs", [])),
        "central_job_count": len(ctx.checkpoint.state.get("central_jobs", [])),
    }
    ctx.checkpoint.state["workload_cleanup_barrier"] = barrier
    ctx.persist()
    return barrier


def _validated_workload_cleanup_barrier(ctx: RunContext) -> dict[str, Any]:
    barrier = ctx.checkpoint.state.get("workload_cleanup_barrier")
    if not isinstance(barrier, dict) or barrier.get("complete") is not True:
        raise RuntimeError("Checkpoint lacks a complete workload cleanup barrier")
    expected = str(barrier.get("snapshot_sha256") or "")
    current = _workload_cleanup_snapshot_sha256(ctx)
    if not expected or expected != current:
        raise RuntimeError("Checkpoint workload identity changed after cleanup completed")
    return barrier


def _resume_workload_cleanup_after_stack_absence(
    ctx: RunContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use an existing barrier, or create one only for a proven empty legacy run."""
    jobs = ctx.checkpoint.state.get("jobs", [])
    central_jobs = ctx.checkpoint.state.get("central_jobs", [])
    if not isinstance(jobs, list) or not isinstance(central_jobs, list):
        raise RuntimeError("Checkpoint workload collections must be lists")

    if ctx.checkpoint.state.get("workload_cleanup_barrier") is None:
        if jobs or central_jobs:
            raise RuntimeError(
                "Target stacks are absent but no completed workload cleanup barrier "
                "was checkpointed"
            )
        workload_cleanup = cleanup_workloads(ctx)
        barrier = _record_workload_cleanup_barrier(ctx, workload_cleanup)
    else:
        barrier = _validated_workload_cleanup_barrier(ctx)
        workload_cleanup = {
            "complete": True,
            "reconciled_from_checkpoint_barrier": True,
            "barrier": copy.deepcopy(barrier),
        }
    _validated_workload_cleanup_barrier(ctx)
    return workload_cleanup, barrier


def _record_target_stack_absence(
    ctx: RunContext,
    stack_absence: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    if stack_absence.get("all_absent") is not True:
        raise RuntimeError("Cannot checkpoint target stack absence while a stack remains")
    workload_barrier = _validated_workload_cleanup_barrier(ctx)
    proof = {
        "verified_at": utc_now(),
        "source": source,
        "workload_cleanup_snapshot_sha256": workload_barrier["snapshot_sha256"],
        "stack_absence": copy.deepcopy(stack_absence),
    }
    ctx.checkpoint.state["target_stacks_absent"] = proof
    ctx.checkpoint.state.setdefault("target_stack_absence_proofs", []).append(proof)
    ctx.persist()
    return proof


def destroy_deployment(ctx: RunContext) -> dict[str, Any]:
    """Retry exact-owned teardown, preserving every structured attempt."""
    if not ctx.checkpoint.deployment_attempted:
        return {"needed": False, "attempts": []}

    initial_absence = _verify_target_stack_absence(ctx)
    if ctx.checkpoint.destroyed and initial_absence["all_absent"]:
        workload_cleanup, workload_barrier = _resume_workload_cleanup_after_stack_absence(ctx)
        absence_proof = _record_target_stack_absence(
            ctx,
            initial_absence,
            source="destroy-already-destroyed-initial-absence",
        )
        _checkpoint_retained_kms_keys(ctx)
        retained_cleanup = _retained_resource_cleanup(ctx)
        final_absence = _verify_target_stack_absence(ctx)
        if not final_absence["all_absent"]:
            raise RuntimeError(
                "A target stack reappeared during repeated retained cleanup: "
                + json.dumps(final_absence["residual"], sort_keys=True)
            )
        completion_proof = _record_target_stack_absence(
            ctx,
            final_absence,
            source="destroy-already-destroyed-completion",
        )
        return {
            "needed": True,
            "already_destroyed": True,
            "workload_cleanup": workload_cleanup,
            "workload_cleanup_barrier": workload_barrier,
            "stack_absence_proof": absence_proof,
            "stack_absence_completion_proof": completion_proof,
            "stack_absence": final_absence,
            "retained_cleanup": retained_cleanup,
            "attempts": ctx.checkpoint.state.get("destroy_attempts", []),
            "workload_cleanup_attempts": ctx.checkpoint.state.get("workload_cleanup_attempts", []),
            "retained_cleanup_attempts": ctx.checkpoint.state.get("retained_cleanup_attempts", []),
        }
    if ctx.checkpoint.destroyed:
        ctx.checkpoint.destroyed = False
        for action_name in ("destroy", "final-inventory"):
            if action_name in ctx.checkpoint.completed_actions:
                ctx.checkpoint.completed_actions.remove(action_name)
        ctx.checkpoint.state.setdefault("stale_destroyed_reconciliations", []).append(
            {"at": utc_now(), "stack_absence": initial_absence}
        )
        ctx.persist()

    if initial_absence["all_absent"]:
        workload_cleanup, workload_barrier = _resume_workload_cleanup_after_stack_absence(ctx)
        absence_proof = _record_target_stack_absence(
            ctx,
            initial_absence,
            source="destroy-resume-initial-absence",
        )
        _checkpoint_new_ecr_repositories(ctx)
        _checkpoint_new_ecr_images(ctx)
        _checkpoint_retained_kms_keys(ctx)
        retained_cleanup = _retained_resource_cleanup(ctx)
        final_absence = _verify_target_stack_absence(ctx)
        if not final_absence["all_absent"]:
            raise RuntimeError(
                "A target stack reappeared during resumed retained cleanup: "
                + json.dumps(final_absence["residual"], sort_keys=True)
            )
        completion_proof = _record_target_stack_absence(
            ctx,
            final_absence,
            source="destroy-resume-completion",
        )
        ctx.checkpoint.destroyed = True
        ctx.persist()
        return {
            "needed": True,
            "resumed_after_stack_absence": True,
            "workload_cleanup": workload_cleanup,
            "workload_cleanup_barrier": workload_barrier,
            "stack_absence_proof": absence_proof,
            "stack_absence_completion_proof": completion_proof,
            "stack_absence": final_absence,
            "retained_cleanup": retained_cleanup,
            "attempts": ctx.checkpoint.state.get("destroy_attempts", []),
            "workload_cleanup_attempts": ctx.checkpoint.state.get("workload_cleanup_attempts", []),
            "retained_cleanup_attempts": ctx.checkpoint.state.get("retained_cleanup_attempts", []),
        }

    workload_cleanup = cleanup_workloads(ctx)
    if not workload_cleanup.get("complete"):
        raise RuntimeError(
            "Workload cleanup is an unresolved teardown barrier: "
            + json.dumps(
                {
                    "errors": workload_cleanup.get("errors", []),
                    "unresolved": workload_cleanup.get("unresolved", []),
                },
                sort_keys=True,
            )
        )
    workload_cleanup_barrier = _record_workload_cleanup_barrier(ctx, workload_cleanup)
    _reconcile_stack_ownership(ctx)
    _checkpoint_new_ecr_repositories(ctx)
    _checkpoint_new_ecr_images(ctx)
    _checkpoint_retained_kms_keys(ctx)

    attempts = ctx.checkpoint.state.setdefault("destroy_attempts", [])
    for invocation_attempt in range(1, ctx.settings.destroy_attempts + 1):
        sequence = len(attempts) + 1
        started_at = utc_now()
        helper_outcomes: list[dict[str, Any]] = []

        def on_cleanup_complete(
            name: str,
            details: dict[str, Any],
            destroy_sequence: int = sequence,
            outcomes: list[dict[str, Any]] = helper_outcomes,
        ) -> None:
            outcome = {
                "destroy_sequence": destroy_sequence,
                "name": name,
                "at": utc_now(),
                "details": copy.deepcopy(details),
            }
            outcomes.append(outcome)
            ctx.checkpoint.state.setdefault("destroy_helper_outcomes", []).append(outcome)
            ctx.persist()

        try:
            helper_authority = _ensure_log_cleanup_helper(ctx)
            _reconcile_stack_ownership(ctx)
            expected_stack_ids = {
                name: (
                    str(record["stack_id"])
                    if (record := _owned_stack_record(ctx, str(region), name)) is not None
                    else None
                )
                for name, region in ctx.checkpoint.state["target_stack_regions"].items()
            }
            prepared_change_sets = _prepared_change_set_authority(ctx)

            def on_prepared(
                stack_name: str,
                region: str,
                stack_id: str,
                change_set_id: str,
                change_set_type: str,
                target_ids: dict[str, str | None] = expected_stack_ids,
                change_sets: dict[str, dict[str, dict[str, str]]] = prepared_change_sets,
            ) -> None:
                _record_prepared_stack_identity(
                    ctx,
                    stack_name,
                    region,
                    stack_id,
                    change_set_id,
                    change_set_type,
                )
                target_ids[stack_name] = stack_id
                change_sets.setdefault(stack_name, {})[change_set_id] = {
                    "change_set_id": change_set_id,
                    "stack_id": stack_id,
                    "change_set_type": change_set_type,
                }

            overall, successful, failed = ctx.stack_manager.destroy_orchestrated(
                force=True,
                parallel=False,
                max_workers=1,
                expected_stack_ids=expected_stack_ids,
                prepared_change_sets=prepared_change_sets,
                authorize_stack=lambda name, region, stack_id: _authorize_owned_stack(
                    ctx,
                    name,
                    region,
                    stack_id,
                ),
                allow_bootstrap=False,
                bootstrap_stacks=ctx.checkpoint.state["bootstrap_stacks"],
                on_cleanup_complete=on_cleanup_complete,
                strict_deployment_token=f"{ctx.settings.run_id}-teardown",
                on_change_set_prepared=on_prepared,
                on_ecr_repository_created=lambda region, repository: (
                    _record_ecr_repository_creation(ctx, region, repository)
                ),
            )
            attempt: dict[str, Any] = {
                "sequence": sequence,
                "invocation_attempt": invocation_attempt,
                "started_at": started_at,
                "overall_success": overall,
                "successful_stacks": successful,
                "failed_stacks": failed,
                "helper_outcomes": helper_outcomes,
                "log_cleanup_helper": helper_authority,
            }
            if overall:
                absence_before_cleanup = _verify_target_stack_absence(ctx)
                attempt["stack_absence_before_retained_cleanup"] = absence_before_cleanup
                if not absence_before_cleanup["all_absent"]:
                    raise RuntimeError(
                        "Target stack absence was not proved after destroy: "
                        + json.dumps(absence_before_cleanup["residual"], sort_keys=True)
                    )
                attempt["target_stack_absence_proof"] = _record_target_stack_absence(
                    ctx,
                    absence_before_cleanup,
                    source="destroy-before-retained-cleanup",
                )
                attempt["retained_cleanup"] = _retained_resource_cleanup(ctx)
                absence_before_completion = _verify_target_stack_absence(ctx)
                attempt["stack_absence_before_completion"] = absence_before_completion
                if not absence_before_completion["all_absent"]:
                    raise RuntimeError(
                        "A target stack reappeared during retained cleanup: "
                        + json.dumps(absence_before_completion["residual"], sort_keys=True)
                    )
                attempt["target_stack_absence_completion_proof"] = _record_target_stack_absence(
                    ctx,
                    absence_before_completion,
                    source="destroy-completion",
                )
        except Exception as exc:  # noqa: BLE001 - retry and preserve teardown evidence
            overall = False
            if "attempt" not in locals() or attempt.get("sequence") != sequence:
                attempt = {
                    "sequence": sequence,
                    "invocation_attempt": invocation_attempt,
                    "started_at": started_at,
                    "successful_stacks": [],
                    "failed_stacks": [],
                    "helper_outcomes": helper_outcomes,
                }
            attempt["overall_success"] = False
            attempt["error"] = f"{type(exc).__name__}: {exc}"
        if not overall:
            try:
                attempt["log_cleanup_helper_cleanup"] = _delete_log_cleanup_helper(ctx)
            except Exception as helper_exc:  # noqa: BLE001 - retain both teardown failures
                helper_error = f"{type(helper_exc).__name__}: {helper_exc}"
                attempt["log_cleanup_helper_cleanup_error"] = helper_error
                previous_error = str(attempt.get("error") or "")
                attempt["error"] = "; ".join(
                    part for part in (previous_error, f"cleanup helper: {helper_error}") if part
                )
        attempt["ended_at"] = utc_now()
        attempts.append(attempt)
        ctx.persist()
        if overall:
            ctx.checkpoint.destroyed = True
            ctx.persist()
            return {
                "needed": True,
                "workload_cleanup": workload_cleanup,
                "workload_cleanup_barrier": workload_cleanup_barrier,
                "workload_cleanup_attempts": ctx.checkpoint.state.get(
                    "workload_cleanup_attempts", []
                ),
                "attempts": attempts,
                "retained_cleanup_attempts": ctx.checkpoint.state.get(
                    "retained_cleanup_attempts", []
                ),
                "stack_absence": attempt["stack_absence_before_completion"],
            }
        if invocation_attempt < ctx.settings.destroy_attempts:
            time.sleep(ctx.settings.destroy_retry_delay_seconds)

    last_attempt = attempts[-1]
    last_failure = last_attempt.get("error") or ", ".join(last_attempt.get("failed_stacks", []))
    raise RuntimeError(
        "Orchestrated teardown did not succeed after "
        f"{ctx.settings.destroy_attempts} invocation attempts; last failure: "
        f"{last_failure or 'unknown'}"
    )


def action_destroy(ctx: RunContext) -> dict[str, Any]:
    """Destroy all run-owned stacks and retained resources."""
    return destroy_deployment(ctx)
