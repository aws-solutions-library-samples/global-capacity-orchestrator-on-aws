"""
Data models for GCO (Global Capacity Orchestrator on AWS).

This module provides Pydantic-style dataclasses for:
- Cluster configuration (EKS settings, thresholds)
- Health monitoring (resource utilization, health status)
- Manifest processing (Kubernetes manifests, submission requests/responses)

All models include validation in __post_init__ to ensure data integrity.
"""

from .cluster_models import ClusterConfig, ResourceThresholds
from .health_models import HealthStatus, RequestedResources, ResourceUtilization
from .inference_models import (
    EndpointState,
    InferenceEndpoint,
    InferenceEndpointSpec,
    RegionStatus,
    RegionSyncState,
)
from .manifest_models import (
    KubernetesManifest,
    ManifestSubmissionRequest,
    ManifestSubmissionResponse,
    ResourceStatus,
)

# Sorted (RUF022); the per-module grouping is visible in the imports above.
__all__ = [
    "ClusterConfig",
    "EndpointState",
    "HealthStatus",
    "InferenceEndpoint",
    "InferenceEndpointSpec",
    "KubernetesManifest",
    "ManifestSubmissionRequest",
    "ManifestSubmissionResponse",
    "RegionStatus",
    "RegionSyncState",
    "RequestedResources",
    "ResourceStatus",
    "ResourceThresholds",
    "ResourceUtilization",
]
