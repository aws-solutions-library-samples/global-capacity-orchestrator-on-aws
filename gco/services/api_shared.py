"""
Shared state, models, and helpers for the Manifest API routers.

This module holds the global state (manifest processor, DynamoDB stores),
Pydantic request/response models, and helper functions used across all API
route modules. Centralizing them here avoids circular imports between
manifest_api.py and the routers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import HTTPException
from kubernetes.client.models import CoreV1Event, V1Job, V1Pod
from pydantic import BaseModel, Field

from gco.hyperpod_config import (
    CAPACITY_PROVIDER_HYPERPOD,
    CAPACITY_PROVIDER_LABEL,
    DEEP_HEALTH_CHECK_STATUS_LABEL,
    INSTANCE_GROUP_LABEL,
    NODE_HEALTH_STATUS_LABEL,
)
from gco.services.manifest_processor import ManifestProcessor
from gco.services.metrics_publisher import ManifestProcessorMetrics
from gco.services.request_context import current_request_id
from gco.services.structured_logging import sanitize_log_value
from gco.services.template_store import (
    JobStore,
    TemplateStore,
    WebhookStore,
)

logger = logging.getLogger(__name__)


def internal_server_error(context: str, error: Exception) -> HTTPException:
    """Log the full exception server-side; return a generic, correlatable 500.

    The client-facing detail carries only the constant message plus the
    request's correlation id — never exception text (the information-exposure
    shape CodeQL flagged across the jobs routes). The id also lands in the
    paired log line, so an operator can grep the service logs for exactly
    the failure a caller reported. Callers raise the returned exception with
    ``from error`` so the traceback chain stays intact.
    """
    request_id = current_request_id()
    logger.error("Error %s (request-id %s): %s", context, request_id, error)
    return HTTPException(
        status_code=500,
        detail=f"Internal server error (request-id: {request_id})",
    )


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

    # A pod being deleted carries deletionTimestamp for its grace period and,
    # when a disruption (eviction, preemption, taint) is what is deleting it,
    # the DisruptionTarget condition. Both are the only in-flight trace of an
    # eviction: once the grace period ends the pod is gone and its events are
    # all that remains (GET .../events keeps returning those; see
    # _collect_job_events). Guarded by type so a partially populated or
    # malformed object cannot put a non-serializable value in the response.
    deletion_timestamp = getattr(metadata, "deletion_timestamp", None)

    return {
        "metadata": {
            "name": metadata.name,
            "namespace": metadata.namespace,
            "creationTimestamp": (
                metadata.creation_timestamp.isoformat() if metadata.creation_timestamp else None
            ),
            "deletionTimestamp": (
                deletion_timestamp.isoformat() if isinstance(deletion_timestamp, datetime) else None
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
            "disruptionTarget": _pod_disruption_target(status),
        },
    }


# ---------------------------------------------------------------------------
# Node placement reporting
# ---------------------------------------------------------------------------

# The instance type a pod actually landed on. A workload constrained to a set
# of interchangeable instance types (nodeAffinity ``In: [...]``) is placed by
# Karpenter within that set, so the manifest only records what the run was
# authorized to use — this label records what it used.
NODE_INSTANCE_TYPE_LABEL = "node.kubernetes.io/instance-type"

# Spot vs on-demand. Distinguishing the two matters for reconciling observed
# cost against an estimate, and for explaining an interrupted run.
NODE_CAPACITY_TYPE_LABEL = "karpenter.sh/capacity-type"

# A SageMaker HyperPod node carries no Karpenter label; GCO labels it with the
# capacity provider instead (see gco/hyperpod_config.py), and the placement
# report calls its capacity type ``hyperpod`` so the three kinds of node a
# pod can land on read alike.
NODE_CAPACITY_TYPE_HYPERPOD = CAPACITY_PROVIDER_HYPERPOD

# Reported alongside the two above because they cost nothing extra (they come
# from the same Node read) and answer the immediate follow-up questions:
# which AZ, which CPU architecture, which Karpenter NodePool provisioned it,
# or which HyperPod instance group and what HyperPod's health checks say.
_REPORTED_NODE_LABELS: tuple[str, ...] = (
    NODE_INSTANCE_TYPE_LABEL,
    NODE_CAPACITY_TYPE_LABEL,
    "topology.kubernetes.io/zone",
    "topology.kubernetes.io/region",
    "kubernetes.io/arch",
    "karpenter.sh/nodepool",
    CAPACITY_PROVIDER_LABEL,
    INSTANCE_GROUP_LABEL,
    NODE_HEALTH_STATUS_LABEL,
    DEEP_HEALTH_CHECK_STATUS_LABEL,
)


def _node_capacity_type(labels: Mapping[str, str]) -> str | None:
    """``spot`` / ``on-demand`` from Karpenter's label, ``hyperpod`` from GCO's provider label."""
    karpenter = labels.get(NODE_CAPACITY_TYPE_LABEL)
    if karpenter:
        return karpenter
    if labels.get(CAPACITY_PROVIDER_LABEL) == CAPACITY_PROVIDER_HYPERPOD:
        return NODE_CAPACITY_TYPE_HYPERPOD
    return None


def _empty_scheduling_info() -> dict[str, Any]:
    """The shape returned when nothing about placement is known yet.

    Every key the collector can fill in is present here, so a caller sees the
    same field set whether placement resolved, no pods exist yet, or the pod
    listing itself failed (``node_lookup_error`` says which).
    """
    return {
        "node_name": None,
        "node_instance_type": None,
        "node_capacity_type": None,
        "node_labels": {},
        "nodes": [],
        "pod_phase": None,
        "pod_phases": {},
        "scheduled_pods": 0,
        "unscheduled_pods": 0,
        "unscheduled": [],
        "node_lookup_error": None,
    }


def _pod_scheduled_condition(status: Any) -> dict[str, Any]:
    """The ``PodScheduled`` condition of a pod, reduced to its reason and message.

    Kubernetes already states why a pod is not placed ("0/6 nodes are
    available: ...", a taint nobody tolerates, Karpenter's nomination churn)
    on this condition; surfacing it here saves the second ``events`` call a
    caller otherwise needs to learn the same thing. Only the ``False`` state
    carries a diagnosis; ``True`` and ``Unknown`` report nothing, as does a pod
    whose conditions have not been populated yet. Never raises: every field
    is taken with ``getattr`` and kept only when it has the expected type.
    """
    empty: dict[str, Any] = {"reason": None, "message": None, "since": None}
    condition = _pod_condition(status, "PodScheduled")
    if condition is None or getattr(condition, "status", None) != "False":
        return empty
    return _condition_summary(condition)


def _pod_condition(status: Any, condition_type: str) -> Any | None:
    """The pod condition of ``condition_type``, or None when absent or malformed."""
    conditions = getattr(status, "conditions", None) if status is not None else None
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if getattr(condition, "type", None) == condition_type:
            return condition
    return None


def _condition_summary(condition: Any) -> dict[str, Any]:
    """A condition reduced to its reason, message, and last transition time.

    Every field is taken with ``getattr`` and kept only when it has the
    expected type, so a malformed condition yields ``None`` values instead of
    an unserializable response.
    """
    reason = getattr(condition, "reason", None)
    message = getattr(condition, "message", None)
    transition = getattr(condition, "last_transition_time", None)
    since = None
    if transition is not None:
        try:
            since = transition.isoformat()
        except Exception:  # pragma: no cover - defensive
            since = None
    return {
        "reason": reason if isinstance(reason, str) else None,
        "message": message if isinstance(message, str) else None,
        "since": since if isinstance(since, str) else None,
    }


def _pod_disruption_target(status: Any) -> dict[str, Any] | None:
    """The pod's ``DisruptionTarget`` condition while a disruption is deleting it.

    Kubernetes stamps this condition (status ``True``) on a pod that is being
    deleted by a disruption rather than by its owner: an Eviction API call —
    which is how Karpenter and EKS Auto Mode drain a node they are about to
    terminate — scheduler preemption, taint-based deletion, or node
    shutdown. The reason names the mechanism (``EvictionByEvictionAPI``,
    ``PreemptionByScheduler``, ``DeletionByTaintManager``, …); it never names
    the cause behind the node's departure, which lives on the Node's and
    NodeClaim's events. Reported alongside ``deletionTimestamp`` so a caller
    can recognize an eviction in progress before the pod disappears.
    ``None`` when the condition is absent or not ``True``.
    """
    condition = _pod_condition(status, "DisruptionTarget")
    if condition is None or getattr(condition, "status", None) != "True":
        return None
    return _condition_summary(condition)


def _parse_node_to_dict(node: Any, name: str) -> dict[str, Any]:
    """Reduce a Kubernetes Node to the placement facts callers ask for.

    ``name`` is the node name the pod reported, which is also the key this
    Node was read by — carrying it through keeps the pod/node join exact
    regardless of what the Node object echoes back.

    Label values are accepted only when they are genuinely strings, so a
    malformed or partially-populated Node cannot put a non-serializable value
    into the response and turn a successful job read into a 500.
    """
    metadata = getattr(node, "metadata", None)
    raw_labels = getattr(metadata, "labels", None) if metadata is not None else None
    labels: dict[str, str] = (
        {k: v for k, v in raw_labels.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_labels, dict)
        else {}
    )
    return {
        "name": name,
        "instance_type": labels.get(NODE_INSTANCE_TYPE_LABEL),
        "capacity_type": _node_capacity_type(labels),
        "labels": {key: labels[key] for key in _REPORTED_NODE_LABELS if labels.get(key)},
    }


def _collect_pod_scheduling(core_v1: Any, pods: list[V1Pod]) -> dict[str, Any]:
    """Report which nodes a workload's pods landed on, and each node's hardware.

    One Node read per *distinct* node, so the common single-pod job costs
    exactly one extra API call on a path already talking to the cluster.

    ``node_name`` / ``node_instance_type`` / ``node_capacity_type`` describe
    the earliest-created scheduled pod, which is stable for the life of the
    workload even as retries add later pods. ``nodes`` carries every node
    involved, with the pods on each, so a retried job that moved between
    instance types is still fully described.

    The pods' own state is reported next to the placement, because the Job's
    status alone cannot tell "the container is training" from "a Job object
    exists and nothing is placed": a Job whose only pod cannot be scheduled
    still counts as active. ``pod_phases`` counts every pod by phase;
    ``pod_phase`` is the one phase they all share (``None`` as soon as they
    disagree, so it is never a summary that hides a Pending pod behind a
    Running one); ``scheduled_pods`` / ``unscheduled_pods`` split them by
    whether a node has been assigned; and ``unscheduled`` names each unplaced
    pod with the ``PodScheduled`` condition's reason and message, which is
    where Kubernetes states why it is waiting.

    Never raises. A Node read that is refused (no RBAC) or 404s (node already
    reclaimed) leaves the instance type ``None`` and records why in
    ``node_lookup_error`` — an absent value that says so is more useful than a
    guess that looks verified.
    """
    info = _empty_scheduling_info()
    phases: dict[str, int] = {}

    def _sort_key(pod: V1Pod) -> tuple[str, str]:
        """Deterministic (created, name) ordering that cannot raise.

        Both components are coerced to strings so a Node/Pod with a missing or
        unexpected timestamp still sorts instead of aborting the whole report.
        """
        metadata = getattr(pod, "metadata", None)
        created = getattr(metadata, "creation_timestamp", None) if metadata is not None else None
        name = getattr(metadata, "name", None) if metadata is not None else None
        stamp = ""
        if created is not None:
            try:
                candidate = created.isoformat()
            except Exception:  # pragma: no cover - defensive
                candidate = None
            stamp = candidate if isinstance(candidate, str) else ""
        return (stamp, name if isinstance(name, str) else "")

    ordered = sorted(pods or [], key=_sort_key)

    node_cache: dict[str, dict[str, Any]] = {}
    node_order: list[str] = []
    pods_by_node: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []

    for pod in ordered:
        metadata = getattr(pod, "metadata", None)
        spec = getattr(pod, "spec", None)
        status = getattr(pod, "status", None)
        node_name = getattr(spec, "node_name", None) if spec is not None else None
        pod_name = getattr(metadata, "name", None) if metadata is not None else None
        phase = getattr(status, "phase", None) if status is not None else None
        if not isinstance(pod_name, str):
            pod_name = None
        if not isinstance(phase, str):
            phase = None
        phases[phase or "Unknown"] = phases.get(phase or "Unknown", 0) + 1

        if not isinstance(node_name, str) or not node_name:
            # No Node read for a pod that has no node: the diagnosis lives on
            # the pod's own PodScheduled condition, which is already in hand.
            info["unscheduled_pods"] += 1
            info["unscheduled"].append(
                {"name": pod_name, "phase": phase, **_pod_scheduled_condition(status)}
            )
            continue

        info["scheduled_pods"] += 1

        if node_name not in node_cache:
            node_order.append(node_name)
            pods_by_node[node_name] = []
            try:
                node_cache[node_name] = _parse_node_to_dict(
                    core_v1.read_node(name=node_name), node_name
                )
            except Exception as exc:
                # The node name originates outside this process and the
                # Kubernetes error echoes it back; sanitize before logging so
                # neither can forge log entries (CWE-117).
                logger.warning(
                    "Could not read node %s: %s",
                    sanitize_log_value(node_name),
                    sanitize_log_value(exc),
                )
                errors.append(f"{node_name}: {exc}")
                node_cache[node_name] = {
                    "name": node_name,
                    "instance_type": None,
                    "capacity_type": None,
                    "labels": {},
                }

        pods_by_node[node_name].append({"name": pod_name, "phase": phase})

    info["pod_phases"] = phases
    if len(phases) == 1:
        # One phase shared by every pod is the only aggregate that cannot
        # mislead; anything mixed stays None and the caller reads the counts.
        (info["pod_phase"],) = phases

    if not node_order:
        return info

    info["nodes"] = [{**node_cache[name], "pods": pods_by_node[name]} for name in node_order]
    primary = info["nodes"][0]
    info["node_name"] = primary["name"]
    info["node_instance_type"] = primary["instance_type"]
    info["node_capacity_type"] = primary["capacity_type"]
    info["node_labels"] = dict(primary["labels"])
    if errors:
        info["node_lookup_error"] = "; ".join(errors)
    return info


def _parse_event_to_dict(event: CoreV1Event) -> dict[str, Any]:
    """Parse a Kubernetes Event object to a dictionary.

    ``eventTime`` is the timestamp an ``events.k8s.io/v1`` producer (the
    scheduler, among others) sets instead of ``firstTimestamp`` /
    ``lastTimestamp``; without it such an event has no time at all in the
    core-API view and sorts to the end.
    """
    event_time = getattr(event, "event_time", None)
    return {
        "type": event.type,
        "reason": event.reason,
        "message": event.message,
        "count": event.count or 1,
        "firstTimestamp": (event.first_timestamp.isoformat() if event.first_timestamp else None),
        "lastTimestamp": (event.last_timestamp.isoformat() if event.last_timestamp else None),
        "eventTime": event_time.isoformat() if isinstance(event_time, datetime) else None,
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


# ---------------------------------------------------------------------------
# Job event collection
# ---------------------------------------------------------------------------
#
# GET /api/v1/jobs/{ns}/{name}/events answers "what happened to this Job". The
# Job's own events and its pods' events do not say why a node went away: that
# is published by Karpenter / EKS Auto Mode on the Node and the NodeClaim
# (SpotInterrupted, InstanceTerminating, TerminatingOnInterruption,
# InstanceUnhealthy, DisruptionTerminating "Disrupting Node: Underutilized",
# …). The pod itself only gets "Evicted pod: <disruption reason>", which for an
# interruption reads "Forceful Termination" with no cause. And an eviction
# deletes the pod, so listing events only for the pods that still exist drops
# the evicted pod's events entirely while the apiserver still holds them (the
# event TTL, one hour by default).
#
# _collect_job_events therefore widens the read to every pod the Job has had
# within the TTL, every node those pods ran on, and the NodeClaim behind each
# node — and says which of those it covered, so a caller can tell "no node
# events" from "the node could not be resolved".

#: Where Kubernetes records events about cluster-scoped objects such as Nodes
#: and NodeClaims: client-go's recorder files an event under the involved
#: object's namespace and falls back to ``default`` when the object has none.
CLUSTER_SCOPED_EVENT_NAMESPACE = "default"

#: Karpenter's NodeClaim, the object behind every Karpenter / EKS Auto Mode
#: node. Its ``status.nodeName`` is the join to the Node once registered.
NODECLAIM_API_GROUP = "karpenter.sh"
NODECLAIM_API_VERSION = "v1"
NODECLAIM_PLURAL = "nodeclaims"

# The Job controller names every pod it creates or deletes in the Job's own
# events ("Created pod: <name>" / "Deleted pod: <name>"), which recovers the
# pods that no longer exist.
_JOB_POD_EVENT_RE = re.compile(r"^(?:Created|Deleted) pod: (\S+)$")

# The scheduler's placement record on the pod: "Successfully assigned
# <namespace>/<pod> to <node>". The only node reference a deleted pod leaves
# besides the kubelet's ``source.host`` on its container events.
_SCHEDULED_EVENT_RE = re.compile(r"^Successfully assigned \S+ to (\S+)$")

# Karpenter's nomination on the pod: "Pod should schedule on: nodeclaim/<name>,
# node/<name>" (either part may be absent). Names a NodeClaim that may already
# be deleted by the time anyone asks.
_NOMINATED_NODECLAIM_RE = re.compile(r"\bnodeclaim/([a-z0-9][-a-z0-9.]*)")
_NOMINATED_NODE_RE = re.compile(r"\bnode/([a-z0-9][-a-z0-9.]*)")

# Kubernetes appends five random characters to a generateName prefix and
# truncates the prefix to 58 so the result fits a 63-character name. The Job
# controller's prefix is "<job>-" (plain) or "<job>-<completion index>-"
# (Indexed). Matching that shape finds a deleted pod whose "Created pod" record
# has already expired — a pod that ran longer than the event TTL before its
# node was reclaimed, which is the training-job case.
_GENERATED_NAME_SUFFIX = r"[a-z0-9]{5}"
_MAX_GENERATED_NAME_PREFIX = 58


def _job_pod_name_pattern(job_name: str, *, indexed: bool) -> re.Pattern[str]:
    """The names the Job controller gives ``job_name``'s pods.

    ``indexed`` admits the ``<job>-<index>-<suffix>`` form too; it is only
    accepted when the Job says ``completionMode: Indexed``, because a
    CronJob's child Jobs (``<cronjob>-<schedule minute>``) would otherwise
    collide with it for a Job that shares the CronJob's name.
    """
    prefix = re.escape(f"{job_name}-"[:_MAX_GENERATED_NAME_PREFIX])
    index = r"(?:\d+-)?" if indexed else ""
    return re.compile(rf"^{prefix}{index}{_GENERATED_NAME_SUFFIX}$")


def _event_involved_name(event: Any) -> str | None:
    """The involved object's name, or None when the event lacks a usable one."""
    involved = getattr(event, "involved_object", None)
    name = getattr(involved, "name", None) if involved is not None else None
    return name if isinstance(name, str) and name else None


def _event_message(event: Any) -> str:
    message = getattr(event, "message", None)
    return message if isinstance(message, str) else ""


def _event_source_host(event: Any) -> str | None:
    """``source.host`` — the node an event was generated on, set by the kubelet."""
    source = getattr(event, "source", None)
    host = getattr(source, "host", None) if source is not None else None
    return host if isinstance(host, str) and host else None


def _event_sort_key(event: dict[str, Any]) -> str:
    return str(
        event.get("lastTimestamp") or event.get("firstTimestamp") or event.get("eventTime") or ""
    )


def _lookup_failure(exc: Exception) -> str:
    """A one-line account of a failed Kubernetes read, for ``scope.errors``.

    A kubernetes ``ApiException`` stringifies to several lines carrying the
    response headers and body; its status and reason are what a caller needs
    to tell a refused read (403) from an absent CRD (404).
    """
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None)
    if status is not None and isinstance(reason, str) and reason:
        return f"({status}) {reason}"
    return str(exc)


def _nodeclaims_for_nodes(custom_objects: Any, nodes: set[str]) -> set[str]:
    """Names of the live NodeClaims whose ``status.nodeName`` is one of ``nodes``.

    NodeClaims declare no selectable fields, so this lists the cluster's
    NodeClaims and matches client-side. A NodeClaim whose node has already
    been deleted may be gone too; its events are still reachable by the name
    the pod's ``Nominated`` event recorded, which the caller merges in.
    Raises on API failure so the caller can record why the lookup is missing.
    """
    listing = custom_objects.list_cluster_custom_object(
        group=NODECLAIM_API_GROUP, version=NODECLAIM_API_VERSION, plural=NODECLAIM_PLURAL
    )
    items = listing.get("items") if isinstance(listing, dict) else None
    matched: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        node_name = status.get("nodeName") if isinstance(status, dict) else None
        metadata = item.get("metadata")
        claim_name = metadata.get("name") if isinstance(metadata, dict) else None
        if node_name in nodes and isinstance(claim_name, str) and claim_name:
            matched.add(claim_name)
    return matched


def _collect_job_events(
    core_v1: Any,
    batch_v1: Any,
    custom_objects: Any,
    *,
    namespace: str,
    job_name: str,
) -> dict[str, Any]:
    """Every event the apiserver still holds about a Job, its pods, and their nodes.

    Returns ``{"events": [...], "scope": {...}}``. ``events`` are parsed with
    :func:`_parse_event_to_dict`, newest first, each carrying its
    ``involvedObject.kind`` (``Job``, ``Pod``, ``Node``, ``NodeClaim``) and
    name so a caller can attribute them. ``scope`` records what the read
    covered:

    - ``pods``: every pod name whose events were included — the Job's current
      pods, the pods its own ``SuccessfulCreate`` / ``SuccessfulDelete``
      events name, and any pod carrying the Job controller's name shape for
      this Job (see :func:`_job_pod_name_pattern`);
    - ``deleted_pods``: the subset of ``pods`` that no longer exists, so an
      evicted pod's events are recognizable as such;
    - ``nodes``: the nodes those pods ran on — the current pods'
      ``spec.nodeName``, the scheduler's ``Scheduled`` record and the
      kubelet's ``source.host`` on a deleted pod's events, and Karpenter's
      ``Nominated`` record;
    - ``nodeclaims``: the Karpenter NodeClaims behind those nodes, from the
      live NodeClaims' ``status.nodeName`` and from ``Nominated`` records;
    - ``errors``: each lookup that failed, by target, so an empty list above
      is distinguishable from a lookup the caller's RBAC or cluster refused.

    The Job's own events, the pod listing, and the namespace's pod events are
    required and raise on failure (the endpoint has nothing to answer without
    them). The Job read, the NodeClaim listing, and every Node / NodeClaim
    event read are best-effort and recorded in ``errors``, so a cluster
    without Karpenter, or a role that cannot yet read cluster-scoped events,
    still gets the Job and pod events it always got.
    """
    job_events = core_v1.list_namespaced_event(
        namespace=namespace,
        field_selector=f"involvedObject.name={job_name},involvedObject.kind=Job",
    ).items
    current_pods = core_v1.list_namespaced_pod(
        namespace=namespace, label_selector=f"job-name={job_name}"
    ).items
    namespace_pod_events = core_v1.list_namespaced_event(
        namespace=namespace, field_selector="involvedObject.kind=Pod"
    ).items

    errors: list[str] = []

    # Indexed Jobs name their pods "<job>-<index>-<suffix>"; the Job says so.
    indexed = False
    try:
        job = batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
        spec = getattr(job, "spec", None)
        indexed = getattr(spec, "completion_mode", None) == "Indexed"
    except Exception as exc:
        logger.info(
            "Could not read Job %s/%s for its completion mode: %s",
            sanitize_log_value(namespace),
            sanitize_log_value(job_name),
            sanitize_log_value(exc),
        )
        errors.append(f"Job {namespace}/{job_name}: {_lookup_failure(exc)}")

    current_pod_names: set[str] = set()
    nodes: set[str] = set()
    for pod in current_pods:
        metadata = getattr(pod, "metadata", None)
        pod_name = getattr(metadata, "name", None) if metadata is not None else None
        if isinstance(pod_name, str) and pod_name:
            current_pod_names.add(pod_name)
        spec = getattr(pod, "spec", None)
        node_name = getattr(spec, "node_name", None) if spec is not None else None
        if isinstance(node_name, str) and node_name:
            nodes.add(node_name)

    recorded_pod_names: set[str] = set()
    for event in job_events:
        match = _JOB_POD_EVENT_RE.match(_event_message(event))
        if match:
            recorded_pod_names.add(match.group(1))

    name_pattern = _job_pod_name_pattern(job_name, indexed=indexed)
    pod_events: list[Any] = []
    covered_pod_names: set[str] = set(current_pod_names) | recorded_pod_names
    for event in namespace_pod_events:
        pod_name = _event_involved_name(event)
        if pod_name is None:
            continue
        if pod_name not in covered_pod_names and not name_pattern.match(pod_name):
            continue
        covered_pod_names.add(pod_name)
        pod_events.append(event)

    # Where did those pods run? A deleted pod's spec is gone, but the
    # scheduler, the kubelet, and Karpenter each left the node's name on its
    # events.
    nodeclaims: set[str] = set()
    for event in pod_events:
        host = _event_source_host(event)
        if host is not None:
            nodes.add(host)
        message = _event_message(event)
        reason = getattr(event, "reason", None)
        if reason == "Scheduled":
            scheduled = _SCHEDULED_EVENT_RE.match(message)
            if scheduled:
                nodes.add(scheduled.group(1))
        elif reason == "Nominated":
            nodes.update(_NOMINATED_NODE_RE.findall(message))
            nodeclaims.update(_NOMINATED_NODECLAIM_RE.findall(message))

    if nodes:
        try:
            nodeclaims |= _nodeclaims_for_nodes(custom_objects, nodes)
        except Exception as exc:
            logger.warning("Could not list NodeClaims: %s", sanitize_log_value(exc))
            errors.append(f"NodeClaims: {_lookup_failure(exc)}")

    related_events: list[Any] = []
    for kind, names in (("Node", nodes), ("NodeClaim", nodeclaims)):
        for target in sorted(names):
            try:
                related_events.extend(
                    core_v1.list_namespaced_event(
                        namespace=CLUSTER_SCOPED_EVENT_NAMESPACE,
                        field_selector=f"involvedObject.kind={kind},involvedObject.name={target}",
                    ).items
                )
            except Exception as exc:
                # The target name originates outside this process and the
                # Kubernetes error echoes it back; sanitize before logging so
                # neither can forge log entries (CWE-117).
                logger.warning(
                    "Could not read events of %s %s: %s",
                    kind,
                    sanitize_log_value(target),
                    sanitize_log_value(exc),
                )
                errors.append(f"{kind} {target} events: {_lookup_failure(exc)}")

    events = [_parse_event_to_dict(e) for e in (*job_events, *pod_events, *related_events)]
    events.sort(key=_event_sort_key, reverse=True)
    return {
        "events": events,
        "scope": {
            "pods": sorted(covered_pod_names),
            "deleted_pods": sorted(covered_pod_names - current_pod_names),
            "nodes": sorted(nodes),
            "nodeclaims": sorted(nodeclaims),
            "errors": errors,
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
