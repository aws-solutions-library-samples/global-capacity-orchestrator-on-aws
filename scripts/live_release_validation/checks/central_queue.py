"""DynamoDB central-queue identity, reconciliation, and polling helpers."""

from __future__ import annotations

import copy
import hashlib
import re
import time
import uuid
from typing import Any, cast
from urllib.parse import quote

from boto3.dynamodb.types import TypeDeserializer

from ..checks.jobs import (
    _central_workload_identity,
    _job_appearance_timeout,
    _load_manifest,
    _response_json,
    _run_token,
)
from ..constants import (
    _CENTRAL_QUEUE_IDEMPOTENCY_NAMESPACE,
    _PATH_JOB_LABEL,
    _TERMINAL_QUEUE_STATUSES,
)
from ..models import RunContext


def _central_manifest(ctx: RunContext) -> tuple[dict[str, Any], str, str, str]:
    manifests, _name, namespace = _load_manifest(ctx, "api-smoke-job.yaml")
    manifest = copy.deepcopy(manifests[0])
    token = _run_token(ctx.settings.run_id)
    name = f"gco-live-ddb-{token}"[:63].rstrip("-")
    marker = f"GCO_LIVE_DDB_{token}"
    manifest["metadata"]["name"] = name
    manifest["metadata"]["labels"][_PATH_JOB_LABEL] = "dynamodb"
    manifest["spec"]["template"]["metadata"]["labels"][_PATH_JOB_LABEL] = "dynamodb"
    manifest["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "sh",
        "-c",
        f"echo {marker}",
    ]
    return manifest, name, namespace, marker


def _deserialize_item(item: dict[str, Any]) -> dict[str, Any]:
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


def _read_central_job_item(ctx: RunContext, job_id: str) -> dict[str, Any]:
    table_name = f"{ctx.config.project_name}-jobs"
    response = ctx.session.client("dynamodb", region_name=ctx.config.global_region).get_item(
        TableName=table_name,
        Key={"job_id": {"S": job_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        raise RuntimeError(f"DynamoDB item {job_id} was not found in {table_name}")
    return _deserialize_item(item)


def _central_queue_job_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(_CENTRAL_QUEUE_IDEMPOTENCY_NAMESPACE, idempotency_key))


def _central_queue_kubernetes_job_name(original_name: str, job_id: str) -> str:
    suffix = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    prefix = re.sub(r"[^a-z0-9-]+", "-", original_name.lower()).strip("-")
    prefix = prefix[: 63 - len(suffix) - 1].rstrip("-") or "gco-job"
    return f"{prefix}-{suffix}"


def _central_persisted_kubernetes_identity(
    job: dict[str, Any],
    *,
    required: bool,
) -> tuple[str, str, str] | None:
    raw = (
        job.get("k8s_job_name"),
        job.get("k8s_job_namespace"),
        job.get("k8s_job_uid"),
    )
    populated = [value is not None for value in raw]
    if not any(populated):
        if required:
            raise RuntimeError("Central DynamoDB record omitted worker Kubernetes identity")
        return None
    if not all(populated):
        raise RuntimeError("Central DynamoDB record contains a partial Kubernetes identity")
    identity = tuple(str(value or "") for value in raw)
    if not all(identity):
        raise RuntimeError("Central DynamoDB record contains an empty Kubernetes identity field")
    return cast(tuple[str, str, str], identity)


def _validate_central_checkpoint_kubernetes_identity(
    central_record: dict[str, Any],
    identity: dict[str, str],
) -> None:
    """Reject partial or conflicting central identity before mutating either record."""
    previous = {key: central_record.get(key) for key in identity}
    populated = [value is not None for value in previous.values()]
    if any(populated) and not all(populated):
        raise RuntimeError("Checkpoint central record contains a partial Kubernetes identity")
    source = central_record.get("k8s_identity_source")
    if source is not None and source != "dynamodb":
        raise RuntimeError("Central checkpoint Kubernetes identity has an unexpected source")
    if source is not None and not any(populated):
        raise RuntimeError("Central checkpoint identity source has no Kubernetes identity")
    for key, value in identity.items():
        if previous[key] is not None and previous[key] != value:
            raise RuntimeError(f"Central checkpoint Kubernetes identity changed: {key}")


def _reconcile_central_workload_identity(
    ctx: RunContext,
    central_record: dict[str, Any],
    persisted_job: dict[str, Any],
    *,
    workload_record: dict[str, Any] | None = None,
    require_identity: bool = True,
) -> dict[str, Any]:
    """Bind exact worker evidence without mutating requested replay identity."""
    _validate_central_job_identity(central_record, persisted_job)
    identity = _central_persisted_kubernetes_identity(
        persisted_job,
        required=require_identity,
    )
    record = workload_record or _central_workload_record(ctx, central_record)
    if identity is None:
        return record

    actual_name, actual_namespace, actual_uid = identity
    expected_name = _central_queue_kubernetes_job_name(
        str(central_record["job_name"]),
        str(central_record["job_id"]),
    )
    if actual_name != expected_name:
        raise RuntimeError(
            f"Central worker persisted unexpected Kubernetes Job name {actual_name!r}; "
            f"expected {expected_name!r}"
        )
    if actual_namespace != central_record["namespace"]:
        raise RuntimeError("Central worker persisted a different Kubernetes namespace")

    central_identity = {
        "k8s_job_name": actual_name,
        "k8s_job_namespace": actual_namespace,
        "k8s_job_uid": actual_uid,
    }
    _validate_central_checkpoint_kubernetes_identity(central_record, central_identity)
    ctx.bind_central_job_identity(
        record,
        job_id=str(central_record["job_id"]),
        name=actual_name,
        namespace=actual_namespace,
        uid=actual_uid,
        appearance_timeout_seconds=_job_appearance_timeout(ctx),
    )
    with ctx.state_lock:
        _validate_central_checkpoint_kubernetes_identity(central_record, central_identity)
        central_record.update(central_identity)
        central_record["k8s_identity_source"] = "dynamodb"
        central_record["workload_appearance_deadline"] = record.get("appearance_deadline")
        ctx.persist_callback(ctx.checkpoint)
    return record


def _register_central_job(
    ctx: RunContext,
    *,
    job_id: str,
    idempotency_key: str,
    record: dict[str, Any],
    marker: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    candidate = {
        "job_id": job_id,
        "idempotency_key": idempotency_key,
        "job_name": record["name"],
        "namespace": record["namespace"],
        "target_region": record["region"],
        "transport_region": record.get("transport_region"),
        "marker": marker,
        "body": copy.deepcopy(body),
    }
    with ctx.state_lock:
        raw_central_jobs = ctx.checkpoint.state.setdefault("central_jobs", [])
        if not isinstance(raw_central_jobs, list) or any(
            not isinstance(item, dict) for item in raw_central_jobs
        ):
            raise RuntimeError("Checkpoint central_jobs must be a list of objects")
        central_jobs = cast(list[dict[str, Any]], raw_central_jobs)
        matches = [
            item
            for item in central_jobs
            if item.get("job_id") == job_id or item.get("idempotency_key") == idempotency_key
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Checkpoint contains duplicate central Job records for {job_id}")
        if matches:
            central_record = matches[0]
            for key, value in candidate.items():
                if central_record.get(key) != value:
                    raise RuntimeError(f"Central Job identity changed for {job_id}: {key}")
        else:
            central_record = {
                **candidate,
                "submission_state": str(record.get("submission_state") or "prepared"),
                "appearance_deadline": record.get("appearance_deadline"),
                "cleanup_complete": False,
            }
            central_jobs.append(central_record)
        if central_record.get("appearance_deadline") is None:
            central_record["appearance_deadline"] = record.get("appearance_deadline")
        ctx.persist_callback(ctx.checkpoint)
        return central_record


def _central_workload_record(
    ctx: RunContext,
    central_record: dict[str, Any],
) -> dict[str, Any]:
    raw_jobs = ctx.checkpoint.state.get("jobs", [])
    if not isinstance(raw_jobs, list) or any(not isinstance(item, dict) for item in raw_jobs):
        raise RuntimeError("Checkpoint jobs must be a list of objects")
    jobs = cast(list[dict[str, Any]], raw_jobs)
    matches = [
        record
        for record in jobs
        if record.get("path") == "dynamodb"
        and record.get("name") == central_record.get("job_name")
        and record.get("namespace") == central_record.get("namespace")
        and record.get("region") == central_record.get("target_region")
        and record.get("transport_region") == central_record.get("transport_region")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Central queue record does not resolve to exactly one checkpointed workload: "
            f"{central_record.get('job_id')}"
        )
    return matches[0]


def _validate_central_job_identity(central_record: dict[str, Any], job: dict[str, Any]) -> None:
    expected = {
        "job_id": central_record["job_id"],
        "job_name": central_record["job_name"],
        "namespace": central_record["namespace"],
        "target_region": central_record["target_region"],
    }
    for key, value in expected.items():
        if str(job.get(key) or "") != str(value):
            raise RuntimeError(f"Central queue returned a different {key} for {value!r}")
    observed_key = job.get("idempotency_key")
    if observed_key is not None and observed_key != central_record["idempotency_key"]:
        raise RuntimeError("Central queue idempotency key changed")


def _reconcile_central_cleanup_workload(
    ctx: RunContext,
    central_record: dict[str, Any],
    persisted_job: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reconcile workload authority from terminal DynamoDB cleanup evidence."""
    _validate_central_job_identity(central_record, persisted_job)
    terminal_status = str(persisted_job.get("status") or "unknown")
    if terminal_status not in _TERMINAL_QUEUE_STATUSES:
        raise RuntimeError("Central cleanup evidence is not terminal")

    worker_proved_not_created = persisted_job.get("workload_not_created") is True
    if worker_proved_not_created and terminal_status != "failed":
        raise RuntimeError(
            "Central worker no-workload proof is valid only for a failed queue record"
        )
    persisted_identity = _central_persisted_kubernetes_identity(
        persisted_job,
        required=terminal_status == "succeeded"
        or (terminal_status == "failed" and not worker_proved_not_created),
    )
    workload_record = _central_workload_record(ctx, central_record)

    if worker_proved_not_created:
        if persisted_identity is not None:
            raise RuntimeError(
                "Failed central Job has both no-workload proof and Kubernetes identity"
            )
        if _central_workload_identity(workload_record) is not None:
            raise RuntimeError(
                "Worker-proven uncreated central Job already has checkpointed workload identity"
            )
        if _central_workload_identity(central_record) is not None:
            raise RuntimeError(
                "Worker-proven uncreated central Job already has central checkpoint identity"
            )
        if workload_record.get("uid"):
            raise RuntimeError(
                "Worker-proven uncreated central Job already has Kubernetes UID authority"
            )
        job_id = str(central_record["job_id"])
        state = str(workload_record.get("submission_state") or "registered")
        prior_proof = workload_record.get("central_worker_not_created_job_id")
        if state == "deleted":
            if prior_proof != job_id:
                raise RuntimeError(
                    "Deleted central workload lacks matching worker no-workload proof"
                )
        else:
            ctx.mark_central_job_not_created_by_worker(workload_record, job_id=job_id)
        return workload_record, True

    if terminal_status != "cancelled":
        return (
            _reconcile_central_workload_identity(
                ctx,
                central_record,
                persisted_job,
                workload_record=workload_record,
            ),
            False,
        )

    if persisted_identity is not None or _central_workload_identity(workload_record) is not None:
        raise RuntimeError("Cancelled-before-claim central Job unexpectedly has workload identity")
    if workload_record.get("uid"):
        raise RuntimeError(
            "Cancelled-before-claim central Job already has Kubernetes UID authority"
        )
    job_id = str(central_record["job_id"])
    state = str(workload_record.get("submission_state") or "registered")
    prior_proof = workload_record.get("central_cancelled_before_claim_job_id")
    if state == "deleted":
        if prior_proof != job_id:
            raise RuntimeError("Deleted central workload lacks matching cancellation proof")
    else:
        ctx.mark_central_job_cancelled_before_claim(workload_record, job_id=job_id)
    return workload_record, True


def _get_central_queue_job(
    ctx: RunContext, central_record: dict[str, Any]
) -> dict[str, Any] | None:
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=f"/api/v1/queue/jobs/{quote(str(central_record['job_id']), safe='')}",
        target_region=central_record.get("transport_region"),
    )
    if response.status_code == 404:
        return None
    if not response.ok:
        raise RuntimeError(f"Central queue lookup failed: {response.status_code} {response.text}")
    data = _response_json(response, "Central queue lookup")
    job = data.get("job")
    if not isinstance(job, dict):
        raise RuntimeError("Central queue lookup omitted job")
    _validate_central_job_identity(central_record, job)
    return job


def _wait_for_central_queue_appearance(
    ctx: RunContext,
    central_record: dict[str, Any],
    *,
    raise_on_timeout: bool = True,
) -> dict[str, Any] | None:
    raw_deadline = central_record.get("appearance_deadline")
    if raw_deadline is None:
        deadline = time.time() + _job_appearance_timeout(ctx)
        central_record["appearance_deadline"] = deadline
        ctx.persist()
    else:
        deadline = float(raw_deadline)
    while True:
        job = _get_central_queue_job(ctx, central_record)
        if job is not None:
            return job
        if time.time() >= deadline:
            if raise_on_timeout:
                raise TimeoutError(
                    f"Central queue job {central_record['job_id']} did not appear before "
                    "the bounded submission deadline"
                )
            return None
        time.sleep(ctx.settings.poll_interval_seconds)


def _wait_for_central_queue_terminal(
    ctx: RunContext,
    central_record: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + ctx.settings.job_timeout_seconds
    history: list[dict[str, Any]] = []
    while True:
        job = _get_central_queue_job(ctx, central_record)
        if job is None:
            raise RuntimeError(
                f"Central queue job {central_record['job_id']} disappeared after observation"
            )
        status = str(job.get("status") or "unknown")
        history.append({"status": status, "at": time.time()})
        if status in _TERMINAL_QUEUE_STATUSES:
            return job, history
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Central queue job {central_record['job_id']} did not reach a terminal status"
            )
        time.sleep(ctx.settings.poll_interval_seconds)
