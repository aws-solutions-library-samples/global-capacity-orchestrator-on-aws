"""Data classes and GPU instance specifications for capacity checking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InstanceTypeInfo:
    """Information about an EC2 instance type."""

    instance_type: str
    vcpus: int
    memory_gib: float
    gpu_count: int = 0
    gpu_type: str | None = None
    gpu_memory_gib: float = 0
    architecture: str = "x86_64"

    @property
    def is_gpu(self) -> bool:
        return self.gpu_count > 0


@dataclass
class SpotPriceInfo:
    """Spot price information for an instance type."""

    instance_type: str
    availability_zone: str
    current_price: float
    avg_price_7d: float
    min_price_7d: float
    max_price_7d: float
    price_stability: float  # 0-1, higher is more stable


@dataclass
class CapacityEstimate:
    """Capacity availability estimate."""

    instance_type: str
    region: str
    availability_zone: str | None
    capacity_type: str  # "spot" or "on-demand"
    availability: str  # "high", "medium", "low", "unavailable", "unknown"
    confidence: float  # 0-1
    estimated_wait_time: str | None = None
    price_per_hour: float | None = None
    recommendation: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# GPU instance type specifications (common types)
GPU_INSTANCE_SPECS = {
    # G4dn - NVIDIA T4
    "g4dn.xlarge": InstanceTypeInfo("g4dn.xlarge", 4, 16, 1, "T4", 16, "x86_64"),
    "g4dn.2xlarge": InstanceTypeInfo("g4dn.2xlarge", 8, 32, 1, "T4", 16, "x86_64"),
    "g4dn.4xlarge": InstanceTypeInfo("g4dn.4xlarge", 16, 64, 1, "T4", 16, "x86_64"),
    "g4dn.8xlarge": InstanceTypeInfo("g4dn.8xlarge", 32, 128, 1, "T4", 16, "x86_64"),
    "g4dn.12xlarge": InstanceTypeInfo("g4dn.12xlarge", 48, 192, 4, "T4", 64, "x86_64"),
    "g4dn.16xlarge": InstanceTypeInfo("g4dn.16xlarge", 64, 256, 1, "T4", 16, "x86_64"),
    # G5 - NVIDIA A10G
    "g5.xlarge": InstanceTypeInfo("g5.xlarge", 4, 16, 1, "A10G", 24, "x86_64"),
    "g5.2xlarge": InstanceTypeInfo("g5.2xlarge", 8, 32, 1, "A10G", 24, "x86_64"),
    "g5.4xlarge": InstanceTypeInfo("g5.4xlarge", 16, 64, 1, "A10G", 24, "x86_64"),
    "g5.8xlarge": InstanceTypeInfo("g5.8xlarge", 32, 128, 1, "A10G", 24, "x86_64"),
    "g5.12xlarge": InstanceTypeInfo("g5.12xlarge", 48, 192, 4, "A10G", 96, "x86_64"),
    "g5.16xlarge": InstanceTypeInfo("g5.16xlarge", 64, 256, 1, "A10G", 24, "x86_64"),
    "g5.24xlarge": InstanceTypeInfo("g5.24xlarge", 96, 384, 4, "A10G", 96, "x86_64"),
    "g5.48xlarge": InstanceTypeInfo("g5.48xlarge", 192, 768, 8, "A10G", 192, "x86_64"),
    # P3 - NVIDIA V100
    "p3.2xlarge": InstanceTypeInfo("p3.2xlarge", 8, 61, 1, "V100", 16, "x86_64"),
    "p3.8xlarge": InstanceTypeInfo("p3.8xlarge", 32, 244, 4, "V100", 64, "x86_64"),
    "p3.16xlarge": InstanceTypeInfo("p3.16xlarge", 64, 488, 8, "V100", 128, "x86_64"),
    "p3dn.24xlarge": InstanceTypeInfo("p3dn.24xlarge", 96, 768, 8, "V100", 256, "x86_64"),
    # P4d / P4de - NVIDIA A100
    "p4d.24xlarge": InstanceTypeInfo("p4d.24xlarge", 96, 1152, 8, "A100", 320, "x86_64"),
    "p4de.24xlarge": InstanceTypeInfo("p4de.24xlarge", 96, 1152, 8, "A100", 640, "x86_64"),
    # P5 / P5e / P5en - NVIDIA H100 / H200
    "p5.48xlarge": InstanceTypeInfo("p5.48xlarge", 192, 2048, 8, "H100", 640, "x86_64"),
    "p5e.48xlarge": InstanceTypeInfo("p5e.48xlarge", 192, 2048, 8, "H200", 1128, "x86_64"),
    "p5en.48xlarge": InstanceTypeInfo("p5en.48xlarge", 192, 2048, 8, "H200", 1128, "x86_64"),
    # P6 - NVIDIA Blackwell B200 (standalone EC2 type; B300 ships only as
    # P6e-GB300 UltraServers, so it is intentionally absent here).
    "p6-b200.48xlarge": InstanceTypeInfo("p6-b200.48xlarge", 192, 2048, 8, "B200", 1440, "x86_64"),
}
