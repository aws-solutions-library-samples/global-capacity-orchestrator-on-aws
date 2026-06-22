"""
Disk-backed status reporting for long-running MCP tools.

The MCP spec lets tools stream progress and log notifications back to the
calling client, but client-side rendering of those notifications is
inconsistent. Some clients drop them, some bury them in a debug panel,
some never surface them at all. The result: a 30-60 minute deploy_all
looks "wedged" to the user even though the underlying CLI is producing
output every few seconds.

This module gives every long-running tool a parallel observability
channel that doesn't depend on the MCP wire at all:

* A JSON ``status`` file at ``~/.gco/tasks/{task_id}.json`` updated on
  every progress event (atomic via tempfile + ``os.replace``).
* A raw ``log`` file at ``~/.gco/tasks/{task_id}.log`` containing the
  full interleaved stdout+stderr of the subprocess, so operators have
  the same forensic record CDK would have left in their terminal.
* Orphan detection on read: when the status reports ``state=running``
  but the recorded PID is no longer alive, the returned dict is
  re-stamped to ``state=orphaned`` so callers see honest data even
  when the MCP wrapper crashed without a final write.

Two MCP tools (``task_status`` / ``task_tail``) and one CLI group
(``gco tasks list/tail/prune``) read these files. Both surfaces are
read-only — the writer always lives in ``_run_long_task``.

The status directory is configurable via ``GCO_TASK_STATUS_DIR`` so
unit tests can isolate to ``tmp_path``. ``GCO_DISABLE_TASK_STATUS=1``
skips file emission entirely (kept as an escape hatch for sandboxed
environments where ``~/.gco`` isn't writable).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

# Keep at most this many task files (status + log) in the directory.
# When a new task starts, anything older than the most recent N gets
# pruned. 50 is enough for a couple of full deploy cycles plus a
# handful of one-off image pushes.
_TASK_RETENTION = 50

# Tail buffer kept in the status file. Larger than what any single
# message line will need, but capped so the JSON file stays small.
_STATUS_TAIL_LINES = 20

# Debounce window for atomic status writes. Per-line writes would do
# hundreds of fsyncs during a noisy CDK phase; this batches them so
# we write at most ~2 times per second under sustained output.
_STATUS_WRITE_DEBOUNCE_SECONDS = 0.5

# How long to consider a process "alive" — really just a guard so a
# stale PID that's been recycled by another unrelated process isn't
# falsely reported as still running. We don't try to be clever about
# PID recycling beyond this; ``ps``-style verification would need
# command-line matching and is out of scope.
_PID_ALIVE_SIGNAL = 0


def status_dir() -> Path:
    """Resolve the status directory honouring the env override.

    Tests set ``GCO_TASK_STATUS_DIR`` to a ``tmp_path`` so they don't
    write to the developer's real home dir.
    """
    override = os.environ.get("GCO_TASK_STATUS_DIR")
    if override:
        return Path(override)
    return Path.home() / ".gco" / "tasks"


def task_status_enabled() -> bool:
    """``True`` unless the operator explicitly opted out.

    The opt-out is a defensive escape hatch — sandboxed CI runs and
    container builds that mount a read-only home directory can set
    ``GCO_DISABLE_TASK_STATUS=1`` to skip the disk writes without
    losing any of the MCP wire-side observability.
    """
    return os.environ.get("GCO_DISABLE_TASK_STATUS", "").lower() not in {"1", "true", "yes"}


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``target`` atomically.

    Uses ``tempfile.NamedTemporaryFile`` in the same directory so
    ``os.replace`` is a same-filesystem rename (atomic on POSIX).
    Readers always see either the previous file or the new one,
    never a partial write.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # delete=False so we can rename it before the context manager closes.
    fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 - explicit close+replace below
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(payload, fd, indent=2, sort_keys=True)
        fd.write("\n")
        fd.flush()
        os.fsync(fd.fileno())
    finally:
        fd.close()
    os.replace(fd.name, target)


def _is_pid_alive(pid: int | None) -> bool:
    """Best-effort liveness check via signal 0.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` when the PID is
    free, ``PermissionError`` when the PID belongs to a process we
    don't own (still alive), and returns ``None`` on success. Any
    other ``OSError`` we treat conservatively as "not alive" so an
    edge case can't strand a task in ``running`` forever.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, _PID_ALIVE_SIGNAL)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _prune_old_tasks(directory: Path, keep: int = _TASK_RETENTION) -> None:
    """Drop the oldest task files so the directory doesn't grow unbounded.

    Tasks are paired (``{task_id}.json`` + ``{task_id}.log``); we sort
    by mtime on the JSON file and remove pairs beyond the retention
    cap. Errors are swallowed — pruning is best-effort and must never
    break a live task's status emission.
    """
    try:
        if not directory.exists():
            return
        json_files = sorted(
            directory.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in json_files[keep:]:
            with _suppress_oserror():
                stale.unlink()
            log = stale.with_suffix(".log")
            with _suppress_oserror():
                if log.exists():
                    log.unlink()
    except OSError:
        # Pruning is best-effort. Never raise from here.
        return


class _suppress_oserror:
    """Compact context manager that swallows OSError only.

    ``contextlib.suppress(OSError)`` would do, but we use the dedicated
    type so future readers can grep for the intentional swallows.
    """

    def __enter__(self) -> _suppress_oserror:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def make_task_id(tool_name: str) -> str:
    """Generate a sortable, collision-resistant task ID.

    Format: ``{tool_name}-{millis_since_epoch}-{counter}``. The
    millisecond timestamp gives natural sortability; the
    monotonically-incrementing process-local counter ensures a tight
    loop of calls (or a multi-threaded burst) never collides on a
    single millisecond. Two tasks landing in the same millisecond
    sort by counter, which is the actual call order.
    """
    counter = _next_task_counter()
    return f"{tool_name}-{int(time.time() * 1000)}-{counter}"


_TASK_COUNTER_LOCK = threading.Lock()
_TASK_COUNTER = 0


def _next_task_counter() -> int:
    """Return a monotonically-increasing counter, thread-safe."""
    global _TASK_COUNTER
    with _TASK_COUNTER_LOCK:
        _TASK_COUNTER += 1
        return _TASK_COUNTER


class TaskStatusWriter:
    """Disk-backed status emitter for one long-running tool invocation.

    Owns the lifecycle of a ``{task_id}.json`` + ``{task_id}.log``
    pair. Use as a context manager — the ``__exit__`` flushes a
    final ``state=succeeded|failed|cancelled`` write so observers
    always see a terminal record.

    Thread-safety: the lock guards the in-memory tail buffer and
    debounce timestamps so it's safe to call ``record_line`` from
    the stdout and stderr drain coroutines concurrently. The
    underlying file ops are single-writer per task by construction.
    """

    def __init__(
        self,
        task_id: str,
        tool: str,
        argv: list[str],
        *,
        pid: int | None,
        total_units: int | None = None,
    ) -> None:
        self.task_id = task_id
        self.tool = tool
        self._argv = list(argv)
        self._pid = pid
        self._total_units = total_units
        self._enabled = task_status_enabled()

        self._dir = status_dir()
        self._status_path = self._dir / f"{task_id}.json"
        self._log_path = self._dir / f"{task_id}.log"

        self._started_at_iso = _now_iso()
        self._started_monotonic = time.monotonic()
        self._stacks_completed = 0
        self._last_stack: str | None = None
        self._last_message: str | None = None
        self._tail: deque[str] = deque(maxlen=_STATUS_TAIL_LINES)
        self._state = "running"
        self._exit_code: int | None = None
        self._error: str | None = None

        self._lock = threading.Lock()
        self._last_write_ts = 0.0
        self._log_fp: IO[str] | None = None

        if self._enabled:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                _prune_old_tasks(self._dir)
                self._log_fp = open(  # noqa: SIM115 - long-lived file handle, closed in finish()
                    self._log_path, "w", encoding="utf-8", buffering=1
                )
            except OSError:
                # If we can't open the log, fall back to status-only.
                self._log_fp = None
            self._write_status_now()

    # --- recording ------------------------------------------------------

    def record_line(self, line: str, *, stream: str) -> None:
        """Append a single output line and refresh the status file.

        ``stream`` is "stdout" or "stderr" — used as a prefix in the
        log file so readers can tell them apart in interleaved order.
        Status writes are debounced to avoid hundreds of fsyncs on
        noisy phases; the log file is unbuffered.
        """
        if not self._enabled:
            return
        with self._lock:
            self._tail.append(line)
            self._last_message = line
            now = time.monotonic()
            if now - self._last_write_ts >= _STATUS_WRITE_DEBOUNCE_SECONDS:
                self._last_write_ts = now
                self._write_status_now()
            if self._log_fp is not None:
                with _suppress_oserror():
                    self._log_fp.write(f"[{stream}] {line}\n")

    def increment_stacks(self, stack_name: str) -> None:
        """Record that one more stack finished.

        Triggers an immediate (un-debounced) write so the
        ``stacks_completed`` counter is fresh for any reader polling
        between stack milestones.
        """
        if not self._enabled:
            return
        with self._lock:
            self._stacks_completed += 1
            self._last_stack = stack_name
            self._last_write_ts = time.monotonic()
            self._write_status_now()

    def set_last_stack(self, stack_name: str) -> None:
        """Update the last-seen stack name without bumping the counter."""
        if not self._enabled:
            return
        with self._lock:
            self._last_stack = stack_name

    def finish(
        self,
        *,
        state: str,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """Stamp a terminal state and flush.

        ``state`` is one of "succeeded", "failed", "cancelled". Once
        finished, the status file is no longer touched — readers
        rely on the timestamp + state to know they have the final
        record.
        """
        if not self._enabled:
            return
        with self._lock:
            self._state = state
            self._exit_code = exit_code
            self._error = error
            self._write_status_now()
            if self._log_fp is not None:
                with _suppress_oserror():
                    self._log_fp.flush()
                    self._log_fp.close()
                self._log_fp = None

    # --- internals ------------------------------------------------------

    def _build_payload(self) -> dict[str, Any]:
        elapsed = int(time.monotonic() - self._started_monotonic)
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "tool": self.tool,
            "argv": self._argv,
            "pid": self._pid,
            "started_at": self._started_at_iso,
            "updated_at": _now_iso(),
            "elapsed_seconds": elapsed,
            "state": self._state,
            "stacks_completed": self._stacks_completed,
            "last_stack": self._last_stack,
            "last_message": self._last_message,
            "tail": list(self._tail),
            "log_path": str(self._log_path),
        }
        if self._total_units is not None and self._total_units > 0:
            payload["stacks_total"] = self._total_units
        if self._exit_code is not None:
            payload["exit_code"] = self._exit_code
        if self._error is not None:
            payload["error"] = self._error
        return payload

    def _write_status_now(self) -> None:
        try:
            _atomic_write_json(self._status_path, self._build_payload())
        except OSError:
            # Disk emission is best-effort — never let a write failure
            # crash the live tool invocation.
            return


# ---------------------------------------------------------------------------
# Read-side helpers for the task_status / task_tail tools and the CLI.
# ---------------------------------------------------------------------------


def list_tasks(directory: Path | None = None) -> list[dict[str, Any]]:
    """Return all known task status records, newest first.

    Each record gets ``is_alive`` re-computed from the recorded PID,
    and ``state`` is rewritten to ``"orphaned"`` when a record claims
    ``running`` but the PID is dead. This is the canonical way for
    callers to detect tasks whose MCP wrapper exited unexpectedly
    while the underlying CDK kept going.
    """
    directory = directory or status_dir()
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(
        directory.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        record = _read_status_file(path)
        if record is not None:
            records.append(record)
    return records


def get_task(task_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """Return one task record by ID, with ``is_alive`` / orphan rewriting.

    Returns ``None`` when the file is missing — callers can use that
    to distinguish "no such task" from "task ran and finished".
    """
    directory = directory or status_dir()
    path = directory / f"{task_id}.json"
    if not path.exists():
        return None
    return _read_status_file(path)


def tail_log(task_id: str, lines: int = 100, directory: Path | None = None) -> list[str]:
    """Return the last ``lines`` lines of the task's raw log.

    Empty list when the log file is missing, the task hasn't emitted
    anything yet, or the directory is unreadable. Lines do NOT include
    the trailing newline so callers don't have to strip them.
    """
    if lines <= 0:
        return []
    directory = directory or status_dir()
    path = directory / f"{task_id}.log"
    if not path.exists():
        return []
    try:
        # deque maxlen does the bounded read for us with constant memory.
        with open(path, encoding="utf-8", errors="replace") as fp:
            buf: deque[str] = deque(fp, maxlen=lines)
        return [line.rstrip("\n") for line in buf]
    except OSError:
        return []


def prune_tasks(keep: int = _TASK_RETENTION, directory: Path | None = None) -> int:
    """Remove all but the most-recent ``keep`` task files.

    Returns the number of task IDs removed (one count per pair —
    a JSON+log removal counts once). Useful for the ``gco tasks
    prune`` CLI when an operator wants a manual sweep.
    """
    directory = directory or status_dir()
    if not directory.exists():
        return 0
    json_files = sorted(
        directory.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for stale in json_files[keep:]:
        with _suppress_oserror():
            stale.unlink()
            removed += 1
        log = stale.with_suffix(".log")
        with _suppress_oserror():
            if log.exists():
                log.unlink()
    return removed


def _read_status_file(path: Path) -> dict[str, Any] | None:
    """Load a single status JSON, applying liveness/orphan post-processing.

    Returns ``None`` only when the file is unreadable or malformed —
    a transient half-written file (which atomic writes shouldn't
    produce, but defence in depth) is treated as missing rather than
    raised.
    """
    try:
        text = path.read_text(encoding="utf-8")
        record = json.loads(text)
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


def task_ids_for(records: Iterable[dict[str, Any]]) -> list[str]:
    """Project a sequence of task records to their IDs (helper for tests)."""
    return [r["task_id"] for r in records if "task_id" in r]
