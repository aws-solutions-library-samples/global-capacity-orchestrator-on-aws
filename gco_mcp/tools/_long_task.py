"""Shared async subprocess runner for long-running MCP tools.

Streams progress through FastMCP's Progress dependency, emits a periodic
heartbeat when the underlying process goes quiet, captures the tail of
stderr for failure surfacing, and raises ``ToolError`` on non-zero exit
with a structured error message so MCP clients can render failures
properly instead of treating them as opaque success-shaped JSON blobs.

In parallel with the MCP wire, every invocation also writes a JSON
status file plus a raw log file under ``~/.gco/tasks/`` via
``_task_status.TaskStatusWriter``. That gives operators (and other
agents) an out-of-band view of what's happening even when the MCP
client drops or buries the streamed notifications. The disk surface
is read by the ``task_status`` / ``task_tail`` MCP tools and by
``gco tasks list/tail``.

Used by every long-running stack-lifecycle tool (deploy_stack, deploy_all,
bootstrap_cdk, destroy_stack, destroy_all) and by images_build / images_push.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import signal
import time
from collections import deque
from collections.abc import Sequence
from typing import Any

from fastmcp.exceptions import ToolError

from tools._task_status import TaskStatusWriter, make_task_id

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``_run_long_task`` -> ``diagrams/code_diagrams/gco_mcp/tools/_long_task._run_long_task.html``
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


_CFN_FAILED_RE = re.compile(r"(CREATE|UPDATE|DELETE)_FAILED")
# Matches the CDK stack-progress prefix it prints in the form
# ``  ✅  gco-global``, ``  ❌  gco-us-east-1``, or ``gco-us-east-1: deploying...``.
_CDK_STACK_LINE_RE = re.compile(r"\b(gco-[a-z0-9-]+)\b")
# Recognises CDK's per-stack done/fail signal: ``✅  gco-global`` (deploy)
# or ``✅  gco-global: destroyed`` (destroy). One match per stack →
# one progress increment so the client's percentage tracks stack
# completion, not individual CFN resource events (which jump from 0
# to dozens mid-stack and reset on the next stack).
_CDK_STACK_DONE_RE = re.compile(r"[✅✨]\s+(gco-[a-z0-9-]+)\b")
_CANCEL_GRACE_SECONDS = 10
# How often to emit a heartbeat status when the subprocess is silent. CDK
# can be quiet for minutes during EKS cluster creation or VPC tear-down
# while CloudFormation churns away. Without a heartbeat, MCP clients
# render the tool as wedged.
_HEARTBEAT_INTERVAL_SECONDS = 30
# How many tail lines of stderr to retain for the failure payload. CDK
# failure summaries typically fit in 30-50 lines; we cap at 80 to avoid
# blowing up the ToolError message size.
_STDERR_TAIL_LINES = 80
_PARTIAL_STATE_DISCLAIMER = (
    "Partial CloudFormation state may remain — inspect via stack_status or the AWS console."
)


def _argv_has_traversal(argv: Sequence[str]) -> tuple[int, str] | None:
    """Return (index, offending_value) for the first non-flag arg with a ``..``
    segment, else None.

    A non-flag element is one that does not start with ``-``. The check matches
    the convention used by ``mcp.cli_runner._run_cli`` so the rejection shape
    is consistent across short- and long-running tools.
    """
    for i, v in enumerate(argv):
        if v.startswith("-"):
            continue
        if ".." in v.split("/") or ".." in v.split("\\"):
            return (i, v[:100])
    return None


async def _run_long_task(
    argv: Sequence[str],
    *,
    ctx: Any,
    progress: Any,
    is_stack_op: bool = True,
    total_units: int | None = None,
) -> str:
    """Run a long-running subprocess with rich status reporting.

    Spawns ``argv`` via :func:`asyncio.create_subprocess_exec`, drains stdout
    and stderr concurrently, and streams progress through three channels:

    * ``progress.set_message(...)`` for every meaningful line (≤ 200 chars)
    * ``progress.increment()`` once per stack completion (CDK's ``✅ gco-…``
      / ``✨ gco-…`` lines), so the progress counter tracks AWS-side stack
      milestones rather than per-resource CFN events
    * ``ctx.info(...)`` for stderr lines so MCP clients with a Context
      surface render them through the standard MCP logging channel

    When ``total_units`` is supplied, it's forwarded to
    ``progress.set_total(total_units)`` once at startup so the client
    can render a proper percentage. Callers that know how many stacks
    they're processing (e.g. ``deploy_all`` knowing the cdk.json
    deployment_regions count) should pass it; callers that don't can
    omit it and the client falls back to indeterminate progress.

    A periodic heartbeat fires every ``_HEARTBEAT_INTERVAL_SECONDS`` when
    no output has been seen, so the client never sees a stalled progress
    state during quiet CDK phases (EKS cluster creation, VPC teardown).
    The heartbeat carries the elapsed wall-clock and the most recent
    stack name observed in the output, in the form
    ``"still running … 4m12s elapsed (last: gco-us-east-1)"``.

    On cancellation, sends ``SIGTERM``, waits up to ``_CANCEL_GRACE_SECONDS``
    for graceful shutdown, sends ``SIGKILL`` on timeout, cancels the drain
    tasks, and re-raises ``CancelledError``. When ``is_stack_op`` is True,
    the re-raised exception's message includes a CloudFormation
    partial-state disclaimer so operators know AWS state may be inconsistent.

    Returns a JSON string on success:

    * ``{"status": "ok", "stacks_completed": <n>, "duration_seconds": <s>,
       "last_stack": <str|null>}``

    Raises :class:`fastmcp.exceptions.ToolError` on subprocess failure with
    a structured message that includes:

    * The exit code
    * The number of stacks completed before failure
    * The last stack the run was working on (parsed from output)
    * Whether any ``*_FAILED`` line was seen
    * The tail of stderr (up to ``_STDERR_TAIL_LINES`` lines)
    * The partial-state disclaimer (when ``is_stack_op=True``)

    The structured payload lives in the ToolError ``args[0]`` as a JSON
    string so MCP clients can deserialize and render it; FastMCP forwards
    the message to the client's ``CallToolResult.content`` automatically.

    Path-traversal rejection is unchanged — returns the same JSON stub
    without spawning a subprocess.
    """
    hit = _argv_has_traversal(argv)
    if hit is not None:
        idx, val = hit
        return json.dumps({"error": "path_traversal_detected", "argv_index": idx, "value": val})

    started = time.monotonic()
    if total_units is not None and total_units > 0:
        # set_total may not be implemented by every Progress impl on
        # every fastmcp version — best-effort.
        with contextlib.suppress(AttributeError, NotImplementedError):
            await progress.set_total(int(total_units))

    # ``asyncio.create_subprocess_exec`` itself can be cancelled mid-
    # call (the inner ``_make_subprocess_transport`` awaits a waiter).
    # When that happens for a stack-op invocation, surface the same
    # partial-state disclaimer the post-creation cancel path emits —
    # AWS state may already be in flight even if our local subprocess
    # never got a PID. Without this guard a fast cancellation lands as
    # a bare ``CancelledError`` and the operator sees no warning that
    # CloudFormation may need inspection.
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except asyncio.CancelledError:
        if is_stack_op:
            raise asyncio.CancelledError(_PARTIAL_STATE_DISCLAIMER) from None
        raise

    # Disk-backed status: parallel observability channel that doesn't
    # depend on the MCP client surfacing notifications. Reads land in
    # the task_status / task_tail tools and in ``gco tasks``.
    #
    # Derive a human-readable tool name from the argv. ``gco stacks
    # deploy-all`` → ``stacks_deploy_all``; the dash is normalized to
    # an underscore so the IDs are filesystem-safe and grep-friendly.
    # Plain ``argv[0]`` (the bare interpreter or finch path) is the
    # fallback for non-gco invocations like ``images_build`` which
    # may shell out directly.
    if len(argv) >= 3 and argv[0] == "gco":
        tool_name = f"{argv[1]}_{argv[2].replace('-', '_')}"
    elif len(argv) >= 2 and argv[0] == "gco":
        tool_name = argv[1]
    else:
        tool_name = argv[0] if argv else "task"
    task_id = make_task_id(tool_name)
    status_writer = TaskStatusWriter(
        task_id=task_id,
        tool=tool_name,
        argv=list(argv),
        pid=proc.pid,
        total_units=total_units,
    )

    stacks_completed = 0
    failed_lines: list[str] = []
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    last_activity = time.monotonic()
    last_stack: str | None = None
    completed_stacks_set: set[str] = set()

    async def _drain(stream: asyncio.StreamReader | None, label: str) -> None:
        nonlocal stacks_completed, last_activity, last_stack
        assert stream is not None  # PIPE is set above for both streams
        async for raw in stream:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            last_activity = time.monotonic()
            await progress.set_message(line[:200])
            status_writer.record_line(line, stream=label)
            stack_done = _CDK_STACK_DONE_RE.search(line)
            if stack_done is not None:
                # Increment once per unique stack — CDK can echo the
                # done line on stdout and stderr, double-counting
                # would mis-report progress.
                name = stack_done.group(1)
                if name not in completed_stacks_set:
                    completed_stacks_set.add(name)
                    stacks_completed += 1
                    await progress.increment()
                    status_writer.increment_stacks(name)
            if _CFN_FAILED_RE.search(line):
                failed_lines.append(line[:200])
            stack_match = _CDK_STACK_LINE_RE.search(line)
            if stack_match:
                last_stack = stack_match.group(1)
                status_writer.set_last_stack(last_stack)
            if label == "stderr":
                stderr_tail.append(line)
                await ctx.info(f"stderr: {line[:200]}")

    async def _heartbeat() -> None:
        """Emit a periodic 'still running' status when the subprocess is quiet.

        The heartbeat does NOT call ``progress.increment()`` — that's
        reserved for AWS-side milestones. It only updates the message
        so the client sees activity, and emits a parallel ``ctx.info``
        so clients without a Progress observer still get the signal.
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            silent_for = time.monotonic() - last_activity
            if silent_for < _HEARTBEAT_INTERVAL_SECONDS:
                continue
            elapsed = int(time.monotonic() - started)
            stack_part = f" (last: {last_stack})" if last_stack else ""
            msg = f"still running … {_format_duration(elapsed)} elapsed{stack_part}"
            await progress.set_message(msg)
            await ctx.info(msg)

    drains = [
        asyncio.create_task(_drain(proc.stdout, "stdout")),
        asyncio.create_task(_drain(proc.stderr, "stderr")),
    ]
    heartbeat = asyncio.create_task(_heartbeat())

    try:
        rc = await proc.wait()
        await asyncio.gather(*drains, return_exceptions=True)
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_CANCEL_GRACE_SECONDS)
            except TimeoutError:
                if proc.returncode is None:
                    proc.send_signal(signal.SIGKILL)
                await proc.wait()
        for d in drains:
            d.cancel()
        heartbeat.cancel()
        status_writer.finish(
            state="cancelled",
            exit_code=proc.returncode,
            error=_PARTIAL_STATE_DISCLAIMER if is_stack_op else "cancelled",
        )
        if is_stack_op:
            raise asyncio.CancelledError(_PARTIAL_STATE_DISCLAIMER) from None
        raise
    finally:
        heartbeat.cancel()

    duration = int(time.monotonic() - started)

    if rc != 0:
        # Compose a structured failure payload so the client sees the
        # full error context (which stack failed, the stderr tail, the
        # partial-state disclaimer) rather than an opaque exit-code stub.
        payload: dict[str, Any] = {
            "error": f"exit_code={rc}",
            "exit_code": rc,
            "task_id": task_id,
            "stacks_completed": stacks_completed,
            "duration_seconds": duration,
            "last_stack": last_stack,
            "failed_events": failed_lines[:10],
            "stderr_tail": list(stderr_tail),
        }
        if is_stack_op:
            payload["disclaimer"] = _PARTIAL_STATE_DISCLAIMER
        status_writer.finish(state="failed", exit_code=rc, error=f"exit_code={rc}")
        # ToolError surfaces the payload to the client as a tool-level
        # failure (CallToolResult.is_error=True). Inline callers see it
        # via the FastMCP transport; agent clients render it as a
        # tool-error message rather than success-shaped data.
        raise ToolError(json.dumps(payload))

    status_writer.finish(state="succeeded", exit_code=rc)
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
