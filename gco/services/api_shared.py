"""
Shared state, models, and helpers for the Manifest API routers.

This module holds the global state (manifest processor, DynamoDB stores),
Pydantic request/response models, and helper functions used across all API
route modules. Centralizing them here avoids circular imports between
manifest_api.py and the routers.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import HTTPException
from kubernetes.client.models import CoreV1Event, V1Job, V1Pod
from pydantic import BaseModel, Field

from gco.services.manifest_processor import ManifestProcessor
from gco.services.metrics_publisher import ManifestProcessorMetrics
from gco.services.template_store import (
    JobStore,
    TemplateStore,
    WebhookStore,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared enums and Pydantic models
# ---------------------------------------------------------------------------


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WebhookEvent(StrEnum):
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    JOB_STARTED = "job.started"


class ManifestSubmissionAPIRequest(BaseModel):
    """API model for manifest submission requests."""

    manifests: list[dict[str, Any]] = Field(
        ..., description="List of Kubernetes manifests to apply"
    )
    namespace: str | None = Field(
        None, description="Default namespace for resources without namespace specified"
    )
    dry_run: bool = Field(False, description="If true, validate manifests without applying them")
    validate_manifests: bool = Field(
        True, description="If true, perform validation checks on manifests", alias="validate"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "manifests": [
                    {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "example"}}
                ],
                "namespace": "gco-jobs",
                "dry_run": False,
            }
        }
    }


class ResourceIdentifier(BaseModel):
    api_version: str = Field(..., description="Kubernetes API version (e.g., 'apps/v1')")
    kind: str = Field(..., description="Kubernetes resource kind (e.g., 'Deployment')")
    name: str = Field(..., description="Resource name")
    namespace: str = Field(..., description="Resource namespace")


class BulkDeleteRequest(BaseModel):
    namespace: str | None = Field(None, description="Filter by namespace")
    status: JobStatus | None = Field(None, description="Filter by status")
    older_than_days: int | None = Field(
        None, description="Delete jobs older than N days", ge=1, le=365
    )
    label_selector: str | None = Field(
        None,
        description="Comma-separated exact-match label filters (key=value only)",
        max_length=1024,
    )
    dry_run: bool = Field(False, description="If true, only return what would be deleted")

    model_config = {
        "json_schema_extra": {
            "example": {
                "namespace": "gco-jobs",
                "status": "completed",
                "older_than_days": 7,
                "dry_run": False,
            }
        }
    }


class JobTemplateRequest(BaseModel):
    name: str = Field(..., description="Template name", min_length=1, max_length=63)
    description: str | None = Field(None, description="Template description")
    manifest: dict[str, Any] = Field(..., description="Job manifest template")
    parameters: dict[str, Any] | None = Field(None, description="Default parameter values")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "gpu-training-template",
                "description": "Template for GPU training jobs",
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "{{name}}"},
                },
                "parameters": {"image": "pytorch/pytorch:latest"},
            }
        }
    }


class JobFromTemplateRequest(BaseModel):
    name: str = Field(..., description="Job name", min_length=1, max_length=63)
    namespace: str = Field("gco-jobs", description="Target namespace")
    parameters: dict[str, Any] | None = Field(None, description="Parameter overrides")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "my-training-job",
                "namespace": "gco-jobs",
                "parameters": {"image": "my-custom-image:v1"},
            }
        }
    }


class WebhookRequest(BaseModel):
    url: str = Field(..., description="Webhook URL to call")
    events: list[WebhookEvent] = Field(..., description="Events to subscribe to")
    namespace: str | None = Field(None, description="Filter by namespace (optional)")
    secret: str | None = Field(None, description="Secret for HMAC signature (optional)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "url": "https://example.com/webhook",
                "events": ["job.completed", "job.failed"],
                "namespace": "gco-jobs",
            }
        }
    }


class QueuedJobRequest(BaseModel):
    manifest: dict[str, Any] = Field(..., description="Kubernetes job manifest")
    target_region: str = Field(..., description="Target region for job execution")
    namespace: str = Field("gco-jobs", description="Kubernetes namespace")
    priority: int = Field(0, description="Job priority (higher = more important)", ge=0, le=100)
    labels: dict[str, str] | None = Field(None, description="Optional labels for filtering")
    max_spot_price: float | None = Field(
        None,
        gt=0,
        description=(
            "Optional spot price cap in USD/hour. The job is not dispatched "
            "until the current spot price of spot_instance_type in the target "
            "region drops to or below this value. Requires spot_instance_type."
        ),
    )
    spot_instance_type: str | None = Field(
        None,
        description=(
            "EC2 instance type whose spot price gates dispatch (e.g. "
            "g5.xlarge). Requires max_spot_price."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "my-training-job"},
                },
                "target_region": "us-east-1",
                "namespace": "gco-jobs",
                "priority": 10,
                "max_spot_price": 0.5,
                "spot_instance_type": "g5.xlarge",
            }
        }
    }


class PaginatedResponse(BaseModel):
    total: int = Field(..., description="Total number of items")
    limit: int = Field(..., description="Items per page")
    offset: int = Field(..., description="Current offset")
    has_more: bool = Field(..., description="Whether more items exist")


class ErrorResponse(BaseModel):
    error: str = Field(..., description="Error type")
    detail: str = Field(..., description="Error details")
    timestamp: str = Field(..., description="Error timestamp")


# ---------------------------------------------------------------------------
# Global state — populated by the lifespan handler in manifest_api.py
# ---------------------------------------------------------------------------
manifest_processor: ManifestProcessor | None = None
manifest_metrics: ManifestProcessorMetrics | None = None
template_store: TemplateStore | None = None
webhook_store: WebhookStore | None = None
job_store: JobStore | None = None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _check_processor() -> ManifestProcessor:
    """Check if manifest processor is initialized and return it."""
    # Import at call-time to read the global that lifespan populates on
    # the manifest_api module (tests also patch it there).
    from gco.services import manifest_api as _api

    if _api.manifest_processor is None:
        raise HTTPException(status_code=503, detail="Manifest processor not initialized")
    return _api.manifest_processor


def _check_namespace(namespace: str, processor: ManifestProcessor) -> None:
    """Check if namespace is allowed."""
    if namespace not in processor.allowed_namespaces:
        raise HTTPException(
            status_code=403,
            detail=f"Namespace '{namespace}' not allowed. Allowed: {list(processor.allowed_namespaces)}",
        )


def _parse_job_to_dict(job: V1Job) -> dict[str, Any]:
    """Parse a Kubernetes Job object to a dictionary."""
    metadata = job.metadata
    status = job.status
    spec = job.spec

    conditions = status.conditions or []
    computed_status = "pending"
    for condition in conditions:
        if condition.type == "Complete" and condition.status == "True":
            computed_status = "succeeded"
            break
        if condition.type == "Failed" and condition.status == "True":
            computed_status = "failed"
            break

    if computed_status == "pending" and (status.active or 0) > 0:
        computed_status = "running"

    # Pull container image refs from the pod template so callers (e.g.
    # the orphan-image cross-reference) can identify which ECR images
    # are still in use without a second round-trip per job.
    template = getattr(spec, "template", None)
    pod_spec = getattr(template, "spec", None) if template is not None else None
    containers = getattr(pod_spec, "containers", None) or []
    init_containers = getattr(pod_spec, "init_containers", None) or []
    container_specs = [
        {"name": getattr(c, "name", ""), "image": getattr(c, "image", "")} for c in containers
    ]
    init_container_specs = [
        {"name": getattr(c, "name", ""), "image": getattr(c, "image", "")} for c in init_containers
    ]

    return {
        "metadata": {
            "name": metadata.name,
            "namespace": metadata.namespace,
            "creationTimestamp": (
                metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
            ),
            "labels": metadata.labels or {},
            "annotations": metadata.annotations or {},
            "uid": metadata.uid,
        },
        "spec": {
            "parallelism": spec.parallelism,
            "completions": spec.completions,
            "backoffLimit": spec.backoff_limit,
            "template": {
                "spec": {
                    "containers": container_specs,
                    "initContainers": init_container_specs,
                },
            },
        },
        "status": {
            "active": status.active or 0,
            "succeeded": status.succeeded or 0,
            "failed": status.failed or 0,
            "startTime": status.start_time.isoformat() if status.start_time else None,
            "completionTime": (
                status.completion_time.isoformat() if status.completion_time else None
            ),
            "conditions": [
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                    "lastTransitionTime": (
                        c.last_transition_time.isoformat() if c.last_transition_time else None
                    ),
                }
                for c in conditions
            ],
        },
        "computed_status": computed_status,
    }


def _parse_pod_to_dict(pod: V1Pod) -> dict[str, Any]:
    """Parse a Kubernetes Pod object to a dictionary."""
    metadata = pod.metadata
    status = pod.status
    spec = pod.spec

    container_statuses = []
    for cs in status.container_statuses or []:
        container_status: dict[str, Any] = {
            "name": cs.name,
            "ready": cs.ready,
            "restartCount": cs.restart_count,
            "image": cs.image,
        }
        if cs.state:
            if cs.state.running:
                container_status["state"] = "running"
                container_status["startedAt"] = (
                    cs.state.running.started_at.isoformat() if cs.state.running.started_at else None
                )
            elif cs.state.waiting:
                container_status["state"] = "waiting"
                container_status["reason"] = cs.state.waiting.reason
            elif cs.state.terminated:
                container_status["state"] = "terminated"
                container_status["exitCode"] = cs.state.terminated.exit_code
                container_status["reason"] = cs.state.terminated.reason
        container_statuses.append(container_status)

    init_container_statuses = []
    for cs in status.init_container_statuses or []:
        init_status = {
            "name": cs.name,
            "ready": cs.ready,
            "restartCount": cs.restart_count,
        }
        init_container_statuses.append(init_status)

    return {
        "metadata": {
            "name": metadata.name,
            "namespace": metadata.namespace,
            "creationTimestamp": (
                metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
            ),
            "labels": metadata.labels or {},
            "uid": metadata.uid,
        },
        "spec": {
            "nodeName": spec.node_name,
            "containers": [{"name": c.name, "image": c.image} for c in spec.containers],
            "initContainers": [
                {"name": c.name, "image": c.image} for c in (spec.init_containers or [])
            ],
        },
        "status": {
            "phase": status.phase,
            "hostIP": status.host_ip,
            "podIP": status.pod_ip,
            "startTime": status.start_time.isoformat() if status.start_time else None,
            "containerStatuses": container_statuses,
            "initContainerStatuses": init_container_statuses,
        },
    }


def _parse_event_to_dict(event: CoreV1Event) -> dict[str, Any]:
    """Parse a Kubernetes Event object to a dictionary."""
    return {
        "type": event.type,
        "reason": event.reason,
        "message": event.message,
        "count": event.count or 1,
        "firstTimestamp": (event.first_timestamp.isoformat() if event.first_timestamp else None),
        "lastTimestamp": (event.last_timestamp.isoformat() if event.last_timestamp else None),
        "source": {
            "component": event.source.component if event.source else None,
            "host": event.source.host if event.source else None,
        },
        "involvedObject": {
            "kind": event.involved_object.kind if event.involved_object else None,
            "name": event.involved_object.name if event.involved_object else None,
            "namespace": event.involved_object.namespace if event.involved_object else None,
        },
    }


def _apply_template_parameters(
    manifest: dict[str, Any], parameters: dict[str, Any]
) -> dict[str, Any]:
    """Apply parameter substitutions to a manifest template."""
    import json
    import re

    manifest_str = json.dumps(manifest)
    for key, value in parameters.items():
        pattern = r"\{\{\s*" + re.escape(key) + r"\s*\}\}"
        manifest_str = re.sub(pattern, str(value), manifest_str)
    result: dict[str, Any] = json.loads(manifest_str)
    return result
