"""Tests for the ``gco swarm`` CLI group.

CliRunner against a FilesystemBackend pinned to ``tmp_path`` (the
module-level backend cache is replaced directly, the same isolation
trick the mission CLI tests use), with the registry-resolution helper
patched to a canned snapshot so no real tool registration happens and
the disk heartbeat channel redirected via ``GCO_TASK_STATUS_DIR``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import importlib  # noqa: E402

# ``from cli.commands import swarm_cmd`` would resolve to the Click Group
# re-exported by the package __init__, not the module — import the module
# explicitly (mission CLI test precedent).
swarm_cmd_mod = importlib.import_module("cli.commands.swarm_cmd")

from mission import state as mission_state  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

REGISTERED: dict[str, Any] = {"find_docs": object(), "find_examples": object()}
TAGS: dict[str, set[str]] = {"find_docs": {"safe"}, "find_examples": {"safe"}}


@pytest.fixture()
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FilesystemBackend:
    """Isolated backend + heartbeat dir + enabled flag + canned registry."""
    fs = FilesystemBackend(root=tmp_path / "missions")
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", fs)
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("GCO_ENABLE_SWARM", "true")
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
    monkeypatch.setattr(
        swarm_cmd_mod, "_resolve_registered_tools_for_cli", lambda: (REGISTERED, TAGS)
    )
    return fs


def invoke(*args: str) -> Any:
    return CliRunner().invoke(swarm_cmd_mod.swarm_cmd, list(args), catch_exceptions=False)


def stdout_json(result: Any) -> dict[str, Any]:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return dict(json.loads(lines[-1]))


class TestGating:
    def test_exit_2_without_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The group refuses with code 2 and the hint when ungated."""
        monkeypatch.delenv("GCO_ENABLE_SWARM", raising=False)
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        result = CliRunner().invoke(swarm_cmd_mod.swarm_cmd, ["list"])
        assert result.exit_code == 2
        assert "GCO_ENABLE_SWARM" in result.output


class TestRunDryRun:
    def test_run_completes_a_deterministic_swarm(self, backend: FilesystemBackend) -> None:
        """run --dry-run drives plan -> spawn -> fleet -> report end to end."""
        result = invoke(
            "run",
            "--directive",
            "Find documentation about inference endpoints.",
            "--tool-allowlist",
            "find_docs",
            "--no-sampling",
            "--dry-run",
            "--max-iterations",
            "15",
        )
        assert result.exit_code == 0, result.output
        report = stdout_json(result)
        assert report["final_verdict"] == "complete"
        assert [row["slot"] for row in report["swarm_children"]] == ["worker-1"]
        assert report["swarm_children"][0]["settled"] is True
        # The stderr stream carried the scaffold, spawn, and iteration events.
        assert '"event": "swarm.run.started"' in result.output
        assert '"event": "swarm.spawn"' in result.output

    def test_run_save_plan_writes_reviewable_json(
        self, backend: FilesystemBackend, tmp_path: Path
    ) -> None:
        """--save-plan persists the validated plan alongside the run."""
        plan_path = tmp_path / "plan.json"
        result = invoke(
            "run",
            "--directive",
            "Find documentation.",
            "--tool-allowlist",
            "find_docs",
            "--no-sampling",
            "--dry-run",
            "--save-plan",
            str(plan_path),
        )
        assert result.exit_code == 0, result.output
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan[0]["slot"] == "worker-1"
        assert plan[0]["budget"]["max_iterations"] >= 1


class TestLifecycleCommands:
    def _start(self, tmp_path: Path) -> str:
        criteria = [
            {
                "criterion_id": "fleet_completed",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_completed",
                "op": ">=",
                "target": 1,
            }
        ]
        criteria_file = tmp_path / "criteria.json"
        criteria_file.write_text(json.dumps(criteria), encoding="utf-8")
        result = invoke(
            "start",
            "--directive",
            "Supervise the fleet.",
            "--criteria-file",
            str(criteria_file),
            "--no-sampling",
        )
        assert result.exit_code == 0, result.output
        return str(stdout_json(result)["session_id"])

    def test_start_persists_an_orchestrator(
        self, backend: FilesystemBackend, tmp_path: Path
    ) -> None:
        """start writes a pending orchestrator with the supervisor allowlist."""
        session_id = self._start(tmp_path)
        session = backend.load_session(session_id)
        assert session is not None
        assert session["role"] == "orchestrator"
        assert session["tool_allowlist"][0] == "children_status"
        assert session["tool_allowlist"][-2:] == ["mission_spawn", "child_abort"]
        assert session["swarm"]["max_children"] == 3

    def test_status_abort_and_list_round_trip(
        self, backend: FilesystemBackend, tmp_path: Path
    ) -> None:
        """status renders the rollup; abort cascades; list shows the swarm."""
        session_id = self._start(tmp_path)

        status_result = invoke("status", session_id)
        assert status_result.exit_code == 0
        rollup = stdout_json(status_result)
        assert rollup["session_id"] == session_id
        assert rollup["pool"]["remaining"] == 15

        table_result = invoke("status", session_id, "--output", "table")
        assert f"Swarm {session_id}" in table_result.output

        abort_result = invoke("abort", session_id)
        assert stdout_json(abort_result)["status"] == "terminated"
        again = invoke("abort", session_id)
        assert again.exit_code == 1
        assert "session_terminal" in again.output

        list_result = invoke("list")
        swarms = stdout_json(list_result)["swarms"]
        assert [row["session_id"] for row in swarms] == [session_id]
        assert swarms[0]["status"] == "terminated"

    def test_status_rejects_non_orchestrator(self, backend: FilesystemBackend) -> None:
        """A standalone session is not a swarm."""
        backend.save_session(  # type: ignore[arg-type]
            {
                "version": 1,
                "session_id": "mission-standalone",
                "directive_text": "x",
                "criteria": [],
                "budget": {"max_iterations": 1, "max_wall_clock_seconds": 1},
                "tool_allowlist": ["find_docs"],
                "checkpoint_cadence": {"kind": "every_iteration"},
                "stagnation_threshold": 3,
                "use_sampling": False,
                "allow_scripted_strategies": False,
                "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
                "iterations": [],
                "no_progress_counter": 0,
            }
        )
        result = invoke("status", "mission-standalone")
        assert result.exit_code == 1
        assert "not_an_orchestrator" in result.output


class TestScaffoldPlan:
    def test_deterministic_plan_to_stdout(self, backend: FilesystemBackend) -> None:
        """scaffold-plan emits the validated fallback plan without starting."""
        result = invoke(
            "scaffold-plan",
            "--directive",
            "Find documentation.",
            "--tool-allowlist",
            "find_docs",
            "--no-sampling",
        )
        assert result.exit_code == 0, result.output
        payload = stdout_json(result)
        assert payload["sampling_path"] is False
        assert payload["plan"][0]["slot"] == "worker-1"
        # Nothing was persisted.
        assert backend.list_sessions() == []

    def test_plan_written_to_file(self, backend: FilesystemBackend, tmp_path: Path) -> None:
        """--output-file writes the plan and keeps stdout quiet."""
        out = tmp_path / "plan.json"
        result = invoke(
            "scaffold-plan",
            "--directive",
            "Find documentation.",
            "--tool-allowlist",
            "find_docs",
            "--no-sampling",
            "--output-file",
            str(out),
        )
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text(encoding="utf-8"))[0]["slot"] == "worker-1"
