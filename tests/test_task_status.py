"""
Tests for the disk-backed task status writer.

Covers:
- ``TaskStatusWriter`` lifecycle (initial write, line recording,
  stack increments, terminal states).
- Atomic writes (no partial files visible during write).
- Orphan detection (status claims running, PID is dead → state="orphaned").
- Tail-log retrieval with bounded memory.
- Pruning beyond the retention cap.
- Opt-out via GCO_DISABLE_TASK_STATUS.
- Concurrent writers don't clobber each other's files.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from tools import _task_status as task_status_module  # noqa: E402
from tools._task_status import (  # noqa: E402  (sys.path append above)
    TaskStatusWriter,
    get_task,
    list_tasks,
    make_task_id,
    prune_tasks,
    status_dir,
    tail_log,
    task_status_enabled,
)


@pytest.fixture
def status_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate task status to a tmp_path so tests don't touch ~/.gco."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)
    return tmp_path


class TestStatusDirEnv:
    def test_status_dir_honours_override(self, status_root: Path) -> None:
        assert status_dir() == status_root

    def test_status_dir_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GCO_TASK_STATUS_DIR", raising=False)
        assert status_dir() == Path.home() / ".gco" / "tasks"

    def test_disabled_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCO_DISABLE_TASK_STATUS", "1")
        assert task_status_enabled() is False

    def test_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)
        assert task_status_enabled() is True


class TestMakeTaskId:
    def test_make_task_id_format(self) -> None:
        tid = make_task_id("deploy_all")
        assert tid.startswith("deploy_all-")
        suffix = tid.removeprefix("deploy_all-")
        # Format: "{millis}-{counter}". Both halves are integers.
        millis_part, counter_part = suffix.split("-")
        assert millis_part.isdigit()
        assert counter_part.isdigit()
        # Millisecond precision → 13-digit timestamp in 2026.
        assert len(millis_part) >= 13

    def test_make_task_id_uniqueness(self) -> None:
        ids = {make_task_id("t") for _ in range(20)}
        # The monotonic counter guarantees uniqueness even for a tight
        # back-to-back loop in the same millisecond.
        assert len(ids) == 20


class TestWriterLifecycle:
    def test_initial_write_creates_status_and_log_files(self, status_root: Path) -> None:
        w = TaskStatusWriter(
            task_id="t-1",
            tool="deploy_all",
            argv=["gco", "stacks", "deploy-all", "-y"],
            pid=os.getpid(),
            total_units=4,
        )
        try:
            assert (status_root / "t-1.json").exists()
            assert (status_root / "t-1.log").exists()
            record = json.loads((status_root / "t-1.json").read_text())
            assert record["state"] == "running"
            assert record["stacks_completed"] == 0
            assert record["stacks_total"] == 4
            assert record["pid"] == os.getpid()
            assert record["argv"] == ["gco", "stacks", "deploy-all", "-y"]
            assert record["log_path"].endswith("t-1.log")
        finally:
            w.finish(state="cancelled")

    def test_record_line_appends_to_log_and_tail(self, status_root: Path) -> None:
        w = TaskStatusWriter(task_id="t-2", tool="deploy_all", argv=["gco"], pid=os.getpid())
        try:
            for i in range(5):
                w.record_line(f"line {i}", stream="stdout")
            # Force a flush by waiting past the debounce window.
            time.sleep(0.6)
            w.record_line("final line", stream="stderr")
        finally:
            w.finish(state="succeeded", exit_code=0)

        record = json.loads((status_root / "t-2.json").read_text())
        assert record["last_message"] == "final line"
        assert "final line" in record["tail"]
        log_text = (status_root / "t-2.log").read_text()
        assert "[stdout] line 0" in log_text
        assert "[stderr] final line" in log_text

    def test_increment_stacks_bumps_counter_and_last_stack(self, status_root: Path) -> None:
        w = TaskStatusWriter(task_id="t-3", tool="deploy_all", argv=[], pid=os.getpid())
        try:
            w.increment_stacks("gco-global")
            w.increment_stacks("gco-api-gateway")
        finally:
            w.finish(state="succeeded", exit_code=0)

        record = json.loads((status_root / "t-3.json").read_text())
        assert record["stacks_completed"] == 2
        assert record["last_stack"] == "gco-api-gateway"

    def test_finish_records_terminal_state(self, status_root: Path) -> None:
        w = TaskStatusWriter(task_id="t-4", tool="deploy_all", argv=[], pid=os.getpid())
        w.finish(state="failed", exit_code=1, error="exit_code=1")

        record = json.loads((status_root / "t-4.json").read_text())
        assert record["state"] == "failed"
        assert record["exit_code"] == 1
        assert record["error"] == "exit_code=1"

    def test_disabled_writer_no_files(
        self, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GCO_DISABLE_TASK_STATUS", "1")
        w = TaskStatusWriter(task_id="t-5", tool="deploy_all", argv=[], pid=os.getpid())
        w.record_line("nope", stream="stdout")
        w.increment_stacks("gco-global")
        w.finish(state="succeeded")

        assert not (status_root / "t-5.json").exists()
        assert not (status_root / "t-5.log").exists()


class TestAtomicWrite:
    def test_no_partial_file_visible(self, status_root: Path) -> None:
        """Repeated writes never leave a half-flushed JSON file."""
        w = TaskStatusWriter(task_id="t-6", tool="deploy_all", argv=[], pid=os.getpid())
        try:
            for i in range(50):
                w.record_line(f"line {i}", stream="stdout")
                # Read concurrently and verify it always parses.
                if (status_root / "t-6.json").exists():
                    text = (status_root / "t-6.json").read_text()
                    json.loads(text)  # must not raise
        finally:
            w.finish(state="succeeded", exit_code=0)


class TestOrphanDetection:
    def test_running_with_dead_pid_rewritten_to_orphaned(self, status_root: Path) -> None:
        # Use PID 1 in a recorded "running" state but with a fabricated
        # PID that's almost certainly dead (a very large number that
        # exceeds the typical PID range on any OS).
        fake_pid = 999_999_999
        record = {
            "task_id": "ghost",
            "tool": "deploy_all",
            "argv": ["gco"],
            "pid": fake_pid,
            "started_at": "2026-05-19T18:00:00Z",
            "updated_at": "2026-05-19T18:00:30Z",
            "elapsed_seconds": 30,
            "state": "running",
            "stacks_completed": 1,
            "last_stack": "gco-global",
            "last_message": "deploying",
            "tail": ["deploying"],
            "log_path": str(status_root / "ghost.log"),
        }
        (status_root / "ghost.json").write_text(json.dumps(record))

        loaded = get_task("ghost")
        assert loaded is not None
        assert loaded["state"] == "orphaned"
        assert loaded["is_alive"] is False

    def test_running_with_live_pid_stays_running(self, status_root: Path) -> None:
        record = {
            "task_id": "live-1",
            "tool": "deploy_all",
            "argv": ["gco"],
            "pid": os.getpid(),
            "started_at": "2026-05-19T18:00:00Z",
            "updated_at": "2026-05-19T18:00:30Z",
            "elapsed_seconds": 30,
            "state": "running",
            "stacks_completed": 0,
            "last_stack": None,
            "last_message": None,
            "tail": [],
            "log_path": str(status_root / "live-1.log"),
        }
        (status_root / "live-1.json").write_text(json.dumps(record))

        loaded = get_task("live-1")
        assert loaded is not None
        assert loaded["state"] == "running"
        assert loaded["is_alive"] is True

    def test_terminal_state_unchanged_when_pid_dead(self, status_root: Path) -> None:
        """A finished task whose PID was reaped must keep state=succeeded."""
        record = {
            "task_id": "done-1",
            "tool": "deploy_all",
            "argv": ["gco"],
            "pid": 999_999_999,
            "started_at": "2026-05-19T18:00:00Z",
            "updated_at": "2026-05-19T18:30:00Z",
            "elapsed_seconds": 1800,
            "state": "succeeded",
            "stacks_completed": 4,
            "exit_code": 0,
            "last_stack": "gco-monitoring",
            "last_message": "ok",
            "tail": ["ok"],
            "log_path": str(status_root / "done-1.log"),
        }
        (status_root / "done-1.json").write_text(json.dumps(record))

        loaded = get_task("done-1")
        assert loaded is not None
        assert loaded["state"] == "succeeded"
        assert loaded["is_alive"] is False


class TestTailLog:
    def test_tail_returns_last_n_lines(self, status_root: Path) -> None:
        log = status_root / "tail-1.log"
        log.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")

        result = tail_log("tail-1", lines=10)
        assert result == [f"line {i}" for i in range(40, 50)]

    def test_tail_with_zero_lines_returns_empty(self, status_root: Path) -> None:
        log = status_root / "tail-2.log"
        log.write_text("a\nb\nc\n")
        assert tail_log("tail-2", lines=0) == []

    def test_tail_missing_log_returns_empty(self, status_root: Path) -> None:
        assert tail_log("nonexistent") == []

    def test_tail_more_lines_than_file_returns_all(self, status_root: Path) -> None:
        log = status_root / "tail-3.log"
        log.write_text("only one line\n")
        assert tail_log("tail-3", lines=100) == ["only one line"]

    def test_tail_caps_caller_lines_and_bytes(
        self,
        status_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(task_status_module, "_TASK_TAIL_MAX_BYTES", 64)
        monkeypatch.setattr(task_status_module, "_TASK_TAIL_MAX_LINES", 2)
        (status_root / "bounded.log").write_bytes(b"x" * 512)

        result = tail_log("bounded", lines=10_000)

        assert len(result) == 1
        assert result[0].startswith("...[truncated]")
        assert len(result[0].encode("utf-8")) <= 64


class TestTaskArtifactSecurity:
    def test_invalid_task_ids_fail_closed(self, status_root: Path) -> None:
        for task_id in ("../escape", "..", ".", "/absolute", "nested/name", "nested\\name"):
            assert get_task(task_id) is None
            assert tail_log(task_id) == []

    def test_symlink_artifacts_are_never_followed(self, status_root: Path) -> None:
        victim_json = status_root.parent / "victim.json"
        victim_log = status_root.parent / "victim.log"
        victim_json.write_text(json.dumps({"task_id": "victim"}), encoding="utf-8")
        victim_log.write_text("host secret\n", encoding="utf-8")
        (status_root / "escape.json").symlink_to(victim_json)
        (status_root / "escape.log").symlink_to(victim_log)

        assert get_task("escape") is None
        assert tail_log("escape") == []
        assert list_tasks() == []

    def test_hard_link_artifacts_are_rejected(self, status_root: Path) -> None:
        victim_json = status_root.parent / "hard-victim.json"
        victim_log = status_root.parent / "hard-victim.log"
        victim_json.write_text(json.dumps({"task_id": "hard"}), encoding="utf-8")
        victim_log.write_text("host secret\n", encoding="utf-8")
        try:
            os.link(victim_json, status_root / "hard.json")
            os.link(victim_log, status_root / "hard.log")
        except OSError as exc:
            pytest.skip(f"hard links unavailable: {exc}")

        assert get_task("hard") is None
        assert tail_log("hard") == []
        assert list_tasks() == []

    def test_planted_log_symlink_is_not_truncated(self, status_root: Path) -> None:
        victim = status_root.parent / "do-not-truncate.log"
        victim.write_text("preserve me", encoding="utf-8")
        (status_root / "predictable.log").symlink_to(victim)

        writer = TaskStatusWriter(
            task_id="predictable",
            tool="deploy_all",
            argv=[],
            pid=os.getpid(),
        )
        try:
            assert writer._log_fp is None
        finally:
            writer.finish(state="cancelled")

        assert victim.read_text(encoding="utf-8") == "preserve me"

    def test_writer_uses_private_modes_and_bounded_log(
        self,
        status_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if os.name != "posix":
            pytest.skip("POSIX permission contract")
        monkeypatch.setattr(task_status_module, "_TASK_LOG_LINE_MAX_BYTES", 64)
        monkeypatch.setattr(task_status_module, "_TASK_LOG_MAX_BYTES", 128)

        writer = TaskStatusWriter(
            task_id="bounded-writer",
            tool="t" * 2_000,
            argv=["a" * 10_000] * 200,
            pid=os.getpid(),
        )
        writer.record_line("x" * 10_000, stream="stdout")
        writer.record_line("y" * 10_000, stream="stderr")
        writer.record_line("z" * 10_000, stream="stdout")
        writer.finish(state="succeeded", exit_code=0)

        status_path = status_root / "bounded-writer.json"
        log_path = status_root / "bounded-writer.log"
        record = json.loads(status_path.read_text(encoding="utf-8"))
        assert record["log_truncated"] is True
        assert len(record["argv"]) == task_status_module._STATUS_ARG_MAX_ITEMS + 1
        assert len(log_path.read_bytes()) <= task_status_module._TASK_LOG_MAX_BYTES
        assert status_root.stat().st_mode & 0o777 == 0o700
        assert status_path.stat().st_mode & 0o777 == 0o600
        assert log_path.stat().st_mode & 0o777 == 0o600


class TestListAndPrune:
    def test_list_tasks_newest_first(self, status_root: Path) -> None:
        # Older record.
        (status_root / "old.json").write_text(
            json.dumps(
                {
                    "task_id": "old",
                    "tool": "deploy_all",
                    "argv": [],
                    "pid": 1,
                    "state": "succeeded",
                    "stacks_completed": 0,
                    "last_stack": None,
                    "last_message": None,
                    "tail": [],
                    "started_at": "2026-05-19T17:00:00Z",
                    "updated_at": "2026-05-19T17:30:00Z",
                    "elapsed_seconds": 1800,
                    "log_path": str(status_root / "old.log"),
                }
            )
        )
        # Make it actually older.
        old_time = time.time() - 3600
        os.utime(status_root / "old.json", (old_time, old_time))

        # Newer record.
        (status_root / "new.json").write_text(
            json.dumps(
                {
                    "task_id": "new",
                    "tool": "deploy_all",
                    "argv": [],
                    "pid": 1,
                    "state": "succeeded",
                    "stacks_completed": 0,
                    "last_stack": None,
                    "last_message": None,
                    "tail": [],
                    "started_at": "2026-05-19T18:00:00Z",
                    "updated_at": "2026-05-19T18:30:00Z",
                    "elapsed_seconds": 1800,
                    "log_path": str(status_root / "new.log"),
                }
            )
        )

        tasks = list_tasks()
        ids = [t["task_id"] for t in tasks]
        assert ids == ["new", "old"]

    def test_prune_keeps_most_recent(self, status_root: Path) -> None:
        # Make 5 task files, keep 2.
        for i in range(5):
            path = status_root / f"task-{i}.json"
            path.write_text(json.dumps({"task_id": f"task-{i}", "pid": 1}))
            (status_root / f"task-{i}.log").write_text(f"log {i}\n")
            mtime = time.time() - (5 - i) * 60
            os.utime(path, (mtime, mtime))
            os.utime(status_root / f"task-{i}.log", (mtime, mtime))

        removed = prune_tasks(keep=2)
        assert removed == 3
        remaining = sorted(p.stem for p in status_root.glob("*.json"))
        assert remaining == ["task-3", "task-4"]
        # Logs paired with pruned tasks are also gone.
        assert not (status_root / "task-0.log").exists()
        assert (status_root / "task-3.log").exists()

    def test_get_task_returns_none_for_missing(self, status_root: Path) -> None:
        assert get_task("nope") is None

    def test_get_task_returns_none_for_malformed(self, status_root: Path) -> None:
        (status_root / "bad.json").write_text("not json {{{")
        assert get_task("bad") is None


class TestConcurrentWriters:
    def test_two_writers_independent_files(self, status_root: Path) -> None:
        """Two writers running in parallel keep separate state."""
        results: dict[str, int] = {}

        def run(task_id: str, count: int) -> None:
            w = TaskStatusWriter(task_id=task_id, tool="t", argv=[], pid=os.getpid())
            try:
                for i in range(count):
                    w.record_line(f"{task_id} line {i}", stream="stdout")
                    w.increment_stacks(f"stack-{i}")
            finally:
                w.finish(state="succeeded", exit_code=0)
            results[task_id] = count

        t1 = threading.Thread(target=run, args=("worker-a", 10))
        t2 = threading.Thread(target=run, args=("worker-b", 7))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        a = json.loads((status_root / "worker-a.json").read_text())
        b = json.loads((status_root / "worker-b.json").read_text())
        assert a["stacks_completed"] == 10
        assert b["stacks_completed"] == 7
        # Tails reflect each task's own stream, not the other's.
        for line in a["tail"]:
            assert "worker-a" in line
        for line in b["tail"]:
            assert "worker-b" in line


class TestWriterFailureAndAutomaticRetention:
    def test_set_pid_updates_pre_spawn_status(self, status_root: Path) -> None:
        writer = TaskStatusWriter(
            task_id="pre-spawn",
            tool="deploy_all",
            argv=["gco", "stacks", "deploy-all"],
            pid=None,
        )
        try:
            initial = json.loads((status_root / "pre-spawn.json").read_text())
            assert initial["pid"] is None
            writer.set_pid(os.getpid())
            updated = json.loads((status_root / "pre-spawn.json").read_text())
            assert updated["pid"] == os.getpid()
        finally:
            writer.finish(state="cancelled")

    def test_initial_status_failure_disables_log_creation(
        self,
        status_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_write(_target: Path, _payload: dict[str, object]) -> None:
            raise OSError("status unavailable")

        monkeypatch.setattr(task_status_module, "_atomic_write_json", fail_write)
        writer = TaskStatusWriter(
            task_id="no-anchor",
            tool="deploy_all",
            argv=[],
            pid=None,
        )
        writer.finish(state="failed", error="spawn failed")

        assert writer._enabled is False
        assert not (status_root / "no-anchor.json").exists()
        assert not (status_root / "no-anchor.log").exists()

    def test_unusable_status_root_never_breaks_writer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("occupied", encoding="utf-8")
        monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(blocked))

        writer = TaskStatusWriter(
            task_id="disabled-disk",
            tool="deploy_all",
            argv=[],
            pid=None,
        )
        writer.set_pid(os.getpid())
        writer.record_line("still runs", stream="stdout")
        writer.finish(state="succeeded", exit_code=0)

        assert writer._enabled is False
        assert blocked.read_text(encoding="utf-8") == "occupied"

    def test_new_writer_prunes_after_creation_and_sweeps_orphan_logs(
        self,
        status_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(task_status_module, "_TASK_RETENTION", 2)
        for index in range(2):
            status_path = status_root / f"old-{index}.json"
            status_path.write_text(json.dumps({"task_id": f"old-{index}"}), encoding="utf-8")
            (status_root / f"old-{index}.log").write_text("old\n", encoding="utf-8")
            timestamp = time.time() - (20 - index)
            os.utime(status_path, (timestamp, timestamp))
        (status_root / "orphan.log").write_text("unanchored\n", encoding="utf-8")

        writer = TaskStatusWriter(
            task_id="current",
            tool="deploy_all",
            argv=[],
            pid=os.getpid(),
        )
        writer.finish(state="succeeded", exit_code=0)

        retained = {path.stem for path in status_root.glob("*.json")}
        assert retained == {"current", "old-1"}
        assert (status_root / "current.log").exists()
        assert (status_root / "old-1.log").exists()
        assert not (status_root / "old-0.log").exists()
        assert not (status_root / "orphan.log").exists()
