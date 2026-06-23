"""Coverage for the gco tasks CLI command group and its helpers.

Drives cli.commands.tasks_cmd directly with click.testing.CliRunner
plus unit calls into the pure helpers. Covers _status_dir override
and default, the _is_pid_alive liveness ladder, _read_status orphan
rewriting and malformed handling, _list_records skipping, the
_format_state TTY palette, _format_elapsed bucketing, and the list,
show, tail (including follow and error paths), and prune commands.
A tmp_path status dir keeps every test isolated and xdist-safe.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands.tasks_cmd import (
    _format_elapsed,
    _format_state,
    _is_pid_alive,
    _list_records,
    _read_status,
    _status_dir,
    tasks,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def status_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a tmp status dir via the env override."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    return tmp_path


class _FakeStdout:
    """Minimal stdout stand-in so _format_state can probe isatty()."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass


def _raiser(exc: type[BaseException]):
    def _inner(*args: object, **kwargs: object) -> None:
        raise exc

    return _inner


def _write_record(directory: Path, task_id: str, **fields: object) -> Path:
    record = {"task_id": task_id, "tool": "deploy_all", "state": "succeeded", "pid": 1}
    record.update(fields)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestStatusDir:
    def test_override_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
        assert _status_dir() == tmp_path

    def test_default_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GCO_TASK_STATUS_DIR", raising=False)
        assert _status_dir() == Path.home() / ".gco" / "tasks"


class TestIsPidAlive:
    def test_none_pid(self) -> None:
        assert _is_pid_alive(None) is False

    def test_non_positive_pid(self) -> None:
        assert _is_pid_alive(0) is False
        assert _is_pid_alive(-9) is False

    def test_live_pid(self) -> None:
        assert _is_pid_alive(os.getpid()) is True

    def test_process_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "kill", _raiser(ProcessLookupError))
        assert _is_pid_alive(424242) is False

    def test_permission_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "kill", _raiser(PermissionError))
        assert _is_pid_alive(424242) is True

    def test_other_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "kill", _raiser(OSError))
        assert _is_pid_alive(424242) is False


class TestReadStatus:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert _read_status(tmp_path / "nope.json") is None

    def test_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json {{{", encoding="utf-8")
        assert _read_status(path) is None

    def test_non_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert _read_status(path) is None

    def test_running_dead_pid_becomes_orphaned(self, tmp_path: Path) -> None:
        path = tmp_path / "ghost.json"
        path.write_text(
            json.dumps({"task_id": "ghost", "state": "running", "pid": 999999999}),
            encoding="utf-8",
        )
        record = _read_status(path)
        assert record is not None
        assert record["state"] == "orphaned"
        assert record["is_alive"] is False

    def test_running_live_pid_stays_running(self, tmp_path: Path) -> None:
        path = tmp_path / "live.json"
        path.write_text(
            json.dumps({"task_id": "live", "state": "running", "pid": os.getpid()}),
            encoding="utf-8",
        )
        record = _read_status(path)
        assert record is not None
        assert record["state"] == "running"
        assert record["is_alive"] is True

    def test_non_int_pid_treated_as_dead(self, tmp_path: Path) -> None:
        path = tmp_path / "weird.json"
        path.write_text(
            json.dumps({"task_id": "w", "state": "succeeded", "pid": "x"}),
            encoding="utf-8",
        )
        record = _read_status(path)
        assert record is not None
        assert record["is_alive"] is False
        assert record["state"] == "succeeded"


class TestListRecords:
    def test_missing_directory(self, tmp_path: Path) -> None:
        assert _list_records(tmp_path / "absent") == []

    def test_lists_valid_skips_malformed(self, tmp_path: Path) -> None:
        _write_record(tmp_path, "ok", state="succeeded", pid=1)
        (tmp_path / "bad.json").write_text("broken", encoding="utf-8")
        ids = [r["task_id"] for r in _list_records(tmp_path)]
        assert ids == ["ok"]


class TestFormatState:
    def test_non_tty_is_plain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout(False))
        assert _format_state("running") == "running"

    def test_tty_colorizes_known(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout(True))
        assert _format_state("running") == "\x1b[36mrunning\x1b[0m"

    def test_tty_unknown_state_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", _FakeStdout(True))
        assert _format_state("mystery") == "mystery"


class TestFormatElapsed:
    def test_none(self) -> None:
        assert _format_elapsed(None) == "-"

    def test_seconds(self) -> None:
        assert _format_elapsed(45) == "45s"

    def test_minutes(self) -> None:
        assert _format_elapsed(125) == "2m05s"

    def test_hours(self) -> None:
        assert _format_elapsed(3725) == "1h02m"


class TestTasksListCommand:
    def test_empty_directory_message(self, runner: CliRunner, status_root: Path) -> None:
        result = runner.invoke(tasks, ["list"])
        assert result.exit_code == 0
        assert "No tasks recorded" in result.output

    def test_json_output(self, runner: CliRunner, status_root: Path) -> None:
        _write_record(status_root, "t-json", state="running", pid=os.getpid())
        result = runner.invoke(tasks, ["list", "--json"])
        assert result.exit_code == 0
        assert "tasks" in result.output
        assert "t-json" in result.output

    def test_table_output(self, runner: CliRunner, status_root: Path) -> None:
        _write_record(
            status_root,
            "t-full",
            state="succeeded",
            pid=1,
            elapsed_seconds=30,
            stacks_completed=2,
            stacks_total=4,
            last_stack="gco-global",
        )
        _write_record(
            status_root,
            "t-min",
            state="failed",
            pid=1,
            elapsed_seconds=None,
            stacks_completed=0,
            last_stack=None,
        )
        result = runner.invoke(tasks, ["list"])
        assert result.exit_code == 0
        assert "TASK ID" in result.output
        assert "t-full" in result.output
        assert "t-min" in result.output

    def test_limit_zero_lists_all(self, runner: CliRunner, status_root: Path) -> None:
        _write_record(status_root, "t-z", pid=1)
        result = runner.invoke(tasks, ["list", "-n", "0"])
        assert result.exit_code == 0
        assert "t-z" in result.output


class TestTasksShowCommand:
    def test_missing_task(self, runner: CliRunner, status_root: Path) -> None:
        result = runner.invoke(tasks, ["show", "ghost"])
        assert result.exit_code == 1
        assert "Task not found" in result.output

    def test_existing_task(self, runner: CliRunner, status_root: Path) -> None:
        _write_record(status_root, "t-show", state="succeeded", pid=1, exit_code=0)
        result = runner.invoke(tasks, ["show", "t-show"])
        assert result.exit_code == 0
        assert "t-show" in result.output


class TestTasksTailCommand:
    def test_missing_log(self, runner: CliRunner, status_root: Path) -> None:
        result = runner.invoke(tasks, ["tail", "ghost"])
        assert result.exit_code == 1
        assert "No log" in result.output

    def test_basic_tail(self, runner: CliRunner, status_root: Path) -> None:
        (status_root / "t-tail.log").write_text("l0\nl1\nl2\nl3\n", encoding="utf-8")
        result = runner.invoke(tasks, ["tail", "t-tail", "-n", "2"])
        assert result.exit_code == 0
        assert "l3" in result.output
        assert "l0" not in result.output

    def test_zero_lines(self, runner: CliRunner, status_root: Path) -> None:
        (status_root / "t-zero.log").write_text("a\nb\n", encoding="utf-8")
        result = runner.invoke(tasks, ["tail", "t-zero", "-n", "0"])
        assert result.exit_code == 0

    def test_read_oserror(self, runner: CliRunner, status_root: Path) -> None:
        # A directory in place of the log file makes open() raise OSError.
        (status_root / "t-dir.log").mkdir()
        result = runner.invoke(tasks, ["tail", "t-dir"])
        assert result.exit_code == 1
        assert "Failed to read log" in result.output

    def test_follow_stops_when_finished(self, runner: CliRunner, status_root: Path) -> None:
        (status_root / "t-fin.log").write_text("done line\n", encoding="utf-8")
        _write_record(status_root, "t-fin", state="succeeded", pid=1)
        result = runner.invoke(tasks, ["tail", "t-fin", "-f"])
        assert result.exit_code == 0
        assert "done line" in result.output

    def test_follow_reads_new_data_then_interrupt(
        self, runner: CliRunner, status_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_path = status_root / "t-foll.log"
        log_path.write_text("first\n", encoding="utf-8")
        _write_record(status_root, "t-foll", state="running", pid=os.getpid())
        calls = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                with open(log_path, "a", encoding="utf-8") as fp:
                    fp.write("second\n")
                return
            raise KeyboardInterrupt

        monkeypatch.setattr(time, "sleep", fake_sleep)
        result = runner.invoke(tasks, ["tail", "t-foll", "-f"])
        assert result.exit_code == 0
        assert "second" in result.output


class TestTasksPruneCommand:
    def test_missing_directory(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "absent"))
        result = runner.invoke(tasks, ["prune"])
        assert result.exit_code == 0
        assert "No task directory" in result.output

    def test_nothing_to_prune(self, runner: CliRunner, status_root: Path) -> None:
        _write_record(status_root, "p-only", pid=1)
        result = runner.invoke(tasks, ["prune", "-k", "5", "-y"])
        assert result.exit_code == 0
        assert "Already at or below" in result.output

    def test_prune_removes_old_with_yes(self, runner: CliRunner, status_root: Path) -> None:
        base = time.time() - 1000
        for i in range(4):
            jpath = _write_record(status_root, f"p-{i}", pid=1)
            mtime = base + i
            os.utime(jpath, (mtime, mtime))
        # Paired logs for the two oldest only so both branches of the
        # log-exists check run during the sweep.
        (status_root / "p-0.log").write_text("x\n", encoding="utf-8")
        (status_root / "p-1.log").write_text("x\n", encoding="utf-8")
        result = runner.invoke(tasks, ["prune", "-k", "1", "-y"])
        assert result.exit_code == 0
        assert "Removed 3" in result.output
        assert (status_root / "p-3.json").exists()
        assert not (status_root / "p-0.json").exists()
        assert not (status_root / "p-0.log").exists()

    def test_prune_abort_without_yes(self, runner: CliRunner, status_root: Path) -> None:
        for i in range(3):
            _write_record(status_root, f"q-{i}", pid=1)
        result = runner.invoke(tasks, ["prune", "-k", "0"], input="n\n")
        assert result.exit_code == 1

    def test_prune_confirm_yes(self, runner: CliRunner, status_root: Path) -> None:
        for i in range(3):
            _write_record(status_root, f"r-{i}", pid=1)
        result = runner.invoke(tasks, ["prune", "-k", "0"], input="y\n")
        assert result.exit_code == 0
        assert "Removed 3" in result.output
