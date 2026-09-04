"""Additional edge and error-path coverage for tools._task_status.

The primary suite in test_task_status.py exercises the happy paths.
This module targets the branches it leaves uncovered: the pid
liveness guards, the prune helper on a missing directory and its
unlink loop, the writer log-open fallback plus the log-less record
and finish paths, set_last_stack, the atomic-write error swallow,
list_tasks on a missing directory and its malformed-file skip,
tail_log OSError handling, prune_tasks on a missing directory and
its missing-log branch, the non-dict payload guard, and the
task_ids_for projection helper.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from tools import _task_status as task_status_module  # noqa: E402
from tools._task_status import (  # noqa: E402 (sys.path insert above)
    TaskStatusWriter,
    _is_pid_alive,
    _prune_old_tasks,
    _read_status_file,
    list_tasks,
    prune_tasks,
    tail_log,
    task_ids_for,
)


@pytest.fixture
def status_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate task status to tmp_path so tests never touch the real home."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)
    return tmp_path


def _raiser(exc: type[BaseException]):
    """Return a function that raises exc, for monkeypatching os.kill."""

    def _inner(*args: object, **kwargs: object) -> None:
        raise exc

    return _inner


class TestIsPidAlive:
    def test_none_pid_is_not_alive(self) -> None:
        assert _is_pid_alive(None) is False

    def test_non_positive_pid_is_not_alive(self) -> None:
        assert _is_pid_alive(0) is False
        assert _is_pid_alive(-3) is False

    def test_live_pid_is_alive(self) -> None:
        assert _is_pid_alive(os.getpid()) is True

    def test_process_lookup_is_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "kill", _raiser(ProcessLookupError))
        assert _is_pid_alive(123456) is False

    def test_permission_error_is_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "kill", _raiser(PermissionError))
        assert _is_pid_alive(123456) is True

    def test_other_oserror_is_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "kill", _raiser(OSError))
        assert _is_pid_alive(123456) is False


class _BoomDir:
    """Stand-in directory whose glob raises, to drive the prune guard."""

    def exists(self) -> bool:
        return True

    def glob(self, pattern: str) -> object:
        raise OSError("glob exploded")


class TestPruneOldTasksHelper:
    def test_missing_directory_is_noop(self, tmp_path: Path) -> None:
        _prune_old_tasks(tmp_path / "missing")

    def test_removes_pairs_beyond_keep(self, status_root: Path) -> None:
        for i in range(4):
            jpath = status_root / f"t{i}.json"
            jpath.write_text(json.dumps({"task_id": f"t{i}"}), encoding="utf-8")
            (status_root / f"t{i}.log").write_text("x\n", encoding="utf-8")
            mtime = 1000 + i
            os.utime(jpath, (mtime, mtime))
        _prune_old_tasks(status_root, keep=1)
        remaining = sorted(p.stem for p in status_root.glob("*.json"))
        assert remaining == ["t3"]
        assert not (status_root / "t0.log").exists()

    def test_swallows_oserror(self) -> None:
        _prune_old_tasks(_BoomDir())


class TestWriterLogFallback:
    def test_log_open_failure_falls_back_to_status_only(self, status_root: Path) -> None:
        # A directory where the log file belongs makes open() raise OSError.
        (status_root / "block.log").mkdir()
        writer = TaskStatusWriter(task_id="block", tool="deploy_all", argv=[], pid=os.getpid())
        try:
            assert writer._log_fp is None
            assert (status_root / "block.json").exists()
            writer.record_line("hello", stream="stdout")
        finally:
            writer.finish(state="succeeded", exit_code=0)
        record = json.loads((status_root / "block.json").read_text())
        assert record["state"] == "succeeded"

    def test_set_last_stack_updates_without_counter(self, status_root: Path) -> None:
        writer = TaskStatusWriter(task_id="sls", tool="t", argv=[], pid=os.getpid())
        writer.set_last_stack("gco-global")
        writer.finish(state="succeeded", exit_code=0)
        record = json.loads((status_root / "sls.json").read_text())
        assert record["last_stack"] == "gco-global"
        assert record["stacks_completed"] == 0

    def test_set_last_stack_disabled_is_noop(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GCO_DISABLE_TASK_STATUS", "1")
        writer = TaskStatusWriter(task_id="sls-off", tool="t", argv=[], pid=os.getpid())
        writer.set_last_stack("ignored")
        assert not (status_root / "sls-off.json").exists()

    def test_write_status_swallows_oserror(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tools._task_status as ts

        monkeypatch.setattr(ts, "_atomic_write_json", _raiser(OSError))
        writer = ts.TaskStatusWriter(task_id="werr", tool="t", argv=[], pid=os.getpid())
        writer.record_line("x", stream="stdout")
        writer.finish(state="failed", exit_code=1)
        assert not (status_root / "werr.json").exists()


class TestListTasksEdges:
    def test_missing_directory_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "ghost"))
        assert list_tasks() == []

    def test_skips_malformed_records(self, status_root: Path) -> None:
        (status_root / "good.json").write_text(
            json.dumps({"task_id": "good", "pid": 1, "state": "succeeded"}), encoding="utf-8"
        )
        (status_root / "bad.json").write_text("not json {{{", encoding="utf-8")
        ids = [r["task_id"] for r in list_tasks()]
        assert ids == ["good"]


class TestTailLogErrors:
    def test_oserror_returns_empty(self, status_root: Path) -> None:
        # A directory in place of the log file makes open() raise OSError.
        (status_root / "dir.log").mkdir()
        assert tail_log("dir") == []


class TestPruneTasksEdges:
    def test_missing_directory_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "nope"))
        assert prune_tasks() == 0

    def test_handles_json_without_paired_log(self, status_root: Path) -> None:
        for i in range(3):
            jpath = status_root / f"n{i}.json"
            jpath.write_text(json.dumps({"task_id": f"n{i}"}), encoding="utf-8")
            mtime = 2000 + i
            os.utime(jpath, (mtime, mtime))
        removed = prune_tasks(keep=1)
        assert removed == 2
        remaining = sorted(p.stem for p in status_root.glob("*.json"))
        assert remaining == ["n2"]


class TestReadStatusFileNonDict:
    def test_non_dict_payload_returns_none(self, status_root: Path) -> None:
        (status_root / "list.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert _read_status_file(status_root / "list.json") is None


class TestTaskIdsFor:
    def test_projects_ids_and_skips_missing(self) -> None:
        records = [{"task_id": "a"}, {"tool": "x"}, {"task_id": "b"}]
        assert task_ids_for(records) == ["a", "b"]


def test_prune_old_tasks_skips_missing_paired_logs(status_root: Path) -> None:
    """_prune_old_tasks must not unlink a log that never existed."""
    for i in range(3):
        jpath = status_root / f"m{i}.json"
        jpath.write_text(json.dumps({"task_id": f"m{i}"}), encoding="utf-8")
        os.utime(jpath, (3000 + i, 3000 + i))
    _prune_old_tasks(status_root, keep=1)
    remaining = sorted(p.stem for p in status_root.glob("*.json"))
    assert remaining == ["m2"]


class TestValidationAndTextBounds:
    @pytest.mark.parametrize(
        "candidate",
        [None, 7, "", ".", "..", "_leading", "-leading", "a" * 129, "a/b", "a\\b", "a\nb"],
    )
    def test_task_id_validation_rejects_unsafe_shapes(self, candidate: object) -> None:
        assert task_status_module.is_valid_task_id(candidate) is False

    def test_task_id_validation_accepts_boundary_length(self) -> None:
        assert task_status_module.is_valid_task_id("a") is True
        assert task_status_module.is_valid_task_id("a" + ".-_9" * 31 + "xyz") is True

    def test_bounded_text_handles_tiny_budget_and_split_utf8(self) -> None:
        assert task_status_module._bounded_text("unchanged", 32) == "unchanged"
        assert task_status_module._bounded_text("abcdef", 3) == "..."
        bounded = task_status_module._bounded_text("é" * 20, 15)
        assert bounded == task_status_module._TRUNCATED_TEXT
        assert len(bounded.encode("utf-8")) <= 15


class TestDescriptorSecurityBranches:
    def test_open_status_directory_rejects_regular_file(self, tmp_path: Path) -> None:
        occupied = tmp_path / "occupied"
        occupied.write_text("not a directory", encoding="utf-8")

        with pytest.raises(OSError, match="not a directory"):
            task_status_module._open_status_directory(occupied)

    def test_open_status_directory_closes_changed_root(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metadata = status_root.stat()
        closed: list[int] = []
        monkeypatch.setattr(task_status_module.os, "open", lambda *_args, **_kwargs: 701)
        monkeypatch.setattr(
            task_status_module.os,
            "fstat",
            lambda _fd: SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino + 1,
            ),
        )
        monkeypatch.setattr(task_status_module.os, "close", closed.append)

        with pytest.raises(OSError, match="changed while opening"):
            task_status_module._open_status_directory(status_root)

        assert closed == [701]

    def test_open_task_artifact_rejects_unknown_suffix_without_opening_root(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unexpected_open(_directory: Path) -> int:
            raise AssertionError("invalid suffix must short-circuit")

        monkeypatch.setattr(task_status_module, "_open_status_directory", unexpected_open)
        assert task_status_module._open_task_artifact("safe", ".txt", status_root) is None

    def test_open_task_artifact_catches_root_resolution_runtime_error(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_open(_directory: Path) -> int:
            raise RuntimeError("resolution cycle")

        monkeypatch.setattr(task_status_module, "_open_status_directory", fail_open)
        assert task_status_module._open_task_artifact("safe", ".json", status_root) is None

    def test_open_task_artifact_closes_both_fds_on_identity_race(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        artifact = status_root / "raced.json"
        artifact.write_text("{}", encoding="utf-8")
        root_fd = os.open(status_root, os.O_RDONLY)
        real_fstat = os.fstat
        real_close = os.close
        closed: list[int] = []

        monkeypatch.setattr(task_status_module, "_open_status_directory", lambda _path: root_fd)

        def changed_fstat(fd: int) -> SimpleNamespace:
            metadata = real_fstat(fd)
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino + 1,
            )

        def tracked_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(task_status_module.os, "fstat", changed_fstat)
        monkeypatch.setattr(task_status_module.os, "close", tracked_close)

        try:
            assert task_status_module._open_task_artifact("raced", ".json", status_root) is None
            assert len(closed) == 2
            assert root_fd in closed
        finally:
            with contextlib.suppress(OSError):
                real_close(root_fd)

    def test_open_regular_nofollow_closes_fd_on_identity_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "raced.log"
        path.write_text("preserve", encoding="utf-8")
        real_fstat = os.fstat
        real_close = os.close
        closed: list[int] = []

        def changed_fstat(fd: int) -> SimpleNamespace:
            metadata = real_fstat(fd)
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino + 1,
            )

        def tracked_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(task_status_module.os, "fstat", changed_fstat)
        monkeypatch.setattr(task_status_module.os, "close", tracked_close)

        with pytest.raises(OSError, match="changed while opening"):
            task_status_module._open_regular_nofollow(path)

        assert len(closed) == 1
        assert path.read_text(encoding="utf-8") == "preserve"

    def test_open_private_log_safely_resets_existing_regular_file(self, tmp_path: Path) -> None:
        path = tmp_path / "existing.log"
        path.write_text("old sensitive contents", encoding="utf-8")
        path.chmod(0o644)

        fd = task_status_module._open_private_log(path)
        try:
            os.write(fd, b"new")
        finally:
            os.close(fd)

        assert path.read_bytes() == b"new"
        if os.name == "posix":
            assert path.stat().st_mode & 0o777 == 0o600

    def test_open_private_log_does_not_truncate_on_identity_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "raced.log"
        path.write_text("must survive", encoding="utf-8")
        real_fstat = os.fstat

        def changed_fstat(fd: int) -> SimpleNamespace:
            metadata = real_fstat(fd)
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino + 1,
            )

        monkeypatch.setattr(task_status_module.os, "fstat", changed_fstat)

        with pytest.raises(OSError, match="changed while opening"):
            task_status_module._open_private_log(path)

        assert path.read_text(encoding="utf-8") == "must survive"


class TestBoundedFileOperations:
    def test_sorted_json_skips_invalid_name_and_lstat_failure(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = status_root / "good.json"
        invalid = status_root / "bad name.json"
        vanished = status_root / "vanished.json"
        for path in (good, invalid, vanished):
            path.write_text("{}", encoding="utf-8")
        real_lstat = Path.lstat

        def selective_lstat(path: Path):
            if path.name == "vanished.json":
                raise OSError("raced away")
            return real_lstat(path)

        monkeypatch.setattr(Path, "lstat", selective_lstat)

        assert task_status_module._sorted_regular_json_files(status_root) == [good]

    def test_read_open_fd_rejects_declared_oversize(self, tmp_path: Path) -> None:
        path = tmp_path / "large.json"
        path.write_bytes(b"12345")
        fd = os.open(path, os.O_RDONLY)
        try:
            assert task_status_module._read_open_regular_fd(fd, 4) is None
        finally:
            os.close(fd)

    def test_read_open_fd_swallows_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "read-error.json"
        path.write_bytes(b"{}")
        fd = os.open(path, os.O_RDONLY)

        def fail_read(_fd: int, _size: int) -> bytes:
            raise OSError("read failed")

        monkeypatch.setattr(task_status_module.os, "read", fail_read)
        try:
            assert task_status_module._read_open_regular_fd(fd, 10) is None
        finally:
            os.close(fd)

    def test_read_open_fd_rejects_defensive_overread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chunks = iter((b"abcd", b""))
        monkeypatch.setattr(
            task_status_module.os,
            "fstat",
            lambda _fd: SimpleNamespace(st_size=1),
        )
        monkeypatch.setattr(task_status_module.os, "read", lambda _fd, _size: next(chunks))

        assert task_status_module._read_open_regular_fd(702, 2) is None

    def test_atomic_write_removes_temporary_after_serialization_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "record.json"

        def fail_dump(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("serialization failed")

        monkeypatch.setattr(task_status_module.json, "dump", fail_dump)

        with pytest.raises(RuntimeError, match="serialization failed"):
            task_status_module._atomic_write_json(target, {"value": object()})

        assert not target.exists()
        assert list(tmp_path.glob(".record.json.*.tmp")) == []

    def test_atomic_write_keeps_result_when_chmod_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "record.json"

        def fail_chmod(*_args: object, **_kwargs: object) -> None:
            raise OSError("chmod unavailable")

        monkeypatch.setattr(task_status_module.os, "chmod", fail_chmod)
        task_status_module._atomic_write_json(target, {"ok": True})

        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

    def test_suppress_oserror_only_swallows_oserror(self) -> None:
        with task_status_module._suppress_oserror():
            raise OSError("expected")

        with (
            pytest.raises(RuntimeError, match="not suppressed"),
            task_status_module._suppress_oserror(),
        ):
            raise RuntimeError("not suppressed")


class TestSecurePruningBranches:
    def test_orphan_sweep_preserves_anchors_invalid_names_and_special_files(
        self, status_root: Path
    ) -> None:
        (status_root / "kept.log").write_text("kept", encoding="utf-8")
        (status_root / "bad name.log").write_text("invalid", encoding="utf-8")
        (status_root / "orphan.log").write_text("remove", encoding="utf-8")
        (status_root / "special.log").mkdir()

        task_status_module._sweep_orphan_logs(status_root, {"kept"})

        assert (status_root / "kept.log").is_file()
        assert (status_root / "bad name.log").is_file()
        assert (status_root / "special.log").is_dir()
        assert not (status_root / "orphan.log").exists()

    def test_prune_preserves_current_record_when_timestamps_tie(self, status_root: Path) -> None:
        current = status_root / "current.json"
        old = status_root / "old.json"
        current.write_text("{}", encoding="utf-8")
        old.write_text("{}", encoding="utf-8")
        os.utime(current, (5000, 5000))
        os.utime(old, (5000, 5000))

        removed = task_status_module._prune_old_tasks(
            status_root,
            keep=1,
            preserve_stem="current",
        )

        assert removed == 1
        assert current.exists()
        assert not old.exists()

    def test_prune_does_not_remove_log_when_status_unlink_is_refused(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        newest = status_root / "newest.json"
        stale = status_root / "stale.json"
        newest.write_text("{}", encoding="utf-8")
        stale.write_text("{}", encoding="utf-8")
        calls: list[Path] = []

        monkeypatch.setattr(
            task_status_module,
            "_sorted_regular_json_files",
            lambda _directory: [newest, stale],
        )

        def refuse_stale(path: Path) -> bool:
            calls.append(path)
            return path != stale

        monkeypatch.setattr(task_status_module, "_unlink_private_regular", refuse_stale)
        monkeypatch.setattr(
            task_status_module,
            "_sweep_orphan_logs",
            lambda _directory, _stems: None,
        )

        assert task_status_module._prune_old_tasks(status_root, keep=1) == 0
        assert calls == [stale]
        assert stale.with_suffix(".log") not in calls

    def test_prune_swallows_value_error_from_metadata_sort(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_sort(_directory: Path) -> list[Path]:
            raise ValueError("bad retention metadata")

        monkeypatch.setattr(task_status_module, "_sorted_regular_json_files", fail_sort)
        assert task_status_module._prune_old_tasks(status_root) == 0

    def test_make_task_id_sanitizes_paths_empty_stems_and_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(task_status_module.time, "time", lambda: 1.234)
        monkeypatch.setattr(task_status_module, "_next_task_counter", lambda: 9)

        assert task_status_module.make_task_id(r"C:\\tools\\gco tool!") == "gco_tool-1234-9"
        assert task_status_module.make_task_id("/opt/bin/!!!") == "task-1234-9"
        long_id = task_status_module.make_task_id("x" * 100)
        assert long_id == f"{'x' * 64}-1234-9"


class TestWriterDegradationBranches:
    def test_invalid_task_id_fails_before_touching_disk(self, status_root: Path) -> None:
        with pytest.raises(ValueError, match="safe filename stem"):
            TaskStatusWriter(task_id="../escape", tool="t", argv=[], pid=None)
        assert list(status_root.iterdir()) == []

    def test_status_root_resolution_failure_disables_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_status_dir() -> Path:
            raise RuntimeError("resolution cycle")

        monkeypatch.setattr(task_status_module, "status_dir", fail_status_dir)
        writer = task_status_module.TaskStatusWriter(
            task_id="resolve-failed",
            tool="t",
            argv=[],
            pid=None,
        )

        assert writer._enabled is False
        writer.set_pid(123)
        writer.record_line("ignored", stream="stdout")
        writer.increment_stacks("gco-global")
        writer.finish(state="failed")

    def test_fdopen_failure_closes_log_descriptor_and_keeps_status(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        borrowed_fd = os.open(status_root / "borrowed.bin", os.O_WRONLY | os.O_CREAT, 0o600)
        monkeypatch.setattr(task_status_module, "_open_private_log", lambda _path: borrowed_fd)

        def fail_fdopen(*_args: object, **_kwargs: object):
            raise OSError("fdopen failed")

        monkeypatch.setattr(task_status_module.os, "fdopen", fail_fdopen)
        try:
            writer = task_status_module.TaskStatusWriter(
                task_id="fdopen-failed",
                tool="t",
                argv=[],
                pid=None,
            )

            assert writer._log_fp is None
            with pytest.raises(OSError):
                os.fstat(borrowed_fd)
            writer.finish(state="succeeded", exit_code=0)
            assert (status_root / "fdopen-failed.json").is_file()
        finally:
            with contextlib.suppress(OSError):
                os.close(borrowed_fd)

    def test_prune_failure_closes_open_log_and_disables_writer(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_prune(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("prune failed")

        monkeypatch.setattr(task_status_module, "_prune_old_tasks", fail_prune)
        writer = task_status_module.TaskStatusWriter(
            task_id="prune-failed",
            tool="t",
            argv=[],
            pid=None,
        )

        assert writer._enabled is False
        assert writer._log_fp is None
        writer.record_line("ignored", stream="stdout")
        writer.finish(state="failed")
        assert (status_root / "prune-failed.log").read_bytes() == b""

    def test_unknown_stream_label_is_written_as_output(self, status_root: Path) -> None:
        writer = task_status_module.TaskStatusWriter(
            task_id="unknown-stream",
            tool="t",
            argv=[],
            pid=None,
        )
        writer.record_line("message", stream="side-channel")
        writer.finish(state="succeeded", exit_code=0)

        assert (status_root / "unknown-stream.log").read_text(encoding="utf-8") == (
            "[output] message\n"
        )

    def test_log_write_error_closes_sink_without_losing_terminal_status(
        self, status_root: Path
    ) -> None:
        class BrokenLog:
            def __init__(self) -> None:
                self.close_called = False

            def write(self, _record: bytes) -> int:
                raise OSError("disk full")

            def close(self) -> None:
                self.close_called = True
                raise OSError("close failed")

        writer = task_status_module.TaskStatusWriter(
            task_id="write-failed",
            tool="t",
            argv=[],
            pid=None,
        )
        assert writer._log_fp is not None
        writer._log_fp.close()
        broken = BrokenLog()
        writer._log_fp = broken  # type: ignore[assignment]

        writer.record_line("still recorded", stream="stdout")
        assert broken.close_called is True
        assert writer._log_fp is None
        writer.finish(state="succeeded", exit_code=0)

        record = json.loads((status_root / "write-failed.json").read_text(encoding="utf-8"))
        assert record["state"] == "succeeded"
        assert record["last_message"] == "still recorded"

    def test_log_limit_writes_marker_when_it_fits(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            task_status_module,
            "_TASK_LOG_MAX_BYTES",
            len(task_status_module._LOG_TRUNCATED_RECORD) + 2,
        )
        writer = task_status_module.TaskStatusWriter(
            task_id="marker-fits",
            tool="t",
            argv=[],
            pid=None,
        )
        writer.record_line("x" * 200, stream="stdout")
        writer.finish(state="succeeded", exit_code=0)

        assert (status_root / "marker-fits.log").read_bytes() == (
            task_status_module._LOG_TRUNCATED_RECORD
        )
        record = json.loads((status_root / "marker-fits.json").read_text(encoding="utf-8"))
        assert record["log_truncated"] is True

    def test_finish_survives_log_close_failure(self, status_root: Path) -> None:
        class CloseFails:
            def __init__(self) -> None:
                self.flush_called = False
                self.close_called = False

            def flush(self) -> None:
                self.flush_called = True

            def close(self) -> None:
                self.close_called = True
                raise OSError("close failed")

        writer = task_status_module.TaskStatusWriter(
            task_id="close-failed",
            tool="t",
            argv=[],
            pid=None,
        )
        assert writer._log_fp is not None
        writer._log_fp.close()
        failing = CloseFails()
        writer._log_fp = failing  # type: ignore[assignment]

        writer.finish(state="succeeded", exit_code=0)

        assert failing.flush_called is True
        assert failing.close_called is True
        assert writer._log_fp is None
        record = json.loads((status_root / "close-failed.json").read_text(encoding="utf-8"))
        assert record["state"] == "succeeded"


class TestTailAndDecodeBranches:
    def test_tail_discards_partial_first_line_from_bounded_window(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(task_status_module, "_TASK_TAIL_MAX_BYTES", 24)
        (status_root / "windowed.log").write_bytes(b"x" * 40 + b"\nkeep-one\nkeep-two\n")

        assert task_status_module.tail_log("windowed", lines=10) == ["keep-one", "keep-two"]

    def test_tail_closes_descriptor_when_metadata_read_fails(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[int] = []
        monkeypatch.setattr(
            task_status_module,
            "_open_task_artifact",
            lambda _task_id, _suffix, _directory: 703,
        )

        def fail_fstat(_fd: int):
            raise OSError("metadata unavailable")

        monkeypatch.setattr(task_status_module.os, "fstat", fail_fstat)
        monkeypatch.setattr(task_status_module.os, "close", closed.append)

        assert task_status_module.tail_log("anything", directory=status_root) == []
        assert closed == [703]

    def test_prune_rejects_resolved_non_directory(self, tmp_path: Path) -> None:
        occupied = tmp_path / "occupied"
        occupied.write_text("file", encoding="utf-8")
        assert task_status_module.prune_tasks(directory=occupied) == 0

    def test_decode_handles_absent_invalid_utf8_and_non_integer_pid(self) -> None:
        assert task_status_module._decode_status_record(None) is None
        assert task_status_module._decode_status_record(b"\xff") is None

        record = task_status_module._decode_status_record(
            json.dumps({"task_id": "string-pid", "pid": "123", "state": "running"}).encode()
        )
        assert record is not None
        assert record["is_alive"] is False
        assert record["state"] == "orphaned"


class TestResidualTaskArtifactBranches:
    def test_open_regular_rejects_directory_before_open(self, tmp_path: Path) -> None:
        """Directory artifacts are rejected by the initial private-file gate."""
        directory = tmp_path / "not-a-file"
        directory.mkdir()
        with pytest.raises(OSError, match="not a private regular file"):
            task_status_module._open_regular_nofollow(directory)

    def test_new_private_log_rejects_invalid_post_open_type_and_closes_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A create/open race cannot turn a task log into a special artifact."""
        path = tmp_path / "raced.log"
        real_close = os.close
        closed: list[int] = []

        def tracked_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(
            task_status_module.os,
            "fstat",
            lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_nlink=1),
        )
        monkeypatch.setattr(task_status_module.os, "close", tracked_close)

        with pytest.raises(OSError, match="not a private regular file"):
            task_status_module._open_private_log(path)

        assert len(closed) == 1

    def test_private_log_fchmod_failure_closes_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Permission hardening failure never leaks the newly opened descriptor."""
        path = tmp_path / "permission.log"
        real_close = os.close
        closed: list[int] = []

        def tracked_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(
            task_status_module.os,
            "fchmod",
            lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
        )
        monkeypatch.setattr(task_status_module.os, "close", tracked_close)

        with pytest.raises(PermissionError, match="denied"):
            task_status_module._open_private_log(path)

        assert len(closed) == 1

    def test_read_regular_file_returns_none_when_open_fails(self, tmp_path: Path) -> None:
        assert task_status_module._read_regular_file(tmp_path / "missing", 100) is None

    def test_tail_handles_truncation_between_stat_and_read(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A log truncated after fstat yields an empty, closed read."""
        (status_root / "shrunk.log").write_text("content\n", encoding="utf-8")
        monkeypatch.setattr(task_status_module, "_TASK_TAIL_MAX_BYTES", 1)
        monkeypatch.setattr(task_status_module.os, "read", lambda _fd, _size: b"")

        assert task_status_module.tail_log("shrunk", directory=status_root) == []

    def test_tail_uses_marker_only_when_window_cannot_fit_suffix(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tiny bounded window remains explicit without exceeding its budget."""
        marker = task_status_module._TRUNCATED_TEXT
        monkeypatch.setattr(task_status_module, "_TASK_TAIL_MAX_BYTES", len(marker.encode()))
        (status_root / "giant.log").write_text("x" * 200, encoding="utf-8")

        assert task_status_module.tail_log("giant", directory=status_root) == [marker]
