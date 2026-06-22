"""Read-only MCP tools for inspecting long-running task status.

Every long-running tool (``deploy_all``, ``destroy_all``, etc.) writes
a JSON status file and a raw log file under ``~/.gco/tasks/`` via
``_task_status.TaskStatusWriter``. The two tools in this module read
those artifacts so any MCP client — including agents that don't render
``ctx.info`` notifications — can see real-time progress without
blocking on the original tool call's response.

Both tools are read-only and intentionally always enabled (no feature
flag): they never mutate AWS state and never spawn subprocesses. The
worst case is reporting nothing when the status directory is empty.
"""

from __future__ import annotations

import json

from audit import audit_logged
from server import mcp

from tools._task_status import get_task, list_tasks, tail_log


@mcp.tool(tags={"safe", "observability"})
@audit_logged
def task_status(task_id: str | None = None, limit: int = 20) -> str:
    """Return live status of long-running tools.

    Each long-running tool (deploy_all, destroy_all, bootstrap_cdk,
    deploy_stack, destroy_stack, images_build, images_push) records
    progress to ``~/.gco/tasks/{task_id}.json`` on every output line.
    This tool reads that disk-backed channel — independent of whatever
    the calling MCP client decides to do with progress notifications —
    so operators (and other agents) can observe what's happening.

    PIDs are re-checked on every read; a status file claiming
    ``state=running`` whose recorded PID is no longer alive is rewritten
    to ``state=orphaned`` in the response so callers see honest data
    even when the original MCP wrapper exited unexpectedly while the
    underlying CLI was still running.

    Args:
        task_id: Specific task to inspect (e.g. ``deploy_all-1747683123``).
            Omit to list every known task, newest first.
        limit: When listing, maximum number of records to return.
            Ignored when ``task_id`` is set.

    Returns:
        JSON string. When ``task_id`` is set, an object with the task
        record. When omitted, ``{"tasks": [...]}`` newest-first.
    """
    if task_id:
        record = get_task(task_id)
        if record is None:
            return json.dumps({"error": "task_not_found", "task_id": task_id})
        return json.dumps(record, indent=2, sort_keys=True)
    records = list_tasks()
    if limit > 0:
        records = records[:limit]
    return json.dumps({"tasks": records}, indent=2, sort_keys=True)


@mcp.tool(tags={"safe", "observability"})
@audit_logged
def task_tail(task_id: str, lines: int = 100) -> str:
    """Return the last N lines of a long-running task's raw output log.

    The log captures the full interleaved stdout+stderr of the
    underlying subprocess (CDK deploy, finch image push, etc.) as the
    tool wrote it to disk. Each line is prefixed with ``[stdout]`` or
    ``[stderr]`` so observers can tell which stream produced it.

    Args:
        task_id: Task to read (from ``task_status``).
        lines: Maximum lines to return. ``100`` is enough to see the
            most recent stack milestone plus surrounding context;
            ``500`` typically covers a full single-stack deploy.

    Returns:
        JSON string ``{"task_id": ..., "lines": [...]}`` containing the
        tail. Empty list when the task hasn't emitted any output yet
        or its log file has been pruned.
    """
    tail = tail_log(task_id, lines=lines)
    return json.dumps({"task_id": task_id, "lines": tail}, indent=2)
