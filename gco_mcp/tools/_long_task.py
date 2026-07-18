"""Shared async subprocess runner for long-running MCP tools.

Streams progress through FastMCP's Progress dependency, emits a periodic
heartbeat when the underlying process goes quiet, captures a bounded tail of
stderr for failure surfacing, and raises ``ToolError`` on non-zero exit.

In parallel with the MCP wire, every invocation writes a JSON status file plus
a size-bounded raw log under ``~/.gco/tasks/`` via
``_task_status.TaskStatusWriter``. This gives operators an out-of-band view of
work even when the MCP client drops streamed notifications.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import Any

import cli_runner
from audit import _try_get_task_id
from fastmcp.exceptions import ToolError

from tools._task_status import TaskStatusWriter, is_valid_task_id, make_task_id

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``_run_long_task`` -> ``diagrams/code_diagrams/gco_mcp/tools/_long_task._run_long_task.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/tools/_long_task._run_long_task.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


_CFN_FAILED_RE = re.compile(r"(CREATE|UPDATE|DELETE)_FAILED")
_CDK_STACK_LINE_RE = re.compile(r"\b(gco-[a-z0-9-]+)\b")
_CDK_STACK_DONE_RE = re.compile(r"[✅✨]\s+(gco-[a-z0-9-]+)\b")
_CANCEL_GRACE_SECONDS = 10
_HEARTBEAT_INTERVAL_SECONDS = 30
_STDERR_TAIL_LINES = 80
_FAILED_EVENT_LINES = 10
_STREAM_READ_BYTES = 16 * 1024
_STREAM_LINE_MAX_BYTES = 64 * 1024
_DIAGNOSTIC_LINE_MAX_BYTES = 4 * 1024
_CLIENT_MESSAGE_MAX_CHARS = 200
_TRUNCATED_TEXT = "...[truncated]"
_PARTIAL_STATE_DISCLAIMER = (
    "Partial CloudFormation state may remain — inspect via stack_status or the AWS console."
)


def _argv_has_traversal(argv: Sequence[str]) -> tuple[int, str] | None:
    """Return the first non-flag argv element containing a ``..`` segment."""
    for index, value in enumerate(argv):
        if value.startswith("-"):
            continue
        if ".." in value.split("/") or ".." in value.split("\\"):
            return index, value[:100]
    return None


def _bounded_text(value: str, max_bytes: int) -> str:
    """Return UTF-8 text within ``max_bytes`` with an explicit marker."""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = _TRUNCATED_TEXT.encode()
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker)]
    return prefix.decode("utf-8", errors="ignore") + _TRUNCATED_TEXT


async def _bounded_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield newline-delimited output without ever accumulating a giant line.

    ``StreamReader`` async iteration delegates to ``readline()``, which can
    raise once its internal limit is exceeded before application-level bounds
    run. Fixed-size ``read()`` calls avoid that failure mode. Bytes beyond the
    per-line budget are discarded until the next newline and represented by an
    explicit truncation marker.
    """
    marker_bytes = _TRUNCATED_TEXT.encode()
    payload_budget = max(0, _STREAM_LINE_MAX_BYTES - len(marker_bytes))
    buffered = bytearray()
    truncated = False

    while True:
        chunk = await stream.read(_STREAM_READ_BYTES)
        if not chunk:
            break
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline
            segment = chunk[offset:end]
            if not truncated:
                remaining = max(0, payload_budget - len(buffered))
                if remaining:
                    buffered.extend(segment[:remaining])
                if len(segment) > remaining:
                    truncated = True

            if newline < 0:
                break

            raw = bytes(buffered)
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            text = raw.decode("utf-8", errors="replace")
            if truncated:
                text += _TRUNCATED_TEXT
            yield _bounded_text(text, _STREAM_LINE_MAX_BYTES)
            buffered.clear()
            truncated = False
            offset = newline + 1

    if buffered or truncated:
        raw = bytes(buffered)
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += _TRUNCATED_TEXT
        yield _bounded_text(text, _STREAM_LINE_MAX_BYTES)


async def _best_effort_client_call(target: Any, method_name: str, *args: object) -> None:
    """Call one optional async client notification without breaking work.

    Client disconnects and version-skewed Progress implementations must not
    terminate the underlying infrastructure operation. ``CancelledError`` is a
    ``BaseException`` and intentionally still propagates.
    """
    try:
        method = getattr(target, method_name)
        await method(*args)
    except Exception:
        return


async def _terminate_and_reap(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
) -> None:
    """Terminate a subprocess, escalate after grace, and always reap it."""
    if process.returncode is None:
        with contextlib.suppress(OSError):
            process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=_CANCEL_GRACE_SECONDS)
        return
    except TimeoutError:
        pass
    except Exception:
        # A failed wait task should not prevent the kill/reap fallback.
        pass

    if process.returncode is None:
        with contextlib.suppress(OSError):
            process.kill()
    if wait_task.cancelled():
        wait_task = asyncio.create_task(process.wait())
    with contextlib.suppress(Exception):
        await asyncio.shield(wait_task)
    if process.returncode is None:
        # Defensive final wait if a mocked or unusual Process did not update
        # returncode through the original wait task.
        with contextlib.suppress(Exception):
            await process.wait()


async def _run_long_task(
    argv: Sequence[str],
    *,
    ctx: Any,
    progress: Any,
    is_stack_op: bool = True,
    total_units: int | None = None,
) -> str:
    """Run a long-lived command with bounded output and durable status.

    Logical ``gco`` argv is resolved to the executable installed beside this
    MCP environment and runs from the project root. Process wait and both pipe
    drains are coordinated concurrently, so a drain error is surfaced promptly
    instead of allowing an unread pipe to deadlock the child. Every exception
    after spawn terminates/reaps the process and stamps terminal disk status.
    """
    logical_argv = list(argv)
    hit = _argv_has_traversal(logical_argv)
    if hit is not None:
        index, value = hit
        return json.dumps({"error": "path_traversal_detected", "argv_index": index, "value": value})

    protocol_task_id = _try_get_task_id(ctx)
    if len(logical_argv) >= 3 and logical_argv[0] == "gco":
        tool_name = f"{logical_argv[1]}_{logical_argv[2].replace('-', '_')}"
    elif len(logical_argv) >= 2 and logical_argv[0] == "gco":
        tool_name = logical_argv[1]
    else:
        tool_name = logical_argv[0] if logical_argv else "task"
    task_id = (
        protocol_task_id
        if isinstance(protocol_task_id, str) and is_valid_task_id(protocol_task_id)
        else make_task_id(tool_name)
    )

    spawn_argv = list(logical_argv)
    if spawn_argv and spawn_argv[0] == "gco":
        spawn_argv[0] = cli_runner._gco_executable()

    started = time.monotonic()
    # Initialize observability before spawning. The writer degrades to an
    # in-memory no-op when its directory is unavailable, so construction cannot
    # strand a child process after spawn.
    status_writer = TaskStatusWriter(
        task_id=task_id,
        tool=tool_name,
        argv=logical_argv,
        pid=None,
        total_units=total_units,
    )

    process: asyncio.subprocess.Process | None = None
    wait_task: asyncio.Task[int] | None = None
    drains: list[asyncio.Task[None]] = []
    coordination: asyncio.Future[list[Any]] | None = None
    heartbeat: asyncio.Task[None] | None = None
    stacks_completed = 0
    failed_lines: deque[str] = deque(maxlen=_FAILED_EVENT_LINES)
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    last_activity = time.monotonic()
    last_stack: str | None = None
    completed_stacks: set[str] = set()
    return_code: int | None = None

    async def _drain(stream: asyncio.StreamReader | None, label: str) -> None:
        nonlocal stacks_completed, last_activity, last_stack
        if stream is None:
            raise RuntimeError(f"{label} pipe was not created")
        async for line in _bounded_stream_lines(stream):
            if not line:
                continue
            last_activity = time.monotonic()
            status_writer.record_line(line, stream=label)

            increment_progress = False
            stack_done = _CDK_STACK_DONE_RE.search(line)
            if stack_done is not None:
                name = stack_done.group(1)
                if name not in completed_stacks:
                    completed_stacks.add(name)
                    stacks_completed += 1
                    status_writer.increment_stacks(name)
                    increment_progress = True

            if _CFN_FAILED_RE.search(line):
                failed_lines.append(_bounded_text(line, _DIAGNOSTIC_LINE_MAX_BYTES))
            stack_match = _CDK_STACK_LINE_RE.search(line)
            if stack_match:
                last_stack = stack_match.group(1)
                status_writer.set_last_stack(last_stack)
            if label == "stderr":
                stderr_tail.append(_bounded_text(line, _DIAGNOSTIC_LINE_MAX_BYTES))

            await _best_effort_client_call(
                progress,
                "set_message",
                line[:_CLIENT_MESSAGE_MAX_CHARS],
            )
            if increment_progress:
                await _best_effort_client_call(progress, "increment")
            if label == "stderr":
                await _best_effort_client_call(
                    ctx,
                    "info",
                    f"stderr: {line[:_CLIENT_MESSAGE_MAX_CHARS]}",
                )

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            if time.monotonic() - last_activity < _HEARTBEAT_INTERVAL_SECONDS:
                continue
            elapsed = int(time.monotonic() - started)
            stack_part = f" (last: {last_stack})" if last_stack else ""
            message = f"still running … {_format_duration(elapsed)} elapsed{stack_part}"
            await _best_effort_client_call(progress, "set_message", message)
            await _best_effort_client_call(ctx, "info", message)

    async def _clean_process_tasks() -> None:
        """Terminate/reap the child and consume every auxiliary task result."""
        nonlocal wait_task
        if process is None:
            return
        if wait_task is None or wait_task.cancelled():
            wait_task = asyncio.create_task(process.wait())
        await _terminate_and_reap(process, wait_task)
        for drain in drains:
            if not drain.done():
                drain.cancel()
        if drains:
            await asyncio.gather(*drains, return_exceptions=True)
        if coordination is not None:
            if not coordination.done():
                coordination.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await coordination

    try:
        if total_units is not None and total_units > 0:
            await _best_effort_client_call(progress, "set_total", int(total_units))

        process = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cli_runner.PROJECT_ROOT),
        )
        status_writer.set_pid(process.pid)

        wait_task = asyncio.create_task(process.wait())
        drains = [
            asyncio.create_task(_drain(process.stdout, "stdout")),
            asyncio.create_task(_drain(process.stderr, "stderr")),
        ]
        heartbeat = asyncio.create_task(_heartbeat())
        # Shield keeps caller cancellation from cancelling wait/drain tasks
        # before the process cleanup path can terminate and reap the child.
        coordination = asyncio.gather(wait_task, *drains)
        results = await asyncio.shield(coordination)
        return_code = int(results[0])
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await _clean_process_tasks()
        status_writer.finish(
            state="cancelled",
            exit_code=process.returncode if process is not None else None,
            error=_PARTIAL_STATE_DISCLAIMER if is_stack_op else "cancelled",
        )
        if is_stack_op:
            raise asyncio.CancelledError(_PARTIAL_STATE_DISCLAIMER) from None
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _clean_process_tasks()
        status_writer.finish(
            state="failed",
            exit_code=process.returncode if process is not None else None,
            error=_bounded_text(
                f"{type(exc).__name__}: {exc}",
                _DIAGNOSTIC_LINE_MAX_BYTES,
            ),
        )
        raise
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat

    duration = int(time.monotonic() - started)
    if return_code is None:
        # Coordination only completes normally after process.wait(). Keep the
        # guard explicit for unusual Process implementations and static safety.
        return_code = process.returncode if process is not None else None
    if return_code is None:
        status_writer.finish(state="failed", error="subprocess return code unavailable")
        raise RuntimeError("subprocess return code unavailable")

    if return_code != 0:
        payload: dict[str, Any] = {
            "error": f"exit_code={return_code}",
            "exit_code": return_code,
            "task_id": task_id,
            "stacks_completed": stacks_completed,
            "duration_seconds": duration,
            "last_stack": last_stack,
            "failed_events": list(failed_lines),
            "stderr_tail": list(stderr_tail),
        }
        if is_stack_op:
            payload["disclaimer"] = _PARTIAL_STATE_DISCLAIMER
        status_writer.finish(
            state="failed", exit_code=return_code, error=f"exit_code={return_code}"
        )
        raise ToolError(json.dumps(payload))

    status_writer.finish(state="succeeded", exit_code=return_code)
    return json.dumps(
        {
            "status": "ok",
            "task_id": task_id,
            "stacks_completed": stacks_completed,
            "duration_seconds": duration,
            "last_stack": last_stack,
        }
    )


def _format_duration(seconds: int) -> str:
    """Render an integer second count as ``HhMmSs`` / ``MmSs`` / ``Ss``."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m{sec:02d}s"
