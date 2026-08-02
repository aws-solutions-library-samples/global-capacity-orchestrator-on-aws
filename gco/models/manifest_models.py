"""
Manifest submission data models for GCO (Global Capacity Orchestrator on AWS).

This module defines dataclasses for Kubernetes manifest processing:
- KubernetesManifest: Single Kubernetes resource definition
- ManifestSubmissionRequest: Request to submit one or more manifests
- ManifestSubmissionResponse: Response with processing results
- ResourceStatus: Status of a single resource operation

These models are used by the manifest processor service to validate,
process, and track Kubernetes resource submissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class KubernetesManifest:
    """
    Represents a single Kubernetes manifest.

    Attributes:
        apiVersion: Kubernetes API version (e.g., 'apps/v1', 'v1')
        kind: Resource kind (e.g., 'Deployment', 'Service', 'ConfigMap')
        metadata: Resource metadata including name and namespace
        spec: Resource specification (for most resource types)
        data: Resource data (for ConfigMaps and Secrets)
    """

    apiVersion: str
    kind: str
    metadata: dict[str, Any]
    spec: dict[str, Any] | None = None
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate Kubernetes manifest structure"""
        if not self.apiVersion:
            raise ValueError("apiVersion cannot be empty")

        if not self.kind:
            raise ValueError("kind cannot be empty")

        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dictionary")

        if "name" not in self.metadata:
            raise ValueError("metadata must contain a 'name' field")

        # Either spec or data should be present (or both for some resources)
        if self.spec is not None and not isinstance(self.spec, dict):
            raise ValueError("spec must be a dictionary")

        if self.data is not None and not isinstance(self.data, dict):
            raise ValueError("data must be a dictionary")

        # At least one of spec or data should be present for most resources
        if (
            self.spec is None
            and self.data is None
            and self.kind not in ["Namespace", "ServiceAccount"]
        ):
            raise ValueError("Either spec or data must be provided for most resource types")

    def get_name(self) -> str:
        """Get the resource name"""
        return str(self.metadata.get("name", ""))

    def get_namespace(self) -> str:
        """Get the resource namespace"""
        return str(self.metadata.get("namespace", "gco-jobs"))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation"""
        result = {"apiVersion": self.apiVersion, "kind": self.kind, "metadata": self.metadata}

        if self.spec is not None:
            result["spec"] = self.spec

        if self.data is not None:
            result["data"] = self.data

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KubernetesManifest:
        """Create from dictionary representation"""
        return cls(
            apiVersion=data["apiVersion"],
            kind=data["kind"],
            metadata=data["metadata"],
            spec=data.get("spec"),
            data=data.get("data"),
        )


@dataclass
class ManifestSubmissionRequest:
    """Request to submit Kubernetes manifests"""

    manifests: list[dict[str, Any]]
    namespace: str | None = None
    dry_run: bool = False
    validate: bool = True

    def __post_init__(self) -> None:
        """Validate submission request"""
        if not self.manifests:
            raise ValueError("At least one manifest must be provided")

        # Validate each manifest can be parsed
        for i, manifest_data in enumerate(self.manifests):
            try:
                KubernetesManifest.from_dict(manifest_data)
            except Exception as e:
                raise ValueError(f"Invalid manifest at index {i}: {e}") from e

    def get_kubernetes_manifests(self) -> list[KubernetesManifest]:
        """Convert to KubernetesManifest objects"""
        return [KubernetesManifest.from_dict(manifest) for manifest in self.manifests]

    def get_resource_count(self) -> int:
        """Get total number of resources to be created"""
        return len(self.manifests)


@dataclass
class ResourceStatus:
    """Status of a deployed Kubernetes resource"""

    api_version: str
    kind: str
    name: str
    namespace: str
    status: str  # 'created', 'updated', 'unchanged', 'failed', 'deleted', 'forbidden'
    message: str | None = None
    uid: str | None = None  # Kubernetes resource UID

    def __post_init__(self) -> None:
        """Validate resource status"""
        valid_statuses = {
            "created",
            "updated",
            "unchanged",
            "failed",
            "deleted",
            "forbidden",
        }
        if self.status not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}, got {self.status}")

        if not self.name:
            raise ValueError("Resource name cannot be empty")

        if not self.namespace:
            raise ValueError("Resource namespace cannot be empty")

    def is_successful(self) -> bool:
        """Check if the resource operation was successful"""
        return self.status in {"created", "updated", "unchanged", "deleted"}

    def get_resource_identifier(self) -> str:
        """Get unique identifier for this resource"""
        return f"{self.api_version}/{self.kind}/{self.namespace}/{self.name}"


@dataclass
class ManifestSubmissionResponse:
    """Response from manifest submission"""

    success: bool
    cluster_id: str
    region: str
    resources: list[ResourceStatus]
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        """Validate submission response"""
        if not self.cluster_id:
            raise ValueError("Cluster ID cannot be empty")

        if not self.region:
            raise ValueError("Region cannot be empty")

        if not isinstance(self.resources, list):
            raise ValueError("Resources must be a list")

    def get_successful_resources(self) -> list[ResourceStatus]:
        """Get list of successfully processed resources"""
        return [r for r in self.resources if r.is_successful()]

    def get_failed_resources(self) -> list[ResourceStatus]:
        """Get list of failed resources"""
        return [r for r in self.resources if not r.is_successful()]

    def get_summary(self) -> dict[str, int]:
        """Get summary of resource processing results"""
        summary = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        for resource in self.resources:
            summary[resource.status] = summary.get(resource.status, 0) + 1
        return summary
