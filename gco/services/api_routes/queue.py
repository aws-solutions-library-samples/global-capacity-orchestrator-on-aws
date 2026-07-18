"""DynamoDB-backed global job queue endpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from gco.services.api_shared import QueuedJobRequest, _check_processor
from gco.services.central_queue_worker import process_queued_jobs_once
from gco.services.structured_logging import sanitize_log_value
from gco.services.template_store import JobSubmissionConflict

if TYPE_CHECKING:
    from gco.services.template_store import JobStore

router = APIRouter(prefix="/api/v1/queue", tags=["Job Queue"])
logger = logging.getLogger(__name__)

_AWS_REGION_PATTERN = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~:/+=-]{1,128}$")
_IDEMPOTENCY_NAMESPACE = uuid.UUID("88284d12-1e04-47d5-8871-607a9e4dac09")


def _validated_queue_manifest(request: QueuedJobRequest) -> dict[str, Any]:
    """Validate the queue envelope and return an isolated Job manifest."""
    configured_regions = {
        value.strip() for value in os.getenv("QUEUE_TARGET_REGIONS", "").split(",") if value.strip()
    }
    if configured_regions:
        if request.target_region not in configured_regions:
            raise HTTPException(status_code=422, detail="target_region is not deployed")
    elif not _AWS_REGION_PATTERN.fullmatch(request.target_region):
        raise HTTPException(status_code=422, detail="target_region is not a valid AWS region")

    processor = _check_processor()
    if request.namespace not in processor.allowed_namespaces:
        raise HTTPException(status_code=422, detail="namespace is not allowed")
    if len(request.namespace) > 63 or not _DNS_LABEL_PATTERN.fullmatch(request.namespace):
        raise HTTPException(status_code=422, detail="namespace is not a valid Kubernetes name")

    manifest = deepcopy(request.manifest)
    if manifest.get("apiVersion") != "batch/v1" or manifest.get("kind") != "Job":
        raise HTTPException(
            status_code=422,
            detail="central queue accepts only apiVersion 'batch/v1', kind 'Job'",
        )
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=422, detail="manifest.metadata must be an object")
    name = metadata.get("name")
    if not isinstance(name, str) or len(name) > 63 or not _DNS_LABEL_PATTERN.fullmatch(name):
        raise HTTPException(status_code=422, detail="manifest.metadata.name is invalid")
    declared_namespace = metadata.get("namespace")
    if declared_namespace is not None and declared_namespace != request.namespace:
        raise HTTPException(
            status_code=422,
            detail="manifest namespace must match the queue envelope namespace",
        )
    metadata["namespace"] = request.namespace
    return manifest


def _submission_hash(request: QueuedJobRequest, manifest: dict[str, Any]) -> str:
    payload = {
        "manifest": manifest,
        "target_region": request.target_region,
        "namespace": request.namespace,
        "priority": request.priority,
        "labels": request.labels or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_job_store() -> JobStore:
    from gco.services.manifest_api import job_store

    if job_store is None:
        raise HTTPException(status_code=503, detail="Job store not initialized")
    return job_store


@router.post("/jobs")
async def submit_job_to_queue(
    request: QueuedJobRequest,
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        description="Stable key for safely replaying an identical submission",
    ),
) -> Response:
    """Submit one validated ``batch/v1`` Job exactly once."""
    if idempotency_key is not None and not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise HTTPException(status_code=422, detail="Idempotency-Key is invalid")

    manifest = _validated_queue_manifest(request)
    request_hash = _submission_hash(request, manifest)
    job_id = (
        str(uuid.uuid5(_IDEMPOTENCY_NAMESPACE, idempotency_key))
        if idempotency_key
        else str(uuid.uuid4())
    )
    store = _get_job_store()

    try:
        job = store.submit_job(
            job_id=job_id,
            manifest=manifest,
            target_region=request.target_region,
            namespace=request.namespace,
            priority=request.priority,
            labels=request.labels,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    except JobSubmissionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        logger.exception("Failed to queue job")
        raise HTTPException(status_code=500, detail="Failed to queue job") from error

    replay = bool(job.pop("idempotent_replay", False))
    return JSONResponse(
        status_code=200 if replay else 201,
        content={
            "timestamp": datetime.now(UTC).isoformat(),
            "message": "Idempotent job replay" if replay else "Job queued successfully",
            "job": job,
        },
    )


@router.get("/jobs")
async def list_queued_jobs(
    target_region: str | None = Query(None, description="Filter by target region"),
    status: str | None = Query(None, description="Filter by status"),
    namespace: str | None = Query(None, description="Filter by namespace"),
    limit: int = Query(100, description="Maximum results", ge=1, le=1000),
    cursor: str | None = Query(
        None,
        description="Opaque continuation cursor returned by the previous page",
        max_length=2048,
    ),
) -> Response:
    """List one bounded page of jobs with optional filters."""
    store = _get_job_store()
    try:
        jobs, next_cursor, partial = store.list_jobs_page(
            target_region=target_region,
            status=status,
            namespace=namespace,
            limit=limit,
            cursor=cursor,
        )
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "count": len(jobs),
                "jobs": jobs,
                "next_cursor": next_cursor,
                "partial": partial,
            },
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as e:
        logger.error(f"Failed to list queued jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {e!s}") from e


@router.get("/jobs/{job_id}")
async def get_queued_job(job_id: str) -> Response:
    """Get details of a specific queued job."""
    store = _get_job_store()
    try:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return JSONResponse(
            status_code=200,
            content={"timestamp": datetime.now(UTC).isoformat(), "job": job},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get job %s: %s", sanitize_log_value(job_id), e)
        raise HTTPException(status_code=500, detail=f"Failed to get job: {e!s}") from e


@router.delete("/jobs/{job_id}")
async def cancel_queued_job(
    job_id: str, reason: str | None = Query(None, description="Cancellation reason")
) -> Response:
    """Cancel a job only while it remains unclaimed in the queue."""
    store = _get_job_store()
    try:
        cancelled = store.cancel_job(job_id, reason=reason)
        if not cancelled:
            raise HTTPException(
                status_code=409,
                detail=f"Job '{job_id}' cannot be cancelled (already running or completed)",
            )
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "message": f"Job '{job_id}' cancelled successfully",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to cancel job %s: %s", sanitize_log_value(job_id), e)
        raise HTTPException(status_code=500, detail=f"Failed to cancel job: {e!s}") from e


@router.get("/stats")
async def get_queue_stats() -> Response:
    """Get job queue statistics by region and status."""
    store = _get_job_store()
    try:
        counts, records_evaluated, truncated = store.get_job_count_summary()
        total_jobs = sum(sum(statuses.values()) for statuses in counts.values())
        total_queued = sum(statuses.get("queued", 0) for statuses in counts.values())
        total_running = sum(statuses.get("running", 0) for statuses in counts.values())

        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "summary": {
                    "total_jobs": total_jobs,
                    "total_queued": total_queued,
                    "total_running": total_running,
                    "complete": not truncated,
                    "records_evaluated": records_evaluated,
                },
                "by_region": counts,
            },
        )
    except Exception as e:
        logger.error(f"Failed to get queue stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {e!s}") from e


@router.post("/poll")
async def poll_and_process_jobs(
    limit: int = Query(5, description="Maximum jobs to process", ge=1, le=20),
) -> Response:
    """Run one immediate queue-worker pass for this region.

    The manifest API also runs this same processor continuously when the
    deployment enables ``CENTRAL_QUEUE_WORKER_ENABLED``. This endpoint remains
    useful for an authenticated operator-triggered pass and diagnostics.
    """
    processor = _check_processor()
    store = _get_job_store()

    try:
        jobs_polled, processed_jobs = await process_queued_jobs_once(
            processor,
            store,
            limit=limit,
        )
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "region": processor.region,
                "jobs_polled": jobs_polled,
                "jobs_processed": len(processed_jobs),
                "results": processed_jobs,
            },
        )
    except Exception as e:
        logger.error(f"Failed to poll jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to poll jobs: {e!s}") from e
