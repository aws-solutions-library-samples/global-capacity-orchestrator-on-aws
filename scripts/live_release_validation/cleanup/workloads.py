"""Delete exactly run-owned Kubernetes workloads and queue records."""

from __future__ import annotations

import copy
import time
from typing import Any
from urllib.parse import quote

from ..checks.central_queue import (
    _central_workload_record,
    _read_central_job_item,
    _reconcile_central_cleanup_workload,
    _validate_central_job_identity,
    _wait_for_central_queue_appearance,
    _wait_for_central_queue_terminal,
)
from ..checks.jobs import (
    _delete_owned_job,
    _job_reference_identity,
    _response_json,
)
from ..constants import (
    _TERMINAL_QUEUE_STATUSES,
)
from ..models import RunContext, utc_now


def _cleanup_central_job(ctx: RunContext, central_job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(central_job["job_id"])
    current = _wait_for_central_queue_appearance(
        ctx,
        central_job,
        raise_on_timeout=False,
    )
    if current is None:
        outcome = {
            "job_id": job_id,
            "complete": False,
            "unresolved": "no consistently readable queue record before the bounded deadline",
        }
        central_job["cleanup_complete"] = False
        central_job["cleanup_result"] = outcome
        ctx.persist()
        raise RuntimeError(
            f"Central queue job {job_id} was not observed; non-observation is not terminal proof"
        )

    status = str(current.get("status") or "unknown")
    previous_cancellation = central_job.get("cancellation")
    cancellation: dict[str, Any] = (
        copy.deepcopy(previous_cancellation)
        if isinstance(previous_cancellation, dict)
        else {"not_required": status in _TERMINAL_QUEUE_STATUSES}
    )
    if status not in _TERMINAL_QUEUE_STATUSES:
        reason = quote("live release validation cleanup", safe="")
        response = ctx.aws_client.make_authenticated_request(
            method="DELETE",
            path=f"/api/v1/queue/jobs/{quote(job_id, safe='')}?reason={reason}",
            target_region=central_job.get("transport_region"),
        )
        if response.status_code == 404:
            raise RuntimeError(f"Central queue job {job_id} disappeared during cancellation")
        if response.status_code == 409:
            cancellation = {
                "not_cancellable": True,
                "status_code": 409,
                "detail": response.text,
            }
        elif response.ok:
            cancellation = {
                "accepted_before_claim": True,
                "response": _response_json(response, "Central queue cancellation"),
            }
        else:
            raise RuntimeError(f"{response.status_code} {response.text}")
        central_job["cancel_attempted"] = True
        central_job["cancellation"] = cancellation
        ctx.persist()
        current, history = _wait_for_central_queue_terminal(ctx, central_job)
    else:
        history = [{"status": status, "at": time.time()}]

    terminal_status = str(current.get("status") or "unknown")
    if terminal_status not in _TERMINAL_QUEUE_STATUSES:
        raise RuntimeError(f"Central queue job {job_id} did not become terminal")
    persisted = _read_central_job_item(ctx, job_id)
    _validate_central_job_identity(central_job, persisted)
    persisted_status = str(persisted.get("status") or "unknown")
    if persisted_status != terminal_status or persisted_status not in _TERMINAL_QUEUE_STATUSES:
        raise RuntimeError(
            f"Central queue job {job_id} lacks consistent terminal DynamoDB evidence"
        )

    _, workload_not_submitted = _reconcile_central_cleanup_workload(
        ctx,
        central_job,
        persisted,
    )

    outcome = {
        "job_id": job_id,
        "complete": True,
        "cancellation": cancellation,
        "terminal_status": terminal_status,
        "status_history": history,
        "consistent_record": persisted,
        "workload_not_submitted": workload_not_submitted,
    }
    central_job["status"] = terminal_status
    central_job["cleanup_complete"] = True
    central_job["cleanup_result"] = outcome
    ctx.persist()
    return outcome


def cleanup_workloads(ctx: RunContext) -> dict[str, Any]:
    """Reconcile every workload and return an explicit teardown barrier result."""
    result: dict[str, Any] = {
        "started_at": utc_now(),
        "complete": False,
        "jobs": [],
        "central_jobs": [],
        "errors": [],
        "unresolved": [],
    }
    reconciled_central_workloads: set[int] = set()
    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        job_id = str(central_job["job_id"])
        try:
            if central_job.get("cleanup_complete"):
                persisted = _read_central_job_item(ctx, job_id)
                _validate_central_job_identity(central_job, persisted)
                persisted_status = str(persisted.get("status") or "unknown")
                checkpoint_status = str(
                    central_job.get("status")
                    or (central_job.get("cleanup_result") or {}).get("terminal_status")
                    or ""
                )
                if persisted_status not in _TERMINAL_QUEUE_STATUSES:
                    raise RuntimeError(
                        f"Previously completed central cleanup for {job_id} is no longer terminal"
                    )
                if checkpoint_status and checkpoint_status != persisted_status:
                    raise RuntimeError(
                        f"Central cleanup status changed from {checkpoint_status} "
                        f"to {persisted_status}"
                    )
                workload_record, _ = _reconcile_central_cleanup_workload(
                    ctx,
                    central_job,
                    persisted,
                )
                result["central_jobs"].append(
                    copy.deepcopy(central_job.get("cleanup_result") or {})
                )
            else:
                result["central_jobs"].append(_cleanup_central_job(ctx, central_job))
                workload_record = _central_workload_record(ctx, central_job)
            reconciled_central_workloads.add(id(workload_record))
        except Exception as exc:  # noqa: BLE001 - preserve every unresolved resource
            error = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"resource": f"central:{job_id}", "error": error})
            result["unresolved"].append({"resource": f"central:{job_id}", "reason": error})

    for record in ctx.checkpoint.state.get("jobs", []):
        if record.get("deleted"):
            continue
        requested_reference = f"{record['region']}:{record['namespace']}/{record['name']}"
        reference = requested_reference
        try:
            if record.get("path") == "dynamodb" and id(record) not in reconciled_central_workloads:
                raise RuntimeError(
                    "Central workload was not reconciled from terminal DynamoDB evidence "
                    "in this cleanup attempt"
                )
            actual_name, actual_namespace = _job_reference_identity(record)
            reference = f"{record['region']}:{actual_namespace}/{actual_name}"
            deletion = _delete_owned_job(ctx, record)
            result["jobs"].append(
                {
                    "region": record["region"],
                    "namespace": actual_namespace,
                    "name": actual_name,
                    "requested_namespace": record["namespace"],
                    "requested_name": record["name"],
                    "uid": record.get("uid"),
                    "deletion": deletion,
                }
            )
        except Exception as exc:  # noqa: BLE001 - preserve every unresolved resource
            error = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"resource": reference, "error": error})
            result["unresolved"].append({"resource": reference, "reason": error})

    for central_job in ctx.checkpoint.state.get("central_jobs", []):
        if not central_job.get("cleanup_complete"):
            reference = f"central:{central_job['job_id']}"
            if not any(item["resource"] == reference for item in result["unresolved"]):
                result["unresolved"].append(
                    {"resource": reference, "reason": "terminal queue evidence is incomplete"}
                )
    for record in ctx.checkpoint.state.get("jobs", []):
        if not record.get("deleted"):
            name = str(record.get("k8s_job_name") or record["name"])
            namespace = str(record.get("k8s_job_namespace") or record["namespace"])
            reference = f"{record['region']}:{namespace}/{name}"
            if not any(item["resource"] == reference for item in result["unresolved"]):
                result["unresolved"].append(
                    {"resource": reference, "reason": "UID-bound Job absence is incomplete"}
                )

    result["complete"] = not result["errors"] and not result["unresolved"]
    result["ended_at"] = utc_now()
    ctx.checkpoint.state.setdefault("workload_cleanup_attempts", []).append(result)
    ctx.persist()
    return result
