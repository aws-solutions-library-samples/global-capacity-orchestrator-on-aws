"""central-queue: the idempotent DynamoDB-backed queue lifecycle."""

from __future__ import annotations

import copy
from typing import Any

from ..checks.central_queue import (
    _central_manifest,
    _central_queue_job_id,
    _get_central_queue_job,
    _read_central_job_item,
    _reconcile_central_workload_identity,
    _register_central_job,
    _validate_central_job_identity,
    _wait_for_central_queue_appearance,
    _wait_for_central_queue_terminal,
)
from ..checks.jobs import (
    _complete_job_lifecycle,
    _effective_job_identity,
    _job_appearance_timeout,
    _register_job,
    _response_json,
    _run_token,
)
from ..constants import (
    _RUN_JOB_LABEL,
)
from ..models import RunContext


def action_central_queue_lifecycle(ctx: RunContext) -> dict[str, Any]:
    """Exercise the idempotent DynamoDB queue and require terminal persistence."""
    manifest, name, namespace, marker = _central_manifest(ctx)
    target_region = ctx.deployment_regions[0]
    record = _register_job(
        ctx,
        name=name,
        namespace=namespace,
        execution_region=target_region,
        path="dynamodb",
        reactivate_deleted=False,
    )
    transport_region = record.get("transport_region")
    idempotency_key = f"gco-live-validation:{ctx.settings.run_id}:central"
    job_id = _central_queue_job_id(idempotency_key)
    body = {
        "manifest": manifest,
        "target_region": target_region,
        "namespace": namespace,
        "priority": 100,
        "labels": {_RUN_JOB_LABEL: _run_token(ctx.settings.run_id)},
    }

    envelope = {
        "transport": "central-queue",
        "body": body,
        "idempotency_key": idempotency_key,
        "job_id": job_id,
        "transport_region": transport_region,
    }
    ctx.prepare_job_submission(record, envelope=envelope, resumable=True)

    central_record = _register_central_job(
        ctx,
        job_id=job_id,
        idempotency_key=idempotency_key,
        record=record,
        marker=marker,
        body=body,
    )
    initial_state = str(record.get("submission_state") or "")
    queue_job = _get_central_queue_job(ctx, central_record)
    submission: dict[str, Any]
    if queue_job is None and initial_state in {"prepared", "submitting", "submitted"}:
        if initial_state in {"prepared", "submitting"}:
            ctx.begin_job_submission(
                record,
                reconciliation_timeout_seconds=_job_appearance_timeout(ctx),
            )
        persisted_envelope = record.get("submission_envelope")
        if not isinstance(persisted_envelope, dict) or persisted_envelope != envelope:
            raise RuntimeError("Central queue replay envelope changed")
        response = ctx.aws_client.make_authenticated_request(
            method="POST",
            path="/api/v1/queue/jobs",
            body=copy.deepcopy(persisted_envelope["body"]),
            headers={"Idempotency-Key": str(persisted_envelope["idempotency_key"])},
            target_region=persisted_envelope.get("transport_region"),
        )
        if response.status_code == 409:
            raise RuntimeError(
                "Central queue rejected the exact idempotent replay because request drift was detected"
            )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Central queue submission failed: {response.status_code} {response.text}"
            )
        submission = _response_json(response, "Central queue submission")
        queued_job = submission.get("job")
        if not isinstance(queued_job, dict):
            raise RuntimeError("Central queue response omitted job")
        _validate_central_job_identity(central_record, queued_job)
        ctx.finish_job_submission(
            record,
            submission,
            appearance_timeout_seconds=_job_appearance_timeout(ctx),
        )
        central_record["submission"] = submission
        central_record["submission_state"] = "submitted"
        central_record["appearance_deadline"] = record["appearance_deadline"]
        ctx.persist()
    elif queue_job is not None:
        submission = (
            record.get("submission")
            or central_record.get("submission")
            or {"reconciled_existing_job": True, "job": queue_job}
        )
        if not isinstance(submission, dict):
            raise RuntimeError("Checkpointed central queue submission is malformed")
        submitted_job = submission.get("job")
        if isinstance(submitted_job, dict):
            _validate_central_job_identity(central_record, submitted_job)
        if initial_state in {"submitting", "submitted", "appeared"}:
            ctx.finish_job_submission(
                record,
                submission,
                appearance_timeout_seconds=_job_appearance_timeout(ctx),
            )
        central_record["submission"] = submission
        central_record["submission_state"] = "reconciled"
        central_record["appearance_deadline"] = record.get("appearance_deadline")
        ctx.persist()
    else:
        raise RuntimeError(
            f"Central queue record is absent and state {initial_state!r} is not replayable"
        )

    _wait_for_central_queue_appearance(ctx, central_record)
    final_job, history = _wait_for_central_queue_terminal(ctx, central_record)
    central_record["status"] = str(final_job.get("status") or "unknown")
    central_record["status_history"] = history
    ctx.persist()
    if final_job.get("status") != "succeeded":
        raise RuntimeError(
            f"Central queue job {job_id} finished as {final_job.get('status')}: "
            f"{final_job.get('error_message') or 'no error message'}"
        )

    item = _read_central_job_item(ctx, job_id)
    if item.get("status") != "succeeded":
        raise RuntimeError(f"DynamoDB record {job_id} is {item.get('status')}, expected succeeded")
    record = _reconcile_central_workload_identity(
        ctx,
        central_record,
        item,
        workload_record=record,
    )
    if record.get("deleted"):
        evidence = record.get("validation_evidence")
        if not isinstance(evidence, dict) or evidence.get("marker") != marker:
            raise RuntimeError(
                "Central Job was deleted without checkpointed live-validation evidence; "
                "refusing an idempotency replay"
            )
        actual_name, actual_namespace = _effective_job_identity(record)
        expected_evidence = {
            "name": actual_name,
            "namespace": actual_namespace,
            "uid": record.get("k8s_job_uid"),
            "central_queue_job_id": job_id,
        }
        for key, expected in expected_evidence.items():
            if evidence.get(key) != expected:
                raise RuntimeError(
                    f"Central Job deletion evidence does not match actual identity: {key}"
                )
        workload_lifecycle = {
            **copy.deepcopy(evidence),
            "deletion": {"reconciled_checkpointed_deletion": True},
        }
    else:
        workload_lifecycle = _complete_job_lifecycle(ctx, record=record, marker=marker)

    central_record["status"] = "succeeded"
    central_record["cleanup_complete"] = True
    central_record["workload_lifecycle"] = workload_lifecycle
    ctx.persist()
    return {
        "submission": submission,
        "job_id": job_id,
        "job_name": name,
        "namespace": namespace,
        "k8s_job_name": record.get("k8s_job_name"),
        "k8s_job_namespace": record.get("k8s_job_namespace"),
        "k8s_job_uid": record.get("k8s_job_uid"),
        "target_region": target_region,
        "status_history": history,
        "final_job": final_job,
        "dynamodb_item": item,
        "workload_lifecycle": workload_lifecycle,
    }
