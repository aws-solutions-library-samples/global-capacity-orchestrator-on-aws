"""
Capacity checking utilities for GCO CLI.

This package provides EC2 capacity checking across regions using real AWS
signals (Spot Placement Scores, pricing, Capacity Blocks, etc.).

Submodules:
    models        — Data classes and GPU instance specs
    checker       — Single-region CapacityChecker
    multi_region  — Multi-region checking and weighted scoring
    advisor       — Bedrock-powered AI capacity recommendations
"""

# Re-export everything so ``from cli.capacity import X`` keeps working.
# Re-export get_config so tests that patch cli.capacity.get_config keep working.
from cli.config import get_config as get_config  # noqa: F401

from .advisor import (
    BedrockCapacityAdvisor,
    BedrockCapacityRecommendation,
    CapacityPredictionResult,
    get_bedrock_capacity_advisor,
)
from .checker import CapacityChecker, get_capacity_checker
from .history import (
    CapacityHistoryStore,
    flatten_capacity_data,
    get_capacity_history_store,
)
from .models import (
    GPU_INSTANCE_SPECS,
    CapacityCheckError,
    CapacityEstimate,
    InstanceTypeInfo,
    SpotPriceInfo,
)
from .multi_region import (
    MultiRegionCapacityChecker,
    RegionCapacity,
    compute_price_trend,
    compute_weighted_score,
    get_multi_region_capacity_checker,
)

__all__ = [
    "GPU_INSTANCE_SPECS",
    "BedrockCapacityAdvisor",
    "BedrockCapacityRecommendation",
    "CapacityCheckError",
    "CapacityChecker",
    "CapacityEstimate",
    "CapacityHistoryStore",
    "CapacityPredictionResult",
    "InstanceTypeInfo",
    "MultiRegionCapacityChecker",
    "RegionCapacity",
    "SpotPriceInfo",
    "compute_price_trend",
    "compute_weighted_score",
    "flatten_capacity_data",
    "get_bedrock_capacity_advisor",
    "get_capacity_checker",
    "get_capacity_history_store",
    "get_multi_region_capacity_checker",
]
