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

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

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
