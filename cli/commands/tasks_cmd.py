"""Long-running task observability commands.

Mirrors the ``task_status`` / ``task_tail`` MCP tools so operators can
inspect the same on-disk status records from a terminal:

* ``gco tasks list`` — newest-first table of recent invocations
* ``gco tasks show TASK_ID`` — full record for one task
* ``gco tasks tail TASK_ID -n 100 [-f]`` — last N lines of raw output,
  with ``-f`` polling like ``tail -f``
* ``gco tasks prune`` — drop all but the most recent N records

Long-running MCP tools (``deploy_all``, ``destroy_all``,
``bootstrap_cdk``, ``deploy_stack``, ``destroy_stack``,
``images_build``, ``images_push``) record progress to
``~/.gco/tasks/{task_id}.json`` and the raw subprocess output to
``~/.gco/tasks/{task_id}.log`` on every line. This module reads
those artifacts and never writes them — the writer lives in
``gco_mcp/tools/_task_status.py``.
"""

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import click


def _status_dir() -> Path:
    """Honour ``GCO_TASK_STATUS_DIR`` for tests, fall back to ``~/.gco/tasks``.

    Mirrors ``mcp.tools._task_status.status_dir`` so the CLI and the MCP
    server always read from the same place. We don't import the MCP
    helper directly because ``cli/`` and ``gco_mcp/`` are separate top-level
    packages and we want this command to work without the MCP install.
    """
    import os

    override = os.environ.get("GCO_TASK_STATUS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".gco" / "tasks"


def _is_pid_alive(pid: int | None) -> bool:
    """Best-effort liveness check via ``os.kill(pid, 0)``.

    Returns ``False`` when the PID is missing/zero or the OS reports
    the process gone. Returns ``True`` for live processes including
    those owned by other users (``PermissionError``). Anything else is
    treated as not-alive so an unexpected ``OSError`` can't strand a
    task in ``running`` forever.
    """
    import os

    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_status(path: Path) -> dict[str, Any] | None:
    """Load one status JSON, applying the orphan rewrite.

    Identical semantics to ``mcp.tools._task_status._read_status_file``:
    re-checks the PID and rewrites ``state=running`` to ``orphaned``
    when the recorded process is dead. Kept as a local copy so the
    CLI doesn't need ``gco_mcp/`` on the import path.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    is_alive = _is_pid_alive(pid if isinstance(pid, int) else None)
    record["is_alive"] = is_alive
    if record.get("state") == "running" and not is_alive:
        record["state"] = "orphaned"
    return record


def _list_records(directory: Path) -> list[dict[str, Any]]:
    """Return all records newest-first."""
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        record = _read_status(path)
        if record is not None:
            out.append(record)
    return out


def _format_state(state: str) -> str:
    """Colour-code state for terminal output. Plain text when not a TTY."""
    if not sys.stdout.isatty():
        return state
    palette = {
        "running": "\x1b[36mrunning\x1b[0m",  # cyan
        "succeeded": "\x1b[32msucceeded\x1b[0m",  # green
        "failed": "\x1b[31mfailed\x1b[0m",  # red
        "cancelled": "\x1b[33mcancelled\x1b[0m",  # yellow
        "orphaned": "\x1b[35morphaned\x1b[0m",  # magenta
    }
    return palette.get(state, state)


def _format_elapsed(seconds: int | None) -> str:
    """Render an integer second count compactly: ``s`` / ``MmSs`` / ``HhMm``."""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


@click.group()
def tasks() -> None:
    """Inspect long-running MCP / CLI task status.

    Commands like ``gco stacks deploy-all`` and ``gco images push`` write
    progress records and raw subprocess logs to ``~/.gco/tasks/`` so you
    can observe them without parsing terminal scrollback. ``gco tasks
    list/show/tail`` read those files; the writer is in the MCP tool
    runner.
    """


@tasks.command("list")
@click.option(
    "-n",
    "--limit",
    type=int,
    default=20,
    show_default=True,
    help="Maximum records to display (newest first).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON instead of the table.",
)
def tasks_list(limit: int, as_json: bool) -> None:
    """List recent task invocations newest-first.

    Shows tool, state (with orphan rewriting for dead PIDs), elapsed
    wall-clock, stacks completed, and the last stack name observed.
    Pass ``--json`` for machine-readable output you can pipe to ``jq``.
    """
    records = _list_records(_status_dir())[:limit] if limit > 0 else _list_records(_status_dir())

    if as_json:
        click.echo(json.dumps({"tasks": records}, indent=2, sort_keys=True))
        return

    if not records:
        click.echo(
            "No tasks recorded yet. Run a long-running command (e.g. 'gco stacks deploy-all') to populate ~/.gco/tasks/."
        )
        return

    header = (
        f"{'TASK ID':<40}  {'TOOL':<18}  {'STATE':<11}  {'ELAPSED':<8}  {'STACKS':<10}  LAST STACK"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for r in records:
        task_id = (r.get("task_id") or "")[:40]
        tool = (r.get("tool") or "")[:18]
        state = _format_state(r.get("state") or "?")
        elapsed = _format_elapsed(r.get("elapsed_seconds"))
        stacks_completed = r.get("stacks_completed") or 0
        stacks_total = r.get("stacks_total")
        stacks = f"{stacks_completed}/{stacks_total}" if stacks_total else f"{stacks_completed}"
        last_stack = r.get("last_stack") or "-"
        # State string may include ANSI codes — pad on the visible width.
        visible_state = r.get("state") or "?"
        state_pad = " " * max(0, 11 - len(visible_state))
        click.echo(
            f"{task_id:<40}  {tool:<18}  {state}{state_pad}  {elapsed:<8}  {stacks:<10}  {last_stack}"
        )


@tasks.command("show")
@click.argument("task_id")
def tasks_show(task_id: str) -> None:
    """Print the full JSON record for one task.

    Useful when ``gco tasks list`` shows a task that needs deeper
    inspection — argv, exit code, stderr tail, etc.
    """
    path = _status_dir() / f"{task_id}.json"
    record = _read_status(path)
    if record is None:
        click.echo(f"Task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(json.dumps(record, indent=2, sort_keys=True))


@tasks.command("tail")
@click.argument("task_id")
@click.option(
    "-n",
    "--lines",
    type=int,
    default=100,
    show_default=True,
    help="Lines to show.",
)
@click.option(
    "-f",
    "--follow",
    is_flag=True,
    help="Follow the log file (poll for new lines, like 'tail -f').",
)
def tasks_tail(task_id: str, lines: int, follow: bool) -> None:
    """Print the last N lines of a task's raw output log.

    Each line is prefixed with ``[stdout]`` or ``[stderr]`` so you can
    tell which stream produced it. ``--follow`` keeps polling the file
    until interrupted, mirroring ``tail -f``.
    """
    log_path = _status_dir() / f"{task_id}.log"
    if not log_path.exists():
        click.echo(f"No log for task: {task_id}", err=True)
        sys.exit(1)

    # Initial tail.
    from collections import deque

    try:
        with open(log_path, encoding="utf-8", errors="replace") as fp:
            buf: deque[str] = deque(fp, maxlen=lines if lines > 0 else 0)
    except OSError as e:
        click.echo(f"Failed to read log: {e}", err=True)
        sys.exit(1)

    for line in buf:
        click.echo(line.rstrip("\n"))

    if not follow:
        return

    # Follow mode: poll the file every 500ms.
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fp:
            fp.seek(0, 2)  # end of file
            while True:
                chunk = fp.read()
                if chunk:
                    click.echo(chunk, nl=False)
                else:
                    # Stop following once the task is no longer running.
                    record = _read_status(_status_dir() / f"{task_id}.json")
                    if record is not None and record.get("state") not in {"running"}:
                        return
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return


@tasks.command("prune")
@click.option(
    "-k",
    "--keep",
    type=int,
    default=50,
    show_default=True,
    help="Keep the N most recent tasks. Older are deleted.",
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation.")
def tasks_prune(keep: int, yes: bool) -> None:
    """Delete old task records, keeping the most recent N.

    Useful if ``~/.gco/tasks/`` has accumulated stale records and you
    want a manual sweep. The MCP runner also auto-prunes on every new
    task start, so this is purely for ad-hoc cleanup.
    """
    directory = _status_dir()
    if not directory.exists():
        click.echo("No task directory yet — nothing to prune.")
        return

    json_files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    stale = json_files[keep:]
    if not stale:
        click.echo(f"Already at or below {keep} task(s). Nothing to do.")
        return

    if not yes:
        click.confirm(
            f"Delete {len(stale)} task record(s) older than the {keep} most recent?",
            abort=True,
        )

    removed = 0
    for path in stale:
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
        log_path = path.with_suffix(".log")
        if log_path.exists():
            with contextlib.suppress(OSError):
                log_path.unlink()

    click.echo(f"Removed {removed} task record(s).")
