"""The smallest cdk.json context ``ConfigLoader`` accepts.

``ConfigLoader`` validates every context (an absent ``project_name`` no
longer skips the checks), so a test that reads a getter's defaults, or calls
one validator in isolation, starts from this context: the required fields
only, every optional block left out so it keeps its default.
"""

from __future__ import annotations

import copy
from typing import Any

_MINIMAL_CONTEXT: dict[str, Any] = {
    "deployment_regions": {
        "global": "us-east-2",
        "api_gateway": "us-east-2",
        "monitoring": "us-east-2",
    },
    "kubernetes_version": "1.37",
    "resource_thresholds": {"cpu_threshold": 80, "memory_threshold": 80, "gpu_threshold": -1},
    "manifest_processor": {
        "replicas": 3,
        "resource_limits": {"cpu": "1000m", "memory": "2Gi"},
    },
    "job_validation_policy": {"allowed_namespaces": ["gco-jobs"], "resource_quotas": {}},
    "api_gateway": {
        "throttle_rate_limit": 1000,
        "throttle_burst_limit": 2000,
        "log_level": "INFO",
        "metrics_enabled": True,
        "tracing_enabled": True,
    },
}


def minimal_context(**blocks: Any) -> dict[str, Any]:
    """Return a fresh copy of the minimal context with ``blocks`` set over it."""
    context = copy.deepcopy(_MINIMAL_CONTEXT)
    context.update(copy.deepcopy(blocks))
    return context
