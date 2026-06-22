"""
Tests for the read-only MCP observability tools (``task_status`` and
``task_tail``) and the matching ``gco tasks`` CLI surface.

These tools never spawn subprocesses or touch AWS — they only read the
disk records written by ``gco_mcp/tools/_task_status.py`` during a long-
running tool invocation. The tests stage records directly so they
don't depend on a live ``deploy_all`` subprocess.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))


@pytest.fixture
def status_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the task status directory to tmp_path."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)
    # Reload the helper to pick up the env override fresh.
    import tools._task_status as ts_mod

    importlib.reload(ts_mod)
    return tmp_path


def _stage_record(
    directory: Path,
    task_id: str,
    *,
    state: str = "succeeded",
    pid: int | None = None,
    stacks_completed: int = 0,
    last_stack: str | None = None,
    tail: list[str] | None = None,
    log_lines: list[str] | None = None,
) -> None:
    record = {
        "task_id": task_id,
        "tool": "deploy_all",
        "argv": ["gco", "stacks", "deploy-all", "-y"],
        "pid": pid if pid is not None else os.getpid(),
        "started_at": "2026-05-19T18:00:00Z",
        "updated_at": "2026-05-19T18:30:00Z",
        "elapsed_seconds": 1800,
        "state": state,
        "stacks_completed": stacks_completed,
        "last_stack": last_stack,
        "last_message": (tail or [""])[-1],
        "tail": tail or [],
        "log_path": str(directory / f"{task_id}.log"),
    }
    if state == "succeeded":
        record["exit_code"] = 0
    (directory / f"{task_id}.json").write_text(json.dumps(record))
    if log_lines is not None:
        (directory / f"{task_id}.log").write_text("\n".join(log_lines) + "\n")


class TestTaskStatusTool:
    def test_list_returns_all_tasks_newest_first(self, status_root: Path) -> None:
        _stage_record(status_root, "old", stacks_completed=2)
        os.utime(status_root / "old.json", (time.time() - 3600, time.time() - 3600))
        _stage_record(status_root, "new", stacks_completed=4)

        from tools.tasks import task_status

        payload = json.loads(task_status())
        ids = [t["task_id"] for t in payload["tasks"]]
        assert ids == ["new", "old"]

    def test_list_respects_limit(self, status_root: Path) -> None:
        for i in range(5):
            _stage_record(status_root, f"task-{i}")
            mtime = time.time() - (5 - i) * 60
            os.utime(status_root / f"task-{i}.json", (mtime, mtime))

        from tools.tasks import task_status

        payload = json.loads(task_status(limit=2))
        assert len(payload["tasks"]) == 2

    def test_get_specific_task(self, status_root: Path) -> None:
        _stage_record(status_root, "specific", stacks_completed=3, last_stack="gco-us-east-1")

        from tools.tasks import task_status

        record = json.loads(task_status(task_id="specific"))
        assert record["task_id"] == "specific"
        assert record["stacks_completed"] == 3
        assert record["last_stack"] == "gco-us-east-1"

    def test_get_missing_task_returns_error_payload(self, status_root: Path) -> None:
        from tools.tasks import task_status

        record = json.loads(task_status(task_id="ghost"))
        assert record["error"] == "task_not_found"
        assert record["task_id"] == "ghost"

    def test_orphan_detection_surfaces_through_tool(self, status_root: Path) -> None:
        # Stage as "running" but with a definitely-dead PID.
        _stage_record(status_root, "ghost", state="running", pid=999_999_999)

        from tools.tasks import task_status

        record = json.loads(task_status(task_id="ghost"))
        assert record["state"] == "orphaned"
        assert record["is_alive"] is False


class TestTaskTailTool:
    def test_tail_returns_log_lines(self, status_root: Path) -> None:
        _stage_record(
            status_root,
            "with-log",
            log_lines=[f"[stdout] line {i}" for i in range(20)],
        )

        from tools.tasks import task_tail

        payload = json.loads(task_tail("with-log", lines=5))
        assert payload["task_id"] == "with-log"
        assert payload["lines"] == [f"[stdout] line {i}" for i in range(15, 20)]

    def test_tail_empty_for_missing_task(self, status_root: Path) -> None:
        from tools.tasks import task_tail

        payload = json.loads(task_tail("nope"))
        assert payload["task_id"] == "nope"
        assert payload["lines"] == []


class TestGcoTasksCli:
    """Smoke-test the ``gco tasks`` CLI surface end to end."""

    def test_list_empty_directory(self, status_root: Path) -> None:
        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["list"])
        assert result.exit_code == 0
        assert "No tasks recorded" in result.output

    def test_list_shows_recent_tasks(self, status_root: Path) -> None:
        _stage_record(status_root, "demo", stacks_completed=2, last_stack="gco-global")

        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["list"])
        assert result.exit_code == 0
        assert "demo" in result.output
        assert "gco-global" in result.output

    def test_list_json_mode(self, status_root: Path) -> None:
        _stage_record(status_root, "demo", stacks_completed=1)

        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["tasks"][0]["task_id"] == "demo"

    def test_show_returns_full_record(self, status_root: Path) -> None:
        _stage_record(status_root, "demo", stacks_completed=2)

        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["show", "demo"])
        assert result.exit_code == 0
        record = json.loads(result.output)
        assert record["task_id"] == "demo"
        assert record["stacks_completed"] == 2

    def test_show_missing_exits_nonzero(self, status_root: Path) -> None:
        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["show", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_tail_shows_last_lines(self, status_root: Path) -> None:
        _stage_record(
            status_root,
            "demo",
            log_lines=[f"[stdout] line {i}" for i in range(30)],
        )

        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["tail", "demo", "-n", "5"])
        assert result.exit_code == 0
        # Last five lines should appear in order.
        for i in range(25, 30):
            assert f"line {i}" in result.output

    def test_tail_missing_exits_nonzero(self, status_root: Path) -> None:
        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        result = runner.invoke(tasks, ["tail", "ghost"])
        assert result.exit_code == 1
        assert "ghost" in result.output

    def test_prune_command(self, status_root: Path) -> None:
        for i in range(5):
            _stage_record(status_root, f"t-{i}")
            mtime = time.time() - (5 - i) * 60
            os.utime(status_root / f"t-{i}.json", (mtime, mtime))

        from cli.commands.tasks_cmd import tasks

        runner = CliRunner()
        # ``--yes`` skips the confirmation prompt the on-disk command
        # uses to guard against accidental bulk deletes.
        result = runner.invoke(tasks, ["prune", "--keep", "2", "--yes"])
        assert result.exit_code == 0
        assert "Removed 3" in result.output
        remaining = sorted(p.stem for p in status_root.glob("*.json"))
        assert remaining == ["t-3", "t-4"]
