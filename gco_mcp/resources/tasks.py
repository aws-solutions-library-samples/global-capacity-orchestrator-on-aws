"""Task status resources (tasks://gco/...) for the GCO MCP server.

Reads through the MCP tasks extension (SEP-2663, the ``fastmcp_tasks``
package in FastMCP 4) to surface the status of a long-running tool
invocation as JSON. The extension's own ``tasks/get`` handler is reused so
this resource reports exactly what a protocol-native ``tasks/get`` request
would return — status, timestamps, poll interval, and the inlined result or
error for finished tasks. Returns a graceful error stub when the FastMCP
build in use doesn't ship the tasks extension.
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


async def _lookup_task_state(task_id: str) -> dict[str, Any] | None:
    """Look up a task's current state through the tasks extension.

    Delegates to ``fastmcp_tasks.handlers.tasks_get`` — the same handler
    that serves protocol ``tasks/get`` requests — so the resource view and
    the wire view can never disagree. Returns ``None`` when the extension
    is unavailable on this build (the caller turns that into a graceful
    "not available" JSON) and raises ``LookupError`` when the extension is
    present but knows nothing about ``task_id``.
    """
    try:
        from fastmcp_tasks.handlers import tasks_get
        from server import mcp as _mcp
    except ImportError:
        return None

    try:
        record = await tasks_get(_mcp, task_id)
    except Exception as exc:
        # The handler raises the protocol not-found error for unknown or
        # expired task ids (and for a docket that has not started yet).
        raise LookupError(str(exc)) from exc
    return _coerce_to_dict(record)


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


async def _task_resource(task_id: str) -> str:
    """Return the current status of ``task_id`` as JSON."""
    if not _TASK_ID_RE.match(task_id):
        return json.dumps({"error": "invalid task_id", "value": task_id})
    try:
        state = await _lookup_task_state(task_id)
    except LookupError as exc:
        return json.dumps(
            {
                "error": "task not found",
                "detail": str(exc)[:200],
                "task_id": task_id,
            }
        )
    if state is None:
        return json.dumps(
            {
                "error": "task protocol not available",
                "detail": (
                    "this build of FastMCP does not ship the tasks extension "
                    "(fastmcp_tasks) this resource handler reads through"
                ),
                "task_id": task_id,
            }
        )
    return json.dumps({"task_id": task_id, "state": state}, indent=2, default=str)


def register(mcp_instance: Any) -> None:
    """Register the task-status resource against the shared MCP server."""
    mcp_instance.resource("tasks://gco/{task_id}")(_task_resource)
