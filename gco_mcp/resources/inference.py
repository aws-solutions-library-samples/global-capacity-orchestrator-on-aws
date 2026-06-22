"""Live inference endpoint resources (gco://inference/...) for the GCO MCP server.

Reads the desired-state record from the inference DynamoDB store via
``cli/inference.py::InferenceManager.get_endpoint`` and returns it as
JSON. The store is the source of truth that the per-region
``inference_monitor`` reconciles against, so this is the most
authoritative spec available without reaching into Kubernetes.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Same RFC 1123 label rule as job names — endpoint names map to
# Kubernetes deployments and services in every regional cluster.
_ENDPOINT_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$")


def _get_manager() -> Any:
    """Lazy-import ``InferenceManager`` so the resource module doesn't
    pull boto3 in at MCP server import time."""
    from cli.inference import InferenceManager

    return InferenceManager()


def _inference_resource(endpoint_name: str) -> str:
    """Return the stored spec for ``endpoint_name`` as JSON."""
    if not _ENDPOINT_NAME_RE.match(endpoint_name):
        return json.dumps(
            {
                "error": "invalid endpoint_name",
                "detail": "must match ^[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?$",
                "value": endpoint_name,
            }
        )
    try:
        record = _get_manager().get_endpoint(endpoint_name)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e), "endpoint_name": endpoint_name})
    if record is None:
        return json.dumps({"error": "endpoint not found", "endpoint_name": endpoint_name})
    return json.dumps(record, indent=2, default=str)


def register(mcp_instance: Any) -> None:
    """Register live inference-state resources against the shared MCP server."""
    mcp_instance.resource("gco://inference/{endpoint_name}")(_inference_resource)
