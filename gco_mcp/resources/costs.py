"""Cost summary resources (costs://gco/...) for the GCO MCP server.

Wraps ``gco costs summary --days N`` so an LLM can pin a cost window
URI (e.g. ``costs://gco/summary/30``) and pull the same payload the
``cost_summary`` tool returns. The number lives in the path so each
window has its own pinnable resource — handy when comparing 7-day,
30-day, and 90-day pictures across turns.
"""

from __future__ import annotations

import json
from typing import Any

import cli_runner


def _summary_resource(days_window: str) -> str:
    """Return the cost summary for a positive-integer day window."""
    try:
        days = int(days_window)
    except TypeError, ValueError:
        return json.dumps({"error": "days_window must be a positive integer", "value": days_window})
    if days <= 0:
        return json.dumps({"error": "days_window must be a positive integer", "value": days_window})
    return cli_runner._run_cli("costs", "summary", "--days", str(days))


def register(mcp_instance: Any) -> None:
    """Register the cost summary resource against the shared MCP server."""
    mcp_instance.resource("costs://gco/summary/{days_window}")(_summary_resource)
