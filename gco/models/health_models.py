"""
Health monitoring data models for GCO (Global Capacity Orchestrator on AWS).

This module defines dataclasses for health monitoring including:
- ResourceUtilization: Current CPU/memory/GPU utilization percentages
- HealthStatus: Complete health status report with utilization and thresholds

These models are used by the health monitor service to track and report
cluster health status for load balancer health checks and monitoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .cluster_models import ResourceThresholds


@dataclass
class RequestedResources:
    """
    Resources requested by pending pods (absolute values).

    Attributes:
        cpu_vcpus: Total vCPUs requested by pending pods
        memory_gb: Total GB memory requested by pending pods
        gpus: Total GPUs requested by pending pods
    """

    cpu_vcpus: float
    memory_gb: float
    gpus: int = 0

    def __post_init__(self) -> None:
        """Validate requested values"""
        if not isinstance(self.cpu_vcpus, (int, float)) or self.cpu_vcpus < 0.0:
            raise ValueError(f"cpu_vcpus must be a non-negative number, got {self.cpu_vcpus}")
        if not isinstance(self.memory_gb, (int, float)) or self.memory_gb < 0.0:
            raise ValueError(f"memory_gb must be a non-negative number, got {self.memory_gb}")
        if not isinstance(self.gpus, int) or self.gpus < 0:
            raise ValueError(f"gpus must be a non-negative integer, got {self.gpus}")


@dataclass
class ResourceUtilization:
    """
    Current resource utilization metrics for a cluster.

    Attributes:
        cpu: CPU utilization percentage (0.0-100.0)
        memory: Memory utilization percentage (0.0-100.0)
        gpu: GPU utilization percentage (0.0-100.0)
    """

    cpu: float
    memory: float
    gpu: float

    def __post_init__(self) -> None:
        """Validate utilization values"""
        for field_name, value in [("cpu", self.cpu), ("memory", self.memory), ("gpu", self.gpu)]:
            if not isinstance(value, (int, float)) or not 0.0 <= value <= 100.0:
                raise ValueError(
                    f"{field_name} must be a number between 0.0 and 100.0, got {value}"
                )


@dataclass
class HealthStatus:
    """Health status report for a cluster"""

    cluster_id: str
    region: str
    timestamp: datetime
    status: Literal["healthy", "unhealthy"]
    resource_utilization: ResourceUtilization
    thresholds: ResourceThresholds  # Forward reference to avoid circular import
    active_jobs: int
    pending_pods: int = 0
    pending_requested: RequestedResources | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        """Validate health status"""
        if not self.cluster_id:
            raise ValueError("Cluster ID cannot be empty")

        if not self.region:
            raise ValueError("Region cannot be empty")

        if self.active_jobs < 0:
            raise ValueError("Active jobs count cannot be negative")

        if self.pending_pods < 0:
            raise ValueError("Pending pods count cannot be negative")

        if self.status not in ["healthy", "unhealthy"]:
            raise ValueError("Status must be 'healthy' or 'unhealthy'")

    def is_healthy(self) -> bool:
        """Check if the cluster is healthy based on enabled thresholds."""
        utilization_ok = all(
            self.thresholds.is_disabled(name) or value <= getattr(self.thresholds, name)
            for name, value in (
                ("cpu_threshold", self.resource_utilization.cpu),
                ("memory_threshold", self.resource_utilization.memory),
                ("gpu_threshold", self.resource_utilization.gpu),
            )
        )

        pending_ok = (
            self.thresholds.is_disabled("pending_pods_threshold")
            or self.pending_pods <= self.thresholds.pending_pods_threshold
        )

        pending_resources_ok = True
        if self.pending_requested:
            pending_resources_ok = all(
                self.thresholds.is_disabled(name) or value <= getattr(self.thresholds, name)
                for name, value in (
                    (
                        "pending_requested_cpu_vcpus",
                        self.pending_requested.cpu_vcpus,
                    ),
                    (
                        "pending_requested_memory_gb",
                        self.pending_requested.memory_gb,
                    ),
                    ("pending_requested_gpus", self.pending_requested.gpus),
                )
            )

        return utilization_ok and pending_ok and pending_resources_ok

    def get_threshold_violations(self) -> list[str]:
        """Get violations for enabled thresholds only."""
        violations = []

        if (
            not self.thresholds.is_disabled("cpu_threshold")
            and self.resource_utilization.cpu > self.thresholds.cpu_threshold
        ):
            violations.append(
                f"CPU: {self.resource_utilization.cpu:.1f}% > {self.thresholds.cpu_threshold}%"
            )

        if (
            not self.thresholds.is_disabled("memory_threshold")
            and self.resource_utilization.memory > self.thresholds.memory_threshold
        ):
            violations.append(
                f"Memory: {self.resource_utilization.memory:.1f}% > {self.thresholds.memory_threshold}%"
            )

        if (
            not self.thresholds.is_disabled("gpu_threshold")
            and self.resource_utilization.gpu > self.thresholds.gpu_threshold
        ):
            violations.append(
                f"GPU: {self.resource_utilization.gpu:.1f}% > {self.thresholds.gpu_threshold}%"
            )

        if (
            not self.thresholds.is_disabled("pending_pods_threshold")
            and self.pending_pods > self.thresholds.pending_pods_threshold
        ):
            violations.append(
                f"Pending Pods: {self.pending_pods} > {self.thresholds.pending_pods_threshold}"
            )

        if self.pending_requested:
            if (
                not self.thresholds.is_disabled("pending_requested_cpu_vcpus")
                and self.pending_requested.cpu_vcpus > self.thresholds.pending_requested_cpu_vcpus
            ):
                violations.append(
                    f"Pending CPU: {self.pending_requested.cpu_vcpus:.1f} vCPUs > {self.thresholds.pending_requested_cpu_vcpus} vCPUs"
                )
            if (
                not self.thresholds.is_disabled("pending_requested_memory_gb")
                and self.pending_requested.memory_gb > self.thresholds.pending_requested_memory_gb
            ):
                violations.append(
                    f"Pending Memory: {self.pending_requested.memory_gb:.1f} GB > {self.thresholds.pending_requested_memory_gb} GB"
                )
            if (
                not self.thresholds.is_disabled("pending_requested_gpus")
                and self.pending_requested.gpus > self.thresholds.pending_requested_gpus
            ):
                violations.append(
                    f"Pending GPUs: {self.pending_requested.gpus} > {self.thresholds.pending_requested_gpus}"
                )

        return violations
