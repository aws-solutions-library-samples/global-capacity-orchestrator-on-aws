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
* A private, size-bounded ``log`` file at
  ``~/.gco/tasks/{task_id}.log`` containing interleaved stdout+stderr
  records from the subprocess.
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
import re
import stat
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

# Task IDs are used as filenames. Keep the accepted alphabet deliberately
# narrower than a generic filesystem name: generated IDs and FastMCP's UUID-like
# IDs fit, while path separators, control characters, drive prefixes, and
# special ``.`` / ``..`` components cannot reach a filesystem API.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Disk observability must remain bounded even when a child emits a giant line or
# never stops logging. The status JSON keeps a smaller per-line projection; the
# raw log has independent per-record and whole-file ceilings; tail readers cap
# both caller-requested line count and bytes read from disk.
_STATUS_LINE_MAX_BYTES = 8 * 1024
_STATUS_FILE_MAX_BYTES = 1 * 1024 * 1024
_STATUS_TOOL_MAX_BYTES = 256
_STATUS_ARG_MAX_ITEMS = 128
_STATUS_ARG_MAX_BYTES = 4 * 1024
_TASK_LOG_LINE_MAX_BYTES = 64 * 1024
_TASK_LOG_MAX_BYTES = 10 * 1024 * 1024
_TASK_TAIL_MAX_LINES = 500
_TASK_TAIL_MAX_BYTES = 1 * 1024 * 1024
_TRUNCATED_TEXT = "...[truncated]"
_LOG_TRUNCATED_RECORD = b"[gco-mcp] log truncated at configured byte limit\n"

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

# Serialize task creation/pruning inside one MCP process. Atomic file writes
# already protect readers; this lock additionally prevents two local writers
# from pruning between each other's status and log creation windows.
_TASK_ARTIFACT_LOCK = threading.RLock()


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


def is_valid_task_id(task_id: object) -> bool:
    """Return whether ``task_id`` is safe to use as one flat filename stem."""
    return (
        isinstance(task_id, str)
        and task_id not in {".", ".."}
        and _TASK_ID_RE.fullmatch(task_id) is not None
    )


def _bounded_text(value: str, max_bytes: int) -> str:
    """Return UTF-8 text no larger than ``max_bytes``, with a marker on truncation."""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = _TRUNCATED_TEXT.encode()
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker)]
    return prefix.decode("utf-8", errors="ignore") + _TRUNCATED_TEXT


def _open_status_directory(directory: Path) -> int:
    """Open the canonical status root and verify its identity without links."""
    root = directory.expanduser().resolve(strict=True)
    before = root.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("task status root is not a directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(root, flags)
    try:
        after = os.fstat(fd)
        if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise OSError("task status root changed while opening")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_task_artifact(
    task_id: object,
    suffix: str,
    directory: Path,
) -> int | None:
    """Open one flat task artifact relative to a verified root descriptor.

    Flat task-ID validation blocks lexical traversal. Descriptor-relative
    no-follow opening then prevents final-link and status-root replacement
    races from redirecting reads outside the configured directory. Nonblocking
    open plus regular/single-link identity checks reject FIFOs, devices,
    sockets, hard links, and raced replacements before any read occurs.
    """
    if not is_valid_task_id(task_id) or suffix not in {".json", ".log"}:
        return None
    try:
        root_fd = _open_status_directory(directory)
    except OSError, RuntimeError:
        return None
    try:
        name = f"{task_id}{suffix}"
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(name, flags, dir_fd=root_fd)
        try:
            after = os.fstat(fd)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise OSError("task artifact changed while opening")
            return fd
        except Exception:
            os.close(fd)
            raise
    except OSError, NotImplementedError, RuntimeError, TypeError, ValueError:
        return None
    finally:
        os.close(root_fd)


def _open_regular_nofollow(path: Path) -> int:
    """Open an existing regular file without following a final symlink.

    ``lstat`` plus post-open identity comparison provides a portable guard; on
    platforms exposing ``O_NOFOLLOW`` the kernel also rejects a swapped symlink
    atomically. Non-regular artifacts (directories, devices, FIFOs, sockets)
    are rejected so a status read can never block on an attacker-controlled
    special file.
    """
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OSError("task artifact is not a private regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise OSError("task artifact changed while opening")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_log(path: Path) -> int:
    """Create or safely reset one private regular log file.

    ``O_TRUNC`` is deliberately avoided until an existing path has passed
    no-follow, regular-file, single-link, and identity checks. This prevents a
    planted symlink or hard link from turning task startup into an arbitrary
    host-file truncation primitive, including on platforms without
    ``O_NOFOLLOW``.
    """
    base_flags = (
        os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError("task log is not a private regular file") from exc
        fd = os.open(path, base_flags)
        try:
            after = os.fstat(fd)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise OSError("task log changed while opening")
            os.ftruncate(fd, 0)
        except Exception:
            os.close(fd)
            raise

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError("task log is not a private regular file")
        os.fchmod(fd, 0o600)
        return fd
    except Exception:
        os.close(fd)
        raise


def _sorted_regular_json_files(directory: Path) -> list[Path]:
    """Return private regular ``*.json`` children sorted newest first."""
    candidates: list[tuple[float, Path]] = []
    try:
        paths = directory.glob("*.json")
        for path in paths:
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if (
                is_valid_task_id(path.stem)
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
            ):
                candidates.append((metadata.st_mtime, path))
    except OSError:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


def _read_open_regular_fd(fd: int, max_bytes: int) -> bytes | None:
    """Read a bounded already-verified regular-file descriptor."""
    try:
        size = os.fstat(fd).st_size
        if size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return data if len(data) <= max_bytes else None
    except OSError:
        return None


def _read_regular_file(path: Path, max_bytes: int) -> bytes | None:
    """Read at most ``max_bytes`` from one no-follow regular file."""
    try:
        fd = _open_regular_nofollow(path)
    except OSError:
        return None
    try:
        return _read_open_regular_fd(fd, max_bytes)
    finally:
        os.close(fd)


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
    temporary_name = fd.name
    try:
        json.dump(payload, fd, indent=2, sort_keys=True)
        fd.write("\n")
        fd.flush()
        os.fsync(fd.fileno())
        fd.close()
        os.replace(temporary_name, target)
    except BaseException:
        fd.close()
        with _suppress_oserror():
            Path(temporary_name).unlink()
        raise
    with _suppress_oserror():
        os.chmod(target, 0o600)


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


def _unlink_private_regular(path: Path) -> bool:
    """Unlink one single-link regular artifact without following links."""
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _sweep_orphan_logs(directory: Path, anchored_stems: set[str]) -> None:
    """Remove private task logs that have no retained status anchor."""
    try:
        candidates = list(directory.glob("*.log"))
    except OSError:
        return
    for candidate in candidates:
        if not is_valid_task_id(candidate.stem) or candidate.stem in anchored_stems:
            continue
        _unlink_private_regular(candidate)


def _prune_old_tasks(
    directory: Path,
    keep: int = _TASK_RETENTION,
    *,
    preserve_stem: str | None = None,
) -> int:
    """Drop old task pairs and private orphan logs, returning pair count.

    Creation calls this only after the current status and optional log exist.
    ``preserve_stem`` makes the just-created task count toward retention even
    when filesystem timestamp resolution causes ties. Pruning is best-effort
    and must never break a live task's status emission.
    """
    removed = 0
    try:
        if not directory.exists():
            return 0
        with _TASK_ARTIFACT_LOCK:
            json_files = _sorted_regular_json_files(directory)
            limit = max(0, int(keep))
            if preserve_stem and limit > 0:
                current = [path for path in json_files if path.stem == preserve_stem]
                others = [path for path in json_files if path.stem != preserve_stem]
                json_files = current[:1] + others
            for stale in json_files[limit:]:
                if not _unlink_private_regular(stale):
                    continue
                removed += 1
                _unlink_private_regular(stale.with_suffix(".log"))

            anchored_stems = {path.stem for path in _sorted_regular_json_files(directory)}
            _sweep_orphan_logs(directory, anchored_stems)
    except OSError, RuntimeError, ValueError:
        # Pruning is best-effort. Never raise from here.
        return removed
    return removed


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
    """Generate a sortable, collision-resistant, filesystem-safe task ID.

    Format: ``{tool_name}-{millis_since_epoch}-{counter}``. Executable paths
    used by generic long tasks are collapsed to a safe stem before the ID is
    assembled; already-safe tool names retain their historical spelling.
    """
    raw_stem = re.split(r"[/\\]", str(tool_name))[-1]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_stem).strip("._-") or "task"
    safe_stem = safe_stem[:64]
    counter = _next_task_counter()
    return f"{safe_stem}-{int(time.time() * 1000)}-{counter}"


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
        if not is_valid_task_id(task_id):
            raise ValueError("task_id must be a safe filename stem")
        self.task_id = task_id
        self.tool = _bounded_text(str(tool), _STATUS_TOOL_MAX_BYTES)
        raw_argv = list(argv)
        self._argv = [
            _bounded_text(str(argument), _STATUS_ARG_MAX_BYTES)
            for argument in raw_argv[:_STATUS_ARG_MAX_ITEMS]
        ]
        if len(raw_argv) > _STATUS_ARG_MAX_ITEMS:
            self._argv.append(f"...[truncated {len(raw_argv) - _STATUS_ARG_MAX_ITEMS} arguments]")
        self._pid = pid
        self._total_units = total_units
        self._enabled = task_status_enabled()

        # Resolve the configured root once. Observability is optional: an
        # invalid home directory, inaccessible override, or resolution cycle
        # disables disk emission rather than preventing the tool from running.
        self._dir = Path()
        if self._enabled:
            try:
                self._dir = status_dir().expanduser().resolve(strict=False)
            except OSError, RuntimeError:
                self._enabled = False
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
        self._log_fp: IO[bytes] | None = None
        self._log_bytes_written = 0
        self._log_truncated = False

        if self._enabled:
            try:
                with _TASK_ARTIFACT_LOCK:
                    self._dir.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
                    with _suppress_oserror():
                        os.chmod(self._dir, stat.S_IRWXU)

                    # Establish the JSON anchor before creating a log. If the
                    # initial status cannot be written, disable all disk output
                    # so no untracked orphan log is left behind.
                    if not self._write_status_now():
                        self._enabled = False
                    else:
                        try:
                            fd = _open_private_log(self._log_path)
                            try:
                                self._log_fp = os.fdopen(fd, "wb", buffering=0)
                                fd = -1
                            finally:
                                if fd >= 0:
                                    os.close(fd)
                        except OSError:
                            # A planted or unavailable log degrades to
                            # status-only observability; never weaken no-follow.
                            self._log_fp = None

                        # Prune after current creation so the retained count
                        # converges to the configured bound, and sweep private
                        # logs whose status anchor no longer exists.
                        _prune_old_tasks(
                            self._dir,
                            _TASK_RETENTION,
                            preserve_stem=self.task_id,
                        )
            except OSError, RuntimeError, ValueError:
                self._enabled = False
                if self._log_fp is not None:
                    with _suppress_oserror():
                        self._log_fp.close()
                    self._log_fp = None

    # --- recording ------------------------------------------------------

    def set_pid(self, pid: int | None) -> None:
        """Attach the spawned process ID and publish it immediately."""
        self._pid = pid
        if not self._enabled:
            return
        with self._lock:
            self._last_write_ts = time.monotonic()
            self._write_status_now()

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
            status_line = _bounded_text(str(line), _STATUS_LINE_MAX_BYTES)
            self._tail.append(status_line)
            self._last_message = status_line
            now = time.monotonic()
            if now - self._last_write_ts >= _STATUS_WRITE_DEBOUNCE_SECONDS:
                self._last_write_ts = now
                self._write_status_now()
            if self._log_fp is not None and not self._log_truncated:
                label = stream if stream in {"stdout", "stderr"} else "output"
                record = f"[{label}] {line}\n".encode("utf-8", errors="replace")
                if len(record) > _TASK_LOG_LINE_MAX_BYTES:
                    marker = _TRUNCATED_TEXT.encode() + b"\n"
                    record = record[: _TASK_LOG_LINE_MAX_BYTES - len(marker)] + marker
                remaining = _TASK_LOG_MAX_BYTES - self._log_bytes_written
                if len(record) <= remaining:
                    try:
                        written = self._log_fp.write(record)
                        self._log_bytes_written += int(written or 0)
                    except OSError:
                        with _suppress_oserror():
                            self._log_fp.close()
                        self._log_fp = None
                else:
                    if len(_LOG_TRUNCATED_RECORD) <= remaining:
                        with _suppress_oserror():
                            written = self._log_fp.write(_LOG_TRUNCATED_RECORD)
                            self._log_bytes_written += int(written or 0)
                    self._log_truncated = True

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
            payload["error"] = _bounded_text(self._error, _STATUS_LINE_MAX_BYTES)
        if self._log_truncated:
            payload["log_truncated"] = True
        return payload

    def _write_status_now(self) -> bool:
        try:
            _atomic_write_json(self._status_path, self._build_payload())
        except Exception:
            # Disk emission is best-effort — never let an unavailable status
            # surface crash or orphan the live tool invocation.
            return False
        return True


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
    for path in _sorted_regular_json_files(directory):
        record = _read_status_file(path)
        if record is not None:
            records.append(record)
    return records


def get_task(task_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """Return one task record by ID, with ``is_alive`` / orphan rewriting.

    Invalid IDs, traversal attempts, symlinks, non-regular files, and missing
    records all fail closed as ``None`` so callers retain the established
    not-found contract without exposing host filesystem details.
    """
    directory = directory or status_dir()
    fd = _open_task_artifact(task_id, ".json", directory)
    if fd is None:
        return None
    try:
        return _read_status_fd(fd)
    finally:
        os.close(fd)


def tail_log(task_id: str, lines: int = 100, directory: Path | None = None) -> list[str]:
    """Return the last ``lines`` lines of the task's raw log.

    Empty list when the log file is missing, the task hasn't emitted
    anything yet, or the directory is unreadable. Lines do NOT include
    the trailing newline so callers don't have to strip them.
    """
    if lines <= 0:
        return []
    directory = directory or status_dir()
    fd = _open_task_artifact(task_id, ".log", directory)
    if fd is None:
        return []
    try:
        requested = min(int(lines), _TASK_TAIL_MAX_LINES)
        size = os.fstat(fd).st_size
        start = max(0, size - _TASK_TAIL_MAX_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        remaining = min(size - start, _TASK_TAIL_MAX_BYTES)
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if start > 0:
            # Drop the first partial line. If the entire byte window is one
            # giant line, return its bounded suffix with an explicit marker.
            boundary = data.find(b"\n")
            if boundary >= 0:
                data = data[boundary + 1 :]
            elif data:
                marker = _TRUNCATED_TEXT.encode()
                suffix_bytes = max(0, _TASK_TAIL_MAX_BYTES - len(marker))
                data = marker[:_TASK_TAIL_MAX_BYTES]
                if suffix_bytes:
                    data += b"".join(chunks)[-suffix_bytes:]
        decoded = data.decode("utf-8", errors="replace")
        return decoded.splitlines()[-requested:]
    except OSError:
        return []
    finally:
        os.close(fd)


def prune_tasks(keep: int = _TASK_RETENTION, directory: Path | None = None) -> int:
    """Remove all but the most-recent ``keep`` task files.

    Returns the number of task IDs removed (one count per pair —
    a JSON+log removal counts once). Useful for the ``gco tasks
    prune`` CLI when an operator wants a manual sweep.
    """
    directory = directory or status_dir()
    try:
        resolved = directory.expanduser().resolve(strict=True)
    except OSError, RuntimeError:
        return 0
    if not resolved.is_dir():
        return 0
    return _prune_old_tasks(resolved, keep)


def _decode_status_record(raw: bytes | None) -> dict[str, Any] | None:
    """Decode and post-process one bounded status payload."""
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    is_alive = _is_pid_alive(pid if isinstance(pid, int) else None)
    record["is_alive"] = is_alive
    if record.get("state") == "running" and not is_alive:
        record["state"] = "orphaned"
    return record


def _read_status_fd(fd: int) -> dict[str, Any] | None:
    """Load a status record from an already-verified descriptor."""
    return _decode_status_record(_read_open_regular_fd(fd, _STATUS_FILE_MAX_BYTES))


def _read_status_file(path: Path) -> dict[str, Any] | None:
    """Load one no-follow status path for directory-listing callers."""
    return _decode_status_record(_read_regular_file(path, _STATUS_FILE_MAX_BYTES))


def task_ids_for(records: Iterable[dict[str, Any]]) -> list[str]:
    """Project a sequence of task records to their IDs (helper for tests)."""
    return [r["task_id"] for r in records if "task_id" in r]
