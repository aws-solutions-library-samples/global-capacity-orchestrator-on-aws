"""Job listing, details, logs, events, metrics, delete, and retry endpoints."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from kubernetes import client as kubernetes_client

from gco.models import ManifestSubmissionRequest
from gco.services.api_shared import (
    BulkDeleteRequest,
    _check_namespace,
    _check_processor,
    _parse_event_to_dict,
    _parse_job_to_dict,
    _parse_pod_to_dict,
)

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])
logger = logging.getLogger(__name__)

_LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[-_.A-Za-z0-9]{0,61}[A-Za-z0-9])?$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _decode_pod_log_response(response: Any) -> str:
    """Decode the raw Kubernetes log body without converting bytes to their repr."""
    release_conn = getattr(response, "release_conn", None)
    try:
        payload = response.data if hasattr(response, "data") else response
        if isinstance(payload, str):
            return payload
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload).decode("utf-8", errors="replace")
        raise TypeError(f"Kubernetes returned an unsupported pod log payload: {type(payload)!r}")
    finally:
        if callable(release_conn):
            release_conn()


def _parse_exact_label_selector(selector: str | None) -> list[tuple[str, str]]:
    """Parse the API's deliberately narrow, fail-closed selector subset."""
    if selector is None:
        return []

    requirements: list[tuple[str, str]] = []
    for raw_clause in selector.split(","):
        clause = raw_clause.strip()
        if not clause or clause.count("=") != 1:
            raise ValueError(
                "Label selectors must be comma-separated exact matches in key=value form"
            )

        key, value = (part.strip() for part in clause.split("=", 1))
        if "/" in key:
            if key.count("/") != 1:
                raise ValueError(f"Invalid label key in selector: {key!r}")
            prefix, name = key.split("/", 1)
            valid_prefix = len(prefix) <= 253 and all(
                _DNS_LABEL_RE.fullmatch(part) for part in prefix.split(".")
            )
        else:
            name = key
            valid_prefix = True

        if not valid_prefix or not _LABEL_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid label key in selector: {key!r}")
        if value and not _LABEL_NAME_RE.fullmatch(value):
            raise ValueError(f"Invalid label value in selector for {key!r}")
        requirements.append((key, value))

    return requirements


def _labels_match(labels: Any, requirements: list[tuple[str, str]]) -> bool:
    """Return whether all parsed exact-match requirements are satisfied."""
    if not isinstance(labels, dict):
        return False
    return all(labels.get(key) == value for key, value in requirements)


@router.get("")
async def list_jobs(
    namespace: str | None = Query(None, description="Filter by namespace"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=1000, description="Maximum number of jobs to return"),
    offset: int = Query(0, ge=0, description="Number of jobs to skip"),
    sort: str = Query("createdAt:desc", description="Sort field and order (field:asc|desc)"),
    label_selector: str | None = Query(
        None,
        max_length=1024,
        description="Comma-separated exact-match label filters (key=value only)",
    ),
) -> Response:
    """List Kubernetes Jobs with pagination and filtering."""
    processor = _check_processor()

    try:
        selector_requirements = _parse_exact_label_selector(label_selector)
        all_jobs = await processor.list_jobs(namespace=namespace, status_filter=status)

        if selector_requirements:
            all_jobs = [
                job
                for job in all_jobs
                if _labels_match(job.get("metadata", {}).get("labels", {}), selector_requirements)
            ]

        sort_field, sort_order = "createdAt", "desc"
        if ":" in sort:
            sort_field, sort_order = sort.split(":", 1)

        def get_sort_key(job: dict[str, Any]) -> Any:
            if sort_field == "createdAt":
                return job.get("metadata", {}).get("creationTimestamp", "")
            if sort_field == "name":
                return job.get("metadata", {}).get("name", "")
            if sort_field == "status":
                return job.get("status", {}).get("active", 0)
            return ""

        all_jobs.sort(key=get_sort_key, reverse=(sort_order == "desc"))

        total = len(all_jobs)
        paginated_jobs = all_jobs[offset : offset + limit]

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total,
            "count": len(paginated_jobs),
            "jobs": paginated_jobs,
        }

        return JSONResponse(status_code=200, content=response)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error listing jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.get("/{namespace}/{name}")
async def get_job(namespace: str, name: str) -> Response:
    """Get details of a specific Job."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        job = processor.batch_v1.read_namespaced_job(name=name, namespace=namespace)
        job_info = _parse_job_to_dict(job)

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            **job_info,
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except Exception as e:
        if "NotFound" in str(e) or "404" in str(e):
            raise HTTPException(
                status_code=404, detail=f"Job '{name}' not found in namespace '{namespace}'"
            ) from e
        logger.error(f"Error getting job: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.get("/{namespace}/{name}/logs")
async def get_job_logs(
    namespace: str,
    name: str,
    container: str | None = Query(None, description="Container name (for multi-container pods)"),
    tail: int = Query(100, ge=1, le=10000, description="Number of lines from the end"),
    previous: bool = Query(False, description="Get logs from previous terminated container"),
    since_seconds: int | None = Query(
        None, ge=1, description="Only return logs newer than N seconds"
    ),
    timestamps: bool = Query(False, description="Include timestamps in log lines"),
) -> Response:
    """Get logs from a Job's pods."""
    from kubernetes.client.rest import ApiException as K8sApiException

    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        try:
            processor.batch_v1.read_namespaced_job(name=name, namespace=namespace)
        except K8sApiException as e:
            if e.status == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"Job '{name}' not found in namespace '{namespace}'",
                ) from e
            raise

        pods = processor.core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={name}"
        )

        if not pods.items:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No pods found for job '{name}'. "
                    "The job may have completed and pods were cleaned up "
                    "(ttlSecondsAfterFinished). Use 'gco jobs get' to check job status."
                ),
            )

        sorted_pods = sorted(
            pods.items,
            key=lambda p: p.metadata.creation_timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        pod = sorted_pods[0]

        pod_phase = pod.status.phase if pod.status else "Unknown"
        if pod_phase == "Pending":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Pod '{pod.metadata.name}' is still Pending — logs are not yet available. "
                    "The node may still be provisioning. Use 'gco jobs events' to check."
                ),
            )

        log_kwargs: dict[str, Any] = {
            "name": pod.metadata.name,
            "namespace": namespace,
            "tail_lines": tail,
            "previous": previous,
            "timestamps": timestamps,
        }
        if container:
            log_kwargs["container"] = container
        if since_seconds:
            log_kwargs["since_seconds"] = since_seconds

        try:
            log_response = processor.core_v1.read_namespaced_pod_log(
                **log_kwargs, _preload_content=False
            )
            logs = _decode_pod_log_response(log_response)
        except K8sApiException as e:
            if e.status == 400:
                error_body = str(e.body) if e.body else str(e.reason)
                if "waiting" in error_body.lower() or "not found" in error_body.lower():
                    available = [c.name for c in pod.spec.containers]
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Logs not available: {error_body}. "
                            f"Pod phase: {pod_phase}. "
                            f"Available containers: {available}"
                        ),
                    ) from e
                raise HTTPException(status_code=400, detail=f"Bad request: {error_body}") from e
            raise

        available_containers = [c.name for c in pod.spec.containers]
        init_containers = [c.name for c in (pod.spec.init_containers or [])]

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "job_name": name,
            "namespace": namespace,
            "pod_name": pod.metadata.name,
            "container": container or (available_containers[0] if available_containers else None),
            "available_containers": available_containers,
            "init_containers": init_containers,
            "previous": previous,
            "tail_lines": tail,
            "logs": logs,
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except K8sApiException as e:
        logger.error(f"Kubernetes API error getting job logs: {e.status} {e.reason}")
        raise HTTPException(
            status_code=502, detail=f"Kubernetes API error: {e.status} {e.reason}"
        ) from e
    except Exception as e:
        logger.error(f"Error getting job logs: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.get("/{namespace}/{name}/events")
async def get_job_events(namespace: str, name: str) -> Response:
    """Get events related to a Job."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        field_selector = f"involvedObject.name={name},involvedObject.kind=Job"
        job_events = processor.core_v1.list_namespaced_event(
            namespace=namespace, field_selector=field_selector
        )

        pods = processor.core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={name}"
        )

        pod_events = []
        for pod in pods.items:
            field_selector = f"involvedObject.name={pod.metadata.name},involvedObject.kind=Pod"
            events = processor.core_v1.list_namespaced_event(
                namespace=namespace, field_selector=field_selector
            )
            pod_events.extend(events.items)

        all_events = [_parse_event_to_dict(e) for e in job_events.items]
        all_events.extend([_parse_event_to_dict(e) for e in pod_events])
        all_events.sort(
            key=lambda e: e.get("lastTimestamp") or e.get("firstTimestamp") or "", reverse=True
        )

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "job_name": name,
            "namespace": namespace,
            "count": len(all_events),
            "events": all_events,
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting job events: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.get("/{namespace}/{name}/pods")
async def get_job_pods(namespace: str, name: str) -> Response:
    """Get pods belonging to a Job."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        pods = processor.core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={name}"
        )
        pod_list = [_parse_pod_to_dict(pod) for pod in pods.items]

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "job_name": name,
            "namespace": namespace,
            "count": len(pod_list),
            "pods": pod_list,
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting job pods: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.get("/{namespace}/{name}/pods/{pod_name}/logs")
async def get_pod_logs(
    namespace: str,
    name: str,
    pod_name: str,
    container: str | None = Query(None, description="Container name"),
    tail: int = Query(100, ge=1, le=10000, description="Number of lines from the end"),
    previous: bool = Query(False, description="Get logs from previous terminated container"),
) -> Response:
    """Get logs from a specific pod belonging to a Job."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        pod = processor.core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        job_name_label = pod.metadata.labels.get("job-name")
        if job_name_label != name:
            raise HTTPException(
                status_code=400, detail=f"Pod '{pod_name}' does not belong to job '{name}'"
            )

        log_kwargs: dict[str, Any] = {
            "name": pod_name,
            "namespace": namespace,
            "tail_lines": tail,
            "previous": previous,
        }
        if container:
            log_kwargs["container"] = container

        log_response = processor.core_v1.read_namespaced_pod_log(
            **log_kwargs, _preload_content=False
        )
        logs = _decode_pod_log_response(log_response)

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "job_name": name,
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
            "logs": logs,
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except Exception as e:
        if "NotFound" in str(e) or "404" in str(e):
            raise HTTPException(status_code=404, detail=f"Pod '{pod_name}' not found") from e
        logger.error(f"Error getting pod logs: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.get("/{namespace}/{name}/metrics")
async def get_job_metrics(namespace: str, name: str) -> Response:
    """Get resource usage metrics for a Job's pods."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        pods = processor.core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={name}"
        )

        if not pods.items:
            raise HTTPException(status_code=404, detail=f"No pods found for job '{name}'")

        pod_metrics = []
        total_cpu_millicores = 0
        total_memory_bytes = 0

        try:
            for pod in pods.items:
                try:
                    metrics = processor.custom_objects.get_namespaced_custom_object(
                        group="metrics.k8s.io",
                        version="v1beta1",
                        namespace=namespace,
                        plural="pods",
                        name=pod.metadata.name,
                    )

                    containers_metrics = []
                    for container in metrics.get("containers", []):
                        cpu_str = container.get("usage", {}).get("cpu", "0")
                        memory_str = container.get("usage", {}).get("memory", "0")

                        cpu_millicores = 0
                        if cpu_str.endswith("n"):
                            cpu_millicores = int(cpu_str[:-1]) // 1000000
                        elif cpu_str.endswith("m"):
                            cpu_millicores = int(cpu_str[:-1])
                        else:
                            cpu_millicores = int(cpu_str) * 1000

                        memory_bytes = 0
                        if memory_str.endswith("Ki"):
                            memory_bytes = int(memory_str[:-2]) * 1024
                        elif memory_str.endswith("Mi"):
                            memory_bytes = int(memory_str[:-2]) * 1024 * 1024
                        elif memory_str.endswith("Gi"):
                            memory_bytes = int(memory_str[:-2]) * 1024 * 1024 * 1024
                        else:
                            memory_bytes = int(memory_str)

                        total_cpu_millicores += cpu_millicores
                        total_memory_bytes += memory_bytes

                        containers_metrics.append(
                            {
                                "name": container.get("name"),
                                "cpu_millicores": cpu_millicores,
                                "memory_bytes": memory_bytes,
                                "memory_mib": round(memory_bytes / (1024 * 1024), 2),
                            }
                        )

                    pod_metrics.append(
                        {"pod_name": pod.metadata.name, "containers": containers_metrics}
                    )

                except Exception as e:
                    logger.warning(f"Could not get metrics for pod {pod.metadata.name}: {e}")
                    pod_metrics.append(
                        {"pod_name": pod.metadata.name, "error": "Metrics not available"}
                    )

        except Exception as e:
            logger.warning(f"Metrics API not available: {e}")

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "job_name": name,
            "namespace": namespace,
            "summary": {
                "total_cpu_millicores": total_cpu_millicores,
                "total_memory_bytes": total_memory_bytes,
                "total_memory_mib": round(total_memory_bytes / (1024 * 1024), 2),
                "pod_count": len(pods.items),
            },
            "pods": pod_metrics,
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting job metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.delete("/{namespace}/{name}")
async def delete_job(
    namespace: str,
    name: str,
    expected_uid: str | None = Query(
        None,
        min_length=1,
        max_length=128,
        description="Optional immutable Kubernetes UID deletion precondition",
    ),
) -> Response:
    """Delete a Job, optionally requiring its immutable Kubernetes UID."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        delete_kwargs: dict[str, Any] = {
            "name": name,
            "namespace": namespace,
        }
        deleted_uid: str | None = None
        if expected_uid is not None:
            current = processor.batch_v1.read_namespaced_job(name=name, namespace=namespace)
            deleted_uid = str(getattr(current.metadata, "uid", "") or "")
            if deleted_uid != expected_uid:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Job '{name}' UID changed; expected {expected_uid!r}, "
                        f"found {deleted_uid or 'unknown'!r}"
                    ),
                )
            delete_kwargs["body"] = kubernetes_client.V1DeleteOptions(
                propagation_policy="Background",
                preconditions=kubernetes_client.V1Preconditions(uid=expected_uid),
            )
        else:
            delete_kwargs["propagation_policy"] = "Background"
        processor.batch_v1.delete_namespaced_job(**delete_kwargs)

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "job_name": name,
            "namespace": namespace,
            "uid": deleted_uid,
            "status": "deleted",
            "message": "Job deleted successfully",
        }

        return JSONResponse(status_code=200, content=response)

    except HTTPException:
        raise
    except Exception as e:
        status = getattr(e, "status", None)
        if status == 409:
            raise HTTPException(
                status_code=409,
                detail=f"Job '{name}' changed before its UID-preconditioned delete",
            ) from e
        if status == 404 or "NotFound" in str(e) or "404" in str(e):
            raise HTTPException(
                status_code=404, detail=f"Job '{name}' not found in namespace '{namespace}'"
            ) from e
        logger.error(f"Error deleting job: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.delete("")
async def bulk_delete_jobs(request: BulkDeleteRequest) -> Response:
    """Bulk delete jobs based on filters."""

    processor = _check_processor()

    try:
        selector_requirements = _parse_exact_label_selector(request.label_selector)
        status_filter = request.status.value if request.status else None
        all_jobs = await processor.list_jobs(
            namespace=request.namespace, status_filter=status_filter
        )

        jobs_to_delete = []
        cutoff_time = None
        if request.older_than_days:
            cutoff_time = datetime.now(UTC) - timedelta(days=request.older_than_days)

        for job in all_jobs:
            if cutoff_time:
                created_str = job.get("metadata", {}).get("creationTimestamp")
                if created_str:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    # Kubernetes normally returns an aware RFC3339 timestamp,
                    # but older fixtures/clients may provide a naive value.
                    # Normalize both forms to aware UTC before comparison.
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    else:
                        created = created.astimezone(UTC)
                    if created > cutoff_time:
                        continue

            if selector_requirements and not _labels_match(
                job.get("metadata", {}).get("labels", {}), selector_requirements
            ):
                continue

            jobs_to_delete.append(job)

        deleted_jobs = []
        failed_jobs = []

        if not request.dry_run:
            for job in jobs_to_delete:
                job_name = job.get("metadata", {}).get("name")
                job_namespace = job.get("metadata", {}).get("namespace")
                try:
                    processor.batch_v1.delete_namespaced_job(
                        name=job_name, namespace=job_namespace, propagation_policy="Background"
                    )
                    deleted_jobs.append({"name": job_name, "namespace": job_namespace})
                except Exception as e:
                    failed_jobs.append(
                        {"name": job_name, "namespace": job_namespace, "error": str(e)}
                    )

        response: dict[str, Any] = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "dry_run": request.dry_run,
            "total_matched": len(jobs_to_delete),
            "deleted_count": len(deleted_jobs),
            "failed_count": len(failed_jobs),
            "jobs": (
                [
                    {
                        "name": j.get("metadata", {}).get("name"),
                        "namespace": j.get("metadata", {}).get("namespace"),
                    }
                    for j in jobs_to_delete
                ]
                if request.dry_run
                else deleted_jobs
            ),
            "failed": failed_jobs if failed_jobs else None,
        }

        return JSONResponse(status_code=200, content=response)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Error bulk deleting jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.post("/{namespace}/{name}/retry")
async def retry_job(namespace: str, name: str) -> Response:
    """Retry a failed job by creating a new job from its spec."""
    processor = _check_processor()
    _check_namespace(namespace, processor)

    try:
        try:
            original_job = processor.batch_v1.read_namespaced_job(name=name, namespace=namespace)
        except Exception as e:
            if "NotFound" in str(e) or "404" in str(e):
                raise HTTPException(
                    status_code=404, detail=f"Job '{name}' not found in namespace '{namespace}'"
                ) from e
            raise

        new_name = f"{name}-retry-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

        new_job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": new_name,
                "namespace": namespace,
                "labels": {
                    **(original_job.metadata.labels or {}),
                    "gco.io/retry-of": name,
                },
                "annotations": {
                    **(original_job.metadata.annotations or {}),
                    "gco.io/original-job": name,
                },
            },
            "spec": {
                "parallelism": original_job.spec.parallelism,
                "completions": original_job.spec.completions,
                "backoffLimit": original_job.spec.backoff_limit,
                "template": original_job.spec.template.to_dict(),
            },
        }

        spec_dict = new_job_manifest.get("spec", {})
        if isinstance(spec_dict, dict):
            template_dict = spec_dict.get("template", {})
            if isinstance(template_dict, dict) and "status" in template_dict:
                del template_dict["status"]

        submission_request = ManifestSubmissionRequest(
            manifests=[new_job_manifest], namespace=namespace, dry_run=False, validate=True
        )

        result = await processor.process_manifest_submission(submission_request)

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "original_job": name,
            "new_job": new_name,
            "namespace": namespace,
            "success": result.success,
            "message": (
                "Job retry created successfully" if result.success else "Failed to create retry job"
            ),
            "errors": result.errors,
        }

        status_code = 201 if result.success else 400
        return JSONResponse(status_code=status_code, content=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrying job: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e
