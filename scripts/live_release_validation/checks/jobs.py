"""Job submission, appearance, terminal-state, and deletion helpers."""

from __future__ import annotations

import copy
import hashlib
import re
import time
from typing import Any, cast
from urllib.parse import quote

from cli.jobs import resolve_submission_identity

from ..constants import (
    _CENTRAL_MANAGED_BY_LABEL,
    _CENTRAL_ORIGINAL_NAME_ANNOTATION,
    _CENTRAL_QUEUE_ID_ANNOTATION,
    _CENTRAL_QUEUE_KEY_LABEL,
    _MANIFEST_DIR,
    _PATH_JOB_LABEL,
    _RUN_JOB_LABEL,
)
from ..context import (
    _job_transport_region,
)
from ..models import RunContext


def _run_token(run_id: str) -> str:
    token = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    token = re.sub(r"-+", "-", token)[:24].rstrip("-")
    if not token:
        raise RuntimeError("run_id does not contain a Kubernetes-safe token")
    return token


def _replace_token(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace("__RUN_TOKEN__", token)
    if isinstance(value, list):
        return [_replace_token(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _replace_token(item, token) for key, item in value.items()}
    return value


def _load_manifest(ctx: RunContext, filename: str) -> tuple[list[dict[str, Any]], str, str]:
    # _MANIFEST_DIR is anchored at the package root by constants.py; never
    # resolve manifests relative to this module's __file__ (that is exactly
    # what failed in run retry1-8002d6c80f62 when this helper moved here).
    path = _MANIFEST_DIR / filename
    manifests = ctx.job_manager.load_manifests(str(path))
    manifests = _replace_token(manifests, _run_token(ctx.settings.run_id))
    job = next(item for item in manifests if item.get("kind") == "Job")
    name = str(job["metadata"]["name"])
    namespace = str(job["metadata"]["namespace"])
    return manifests, name, namespace


def _central_workload_identity(record: dict[str, Any]) -> tuple[str, str, str] | None:
    values = (
        record.get("k8s_job_name"),
        record.get("k8s_job_namespace"),
        record.get("k8s_job_uid"),
    )
    populated = [value is not None for value in values]
    if any(populated) and not all(populated):
        raise RuntimeError("Checkpoint contains a partial central Kubernetes identity")
    if not any(populated):
        return None
    identity = tuple(str(value or "") for value in values)
    if not all(identity):
        raise RuntimeError("Checkpoint contains an empty central Kubernetes identity field")
    return cast(tuple[str, str, str], identity)


def _effective_job_identity(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("path") == "dynamodb":
        central_identity = _central_workload_identity(record)
        if central_identity is None:
            raise RuntimeError(
                "Central workload Kubernetes identity has not been bound from DynamoDB"
            )
        return central_identity[0], central_identity[1]
    return str(record["name"]), str(record["namespace"])


def _job_reference_identity(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("path") == "dynamodb":
        central_identity = _central_workload_identity(record)
        if central_identity is not None:
            return central_identity[0], central_identity[1]
    return str(record["name"]), str(record["namespace"])


def _job_api_path(record: dict[str, Any], suffix: str = "") -> str:
    actual_name, actual_namespace = _effective_job_identity(record)
    namespace = quote(actual_namespace, safe="")
    name = quote(actual_name, safe="")
    return f"/api/v1/jobs/{namespace}/{name}{suffix}"


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{operation} returned invalid JSON: {response.text}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{operation} returned a non-object JSON response")
    return value


def _verify_response_region(data: dict[str, Any], expected_region: str, operation: str) -> None:
    actual_region = str(data.get("region") or "")
    if actual_region != expected_region:
        raise RuntimeError(
            f"{operation} came from Region {actual_region or 'unknown'}, expected {expected_region}"
        )


def _validate_central_workload_metadata(
    record: dict[str, Any],
    metadata: dict[str, Any],
    labels: dict[str, Any],
    uid: str,
) -> None:
    queue_job_id = str(record.get("central_queue_job_id") or "")
    expected_uid = str(record.get("k8s_job_uid") or "")
    if not queue_job_id or not expected_uid:
        raise RuntimeError("Central workload is missing immutable queue/UID authority")
    if uid != expected_uid:
        raise RuntimeError("Kubernetes Job UID differs from persisted central worker identity")
    if labels.get(_CENTRAL_MANAGED_BY_LABEL) != "central-queue":
        raise RuntimeError("Central Job managed-by label does not match the worker contract")
    expected_queue_key = hashlib.sha256(queue_job_id.encode("utf-8")).hexdigest()[:32]
    if labels.get(_CENTRAL_QUEUE_KEY_LABEL) != expected_queue_key:
        raise RuntimeError("Central Job queue-key label does not match its queue ID")
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        raise RuntimeError("Central Job lookup omitted ownership annotations")
    if annotations.get(_CENTRAL_QUEUE_ID_ANNOTATION) != queue_job_id:
        raise RuntimeError("Central Job queue ID annotation does not match the checkpoint")
    if annotations.get(_CENTRAL_ORIGINAL_NAME_ANNOTATION) != record["name"]:
        raise RuntimeError("Central Job original-name annotation does not match the request")


def _get_owned_job(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any] | None:
    """Return a Job only after authoritative HTTP and UID/label verification."""
    actual_name, actual_namespace = _effective_job_identity(record)
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=_job_api_path(record),
        target_region=record.get("transport_region"),
    )
    if response.status_code == 404:
        return None
    if not response.ok:
        raise RuntimeError(
            f"Job lookup failed for {record['region']}:{actual_namespace}/{actual_name}: "
            f"{response.status_code} {response.text}"
        )
    data = _response_json(response, "Job lookup")
    _verify_response_region(data, str(record["region"]), "Job lookup")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("Job lookup omitted metadata")
    if metadata.get("name") != actual_name or metadata.get("namespace") != actual_namespace:
        raise RuntimeError("Job lookup returned a different Kubernetes identity")
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        raise RuntimeError("Job lookup omitted ownership labels")
    if labels.get(_RUN_JOB_LABEL) != record["run_label"]:
        raise RuntimeError("Job run label does not match the checkpoint")
    if labels.get(_PATH_JOB_LABEL) != record["path"]:
        raise RuntimeError("Job validation-path label does not match the checkpoint")
    uid = str(metadata.get("uid") or "")
    if not uid:
        raise RuntimeError("Job lookup omitted metadata.uid")
    if record.get("path") == "dynamodb":
        _validate_central_workload_metadata(record, metadata, labels, uid)
    ctx.record_job_uid(record, uid)
    return data


def _reactivate_deleted_job_record(ctx: RunContext, record: dict[str, Any]) -> None:
    """Permit a crash-window replay only after authoritative prior absence."""
    if not record.get("deleted"):
        return
    if _get_owned_job(ctx, record) is not None:
        raise RuntimeError("Checkpoint marks a Job deleted but the exact UID still exists")
    with ctx.state_lock:
        previous_uid = record.get("uid")
        if previous_uid:
            record.setdefault("previous_uids", []).append(previous_uid)
        record["uid"] = None
        record["deleted"] = False
        record["submission_state"] = "registered"
        for key in (
            "submission_started_at",
            "submission_reconcile_deadline",
            "submission_acknowledged_at",
            "appearance_deadline",
            "submission",
            "submission_envelope",
            "submission_resumable",
            "submission_blocked_reason",
            "submission_blocked_at",
            "not_submitted_at",
            "validation_evidence",
            "deleted_at",
        ):
            record.pop(key, None)
        ctx.persist_callback(ctx.checkpoint)


def _job_appearance_timeout(ctx: RunContext) -> int:
    return min(ctx.settings.job_timeout_seconds, ctx.settings.queue_timeout_seconds)


def _wait_for_owned_job_appearance(
    ctx: RunContext,
    record: dict[str, Any],
    *,
    raise_on_timeout: bool = True,
) -> dict[str, Any] | None:
    raw_deadline = record.get("appearance_deadline")
    if raw_deadline is None:
        deadline = time.time() + _job_appearance_timeout(ctx)
        with ctx.state_lock:
            record["appearance_deadline"] = deadline
            ctx.persist_callback(ctx.checkpoint)
    else:
        deadline = float(raw_deadline)
    while True:
        job = _get_owned_job(ctx, record)
        if job is not None:
            return job
        if time.time() >= deadline:
            if raise_on_timeout:
                actual_name, actual_namespace = _effective_job_identity(record)
                raise TimeoutError(
                    f"Job {record['region']}:{actual_namespace}/{actual_name} "
                    "did not appear before the bounded submission deadline"
                )
            return None
        time.sleep(ctx.settings.poll_interval_seconds)


def _wait_for_ambiguous_job_reconciliation(
    ctx: RunContext,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Observe a non-replayable escaped submission until its distinct deadline."""
    raw_deadline = record.get("submission_reconcile_deadline")
    if raw_deadline is None:
        raise RuntimeError("Ambiguous Job submission has no reconciliation deadline")
    deadline = float(raw_deadline)
    while True:
        job = _get_owned_job(ctx, record)
        if job is not None:
            return job
        if time.time() >= deadline:
            return None
        time.sleep(ctx.settings.poll_interval_seconds)


def _job_status(job: dict[str, Any]) -> str:
    status = job.get("status") or {}
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Complete" and condition.get("status") == "True":
            return "succeeded"
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return "failed"
    return "running" if int(status.get("active") or 0) > 0 else "pending"


def _wait_for_owned_job_terminal(
    ctx: RunContext, record: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + ctx.settings.job_timeout_seconds
    history: list[dict[str, Any]] = []
    while True:
        job = _get_owned_job(ctx, record)
        if job is None:
            raise RuntimeError("An owned Job disappeared before reaching a terminal state")
        status = _job_status(job)
        history.append({"at": time.time(), "status": status})
        if status in {"succeeded", "failed"}:
            return job, history
        if time.monotonic() >= deadline:
            actual_name, actual_namespace = _effective_job_identity(record)
            raise TimeoutError(
                f"Job {record['region']}:{actual_namespace}/{actual_name} "
                f"did not complete within {ctx.settings.job_timeout_seconds}s"
            )
        time.sleep(ctx.settings.poll_interval_seconds)


def _owned_job_logs(ctx: RunContext, record: dict[str, Any], tail: int = 200) -> str:
    if _get_owned_job(ctx, record) is None:
        raise RuntimeError("Owned Job disappeared before its logs were read")
    actual_name, actual_namespace = _effective_job_identity(record)
    response = ctx.aws_client.make_authenticated_request(
        method="GET",
        path=_job_api_path(record, f"/logs?tail={tail}"),
        target_region=record.get("transport_region"),
    )
    if not response.ok:
        raise RuntimeError(f"Job log lookup failed: {response.status_code} {response.text}")
    data = _response_json(response, "Job log lookup")
    _verify_response_region(data, str(record["region"]), "Job log lookup")
    if data.get("job_name") != actual_name or data.get("namespace") != actual_namespace:
        raise RuntimeError("Job log lookup returned a different Job identity")
    return str(data.get("logs") or "")


def _wait_for_owned_job_absence(ctx: RunContext, record: dict[str, Any]) -> None:
    consecutive_absent = 0
    deadline = time.monotonic() + 180
    while True:
        current = _get_owned_job(ctx, record)
        if current is None:
            consecutive_absent += 1
            if consecutive_absent >= 3:
                return
        else:
            consecutive_absent = 0
        if time.monotonic() >= deadline:
            actual_name, actual_namespace = _effective_job_identity(record)
            raise TimeoutError(
                f"Job {record['region']}:{actual_namespace}/{actual_name} remained visible"
            )
        time.sleep(min(5, ctx.settings.poll_interval_seconds))


def _delete_owned_job(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any]:
    state = str(record.get("submission_state") or "registered")
    if record.get("path") == "dynamodb" and _central_workload_identity(record) is None:
        if state in {"registered", "prepared", "not_submitted"}:
            ctx.mark_job_not_submitted(record)
            ctx.mark_job_deleted(record)
            return {"not_submitted": True, "already_absent": True}
        raise RuntimeError(
            "Central Job submission may have escaped but no worker-persisted Kubernetes "
            "identity was bound; cleanup remains unresolved"
        )

    current = _get_owned_job(ctx, record)
    if current is None and state == "submitting" and not record.get("uid"):
        current = _wait_for_ambiguous_job_reconciliation(ctx, record)
    elif current is None and state == "submitted" and not record.get("uid"):
        current = _wait_for_owned_job_appearance(ctx, record, raise_on_timeout=False)
    if current is None:
        if record.get("uid"):
            _wait_for_owned_job_absence(ctx, record)
            ctx.mark_job_deleted(record)
            return {"authoritative_absence_after_uid_observation": True}
        if state in {"registered", "prepared", "not_submitted"}:
            ctx.mark_job_not_submitted(record)
            ctx.mark_job_deleted(record)
            return {"not_submitted": True, "already_absent": True}
        raise RuntimeError(
            "Job submission may have escaped but no immutable Kubernetes UID was observed; "
            "cleanup remains unresolved"
        )

    expected_uid = str(record.get("uid") or "")
    if not expected_uid:
        raise RuntimeError("Owned Job has no checkpointed UID at deletion time")
    separator = "&" if "?" in _job_api_path(record) else "?"
    response = ctx.aws_client.make_authenticated_request(
        method="DELETE",
        path=(f"{_job_api_path(record)}{separator}expected_uid={quote(expected_uid, safe='')}"),
        target_region=record.get("transport_region"),
    )
    if response.status_code == 404:
        deletion: dict[str, Any] = {"authoritative_404_after_uid_observation": True}
    elif response.status_code == 409:
        raise RuntimeError("Job UID changed before deletion; Kubernetes precondition rejected it")
    elif response.ok:
        deletion = _response_json(response, "Job deletion")
        _verify_response_region(deletion, str(record["region"]), "Job deletion")
        response_uid = deletion.get("uid")
        if response_uid is not None and str(response_uid) != expected_uid:
            raise RuntimeError("Job deletion response UID did not match the checkpoint")
    else:
        raise RuntimeError(f"Job deletion failed: {response.status_code} {response.text}")
    _wait_for_owned_job_absence(ctx, record)
    ctx.mark_job_deleted(record)
    return deletion


def _complete_job_lifecycle(
    ctx: RunContext,
    *,
    record: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    appeared = _wait_for_owned_job_appearance(ctx, record)
    actual_name, actual_namespace = _effective_job_identity(record)
    if appeared is None:
        raise RuntimeError(
            f"Job {actual_namespace}/{actual_name} never appeared in {record['region']}"
        )
    final, history = _wait_for_owned_job_terminal(ctx, record)
    status = _job_status(final)
    if status != "succeeded":
        raise RuntimeError(
            f"Job {actual_namespace}/{actual_name} in {record['region']} "
            f"finished with status {status}"
        )
    logs = _owned_job_logs(ctx, record)
    if marker not in logs:
        raise RuntimeError(f"Job logs did not contain expected marker {marker!r}")
    evidence = {
        "name": actual_name,
        "namespace": actual_namespace,
        "requested_name": record["name"],
        "requested_namespace": record["namespace"],
        "region": record["region"],
        "transport_region": record.get("transport_region"),
        "uid": record.get("uid"),
        "central_queue_job_id": record.get("central_queue_job_id"),
        "status": status,
        "status_history": history,
        "marker": marker,
        "appearance": {
            "region": appeared.get("region"),
            "uid": (appeared.get("metadata") or {}).get("uid"),
        },
    }
    with ctx.state_lock:
        record["validation_evidence"] = copy.deepcopy(evidence)
        ctx.persist_callback(ctx.checkpoint)
    deletion = _delete_owned_job(ctx, record)
    return {**evidence, "deletion": deletion}


def _register_job(
    ctx: RunContext,
    *,
    name: str,
    namespace: str,
    execution_region: str,
    path: str,
    reactivate_deleted: bool = True,
) -> dict[str, Any]:
    record = ctx.register_job(
        name=name,
        namespace=namespace,
        region=execution_region,
        path=path,
        run_label=_run_token(ctx.settings.run_id),
        transport_region=_job_transport_region(ctx, execution_region),
    )
    if reactivate_deleted:
        _reactivate_deleted_job_record(ctx, record)
    return record


def _run_api_transport_lifecycle(
    ctx: RunContext,
    *,
    manifest_filename: str,
    path: str,
    marker_prefix: str,
) -> dict[str, Any]:
    """Run one manifest's complete authenticated-API Job lifecycle.

    The crash-safe submission dance shared by the ``api`` action and every
    scheduler probe: register the deterministic record, persist the envelope,
    reconcile any escaped prior submission, submit through the manifest API,
    then observe appearance, completion, the log marker, and deletion. The
    marker is ``GCO_LIVE_<MARKER_PREFIX>_<run token>`` and must be emitted by
    the manifest's workload.
    """
    manifests, name, namespace = _load_manifest(ctx, manifest_filename)
    token = _run_token(ctx.settings.run_id)
    marker = f"GCO_LIVE_{marker_prefix}_{token}"
    execution_region = ctx.deployment_regions[0]
    record = _register_job(
        ctx,
        name=name,
        namespace=namespace,
        execution_region=execution_region,
        path=path,
    )
    envelope = {
        "transport": "api",
        "manifests": manifests,
        "namespace": namespace,
        "execution_region": execution_region,
        "transport_region": record.get("transport_region"),
        "labels": {_RUN_JOB_LABEL: token},
    }
    ctx.prepare_job_submission(record, envelope=envelope, resumable=False)

    existing = _get_owned_job(ctx, record)
    submission: dict[str, Any] | None = None
    state = str(record.get("submission_state") or "")
    if existing is None and state == "submitting":
        existing = _wait_for_ambiguous_job_reconciliation(ctx, record)
        if existing is None:
            reason = (
                f"{path} submission crossed a non-idempotent boundary but no Job "
                "appeared; automatic replay is forbidden"
            )
            ctx.block_job_submission(record, reason)
            raise RuntimeError(reason)
    elif existing is None and state == "submitted":
        existing = _wait_for_owned_job_appearance(ctx, record)
    elif existing is None and state == "blocked":
        raise RuntimeError(
            str(record.get("submission_blocked_reason") or f"{path} submission blocked")
        )

    if existing is None:
        if state != "prepared":
            raise RuntimeError(f"Cannot submit {path} Job from state {state!r}")
        ctx.begin_job_submission(
            record,
            reconciliation_timeout_seconds=_job_appearance_timeout(ctx),
        )
        submission = ctx.job_manager.submit_job(
            manifests,
            namespace=namespace,
            target_region=record.get("transport_region"),
            labels={_RUN_JOB_LABEL: token},
        )
        submitted_name, submitted_namespace = resolve_submission_identity(
            submission,
            fallback_name=name,
            fallback_namespace=namespace,
        )
        if submitted_name != name or submitted_namespace != namespace:
            raise RuntimeError(
                f"{path} submission identity mismatch: "
                f"expected {namespace}/{name}, got {submitted_namespace}/{submitted_name}"
            )
        response_region = submission.get("region")
        if response_region is not None and str(response_region) != execution_region:
            raise RuntimeError(
                f"{path} submission executed in {response_region}, expected {execution_region}"
            )
        ctx.finish_job_submission(
            record,
            submission,
            appearance_timeout_seconds=_job_appearance_timeout(ctx),
        )

    lifecycle = _complete_job_lifecycle(ctx, record=record, marker=marker)
    lifecycle["submission"] = submission or {"reconciled_existing_job": True}
    return lifecycle
