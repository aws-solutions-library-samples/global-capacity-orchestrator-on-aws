"""Task status resources (tasks://gco/...) for the GCO MCP server.

Reads through FastMCP's task protocol surface to surface the status of
a long-running tool invocation as JSON. Returns a graceful error stub
when the FastMCP build in use doesn't expose a task-status query — the
protocol surface lives under ``fastmcp.server.tasks`` and its public
shape can shift between minor versions.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Task IDs are client-controlled strings (FastMCP forwards whatever the
# client passed). Restrict to a generous alphanumeric+ punctuation set
# so a malformed URI expansion can't sneak shell metacharacters into
# downstream lookups.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _lookup_task_state(task_id: str) -> dict[str, Any] | None:
    """Best-effort lookup of a task's current state through FastMCP's API.

    FastMCP exposes task introspection via the docket store registered
    on the server instance. The exact accessor moves between minor
    versions, so this helper tries the documented paths in order and
    returns ``None`` if none of them work — the caller turns that into
    a graceful error JSON.
    """
    try:
        from server import mcp as _mcp
    except ImportError:
        return None

    # Newer FastMCP exposes a direct ``get_task`` accessor.
    getter = getattr(_mcp, "get_task", None)
    if callable(getter):
        try:
            record = getter(task_id)
        except Exception:  # noqa: BLE001
            record = None
        if record is not None:
            return _coerce_to_dict(record)

    # Older builds keep state on the docket adapter.
    docket = getattr(_mcp, "_docket", None) or getattr(_mcp, "docket", None)
    for attr in ("get_task", "get", "fetch_task"):
        accessor = getattr(docket, attr, None)
        if callable(accessor):
            try:
                record = accessor(task_id)
            except Exception:  # noqa: BLE001
                continue
            if record is not None:
                return _coerce_to_dict(record)

    return None


def _coerce_to_dict(record: object) -> dict[str, Any]:
    """Best-effort conversion of an opaque task record to a JSON-friendly dict."""
    if isinstance(record, dict):
        return record
    for attr in ("model_dump", "dict", "to_dict", "_asdict"):
        method = getattr(record, attr, None)
        if callable(method):
            try:
                payload = method()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict):
                return payload
    if hasattr(record, "__dict__"):
        return {k: v for k, v in vars(record).items() if not k.startswith("_")}
    return {"value": str(record)}


def _task_resource(task_id: str) -> str:
    """Return the current status of ``task_id`` as JSON."""
    if not _TASK_ID_RE.match(task_id):
        return json.dumps({"error": "invalid task_id", "value": task_id})
    state = _lookup_task_state(task_id)
    if state is None:
        return json.dumps(
            {
                "error": "task protocol not available",
                "detail": (
                    "this build of FastMCP does not expose a task-status accessor "
                    "this resource handler can call"
                ),
                "task_id": task_id,
            }
        )
    return json.dumps({"task_id": task_id, "state": state}, indent=2, default=str)


def register(mcp_instance: Any) -> None:
    """Register the task-status resource against the shared MCP server."""
    mcp_instance.resource("tasks://gco/{task_id}")(_task_resource)
