"""CLI tests for the ``gco mission`` subcommand group.

Drives the Click subcommands through ``CliRunner`` against a
``FilesystemBackend`` rooted at ``tmp_path``: every test points the
shared backend cache (the module-level ``_BACKEND_INSTANCE`` in
``mission.state``) at the per-test temp directory so sessions written
by one test never leak into another. The ``GCO_ENABLE_MISSION``
feature flag is set per-test through ``monkeypatch.setenv`` so the
``mission`` group's gate (raises ``SystemExit(2)`` when the flag is
unset) does not block the subcommand under test.

Five cases:

* ``test_mission_start_creates_session`` — ``start`` with explicit
  budget and a one-criterion JSON file returns exit code 0 and a JSON
  payload carrying a generated ``session_id``.
* ``test_mission_start_run_mode_completes`` — ``start --run`` with
  ``--max-iterations 1`` lets the budget cap fire on the first
  iteration; the engine writes a Final_Report and the CLI prints it
  to stdout.
* ``test_mission_status_on_nonexistent`` — ``status`` against an
  unknown id exits 1 with a structured ``session_not_found`` envelope
  on stderr.
* ``test_mission_list_table_output`` — two ``start`` calls then
  ``list --output table`` produce a header line and one row per
  session.
* ``test_mission_feature_flag_hint`` — with neither
  ``GCO_ENABLE_MISSION`` nor ``GCO_ENABLE_ALL_TOOLS`` set, ``start``
  exits 2 with the hint mentioning the flag name on stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

# The Mission package lives under ``mcp/mission`` and is imported as
# ``mission.*``. Mirror the path-injection pattern used throughout the
# rest of the ``test_mission_*`` files so the imports below resolve
# regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from cli.main import cli  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the Mission backend cache at a per-test temp directory.

    ``mission.state.get_backend()`` memoises the resolved backend in a
    module-level ``_BACKEND_INSTANCE`` so concurrent ``mission_*`` calls
    share state. Without overriding the cache every CLI test would
    write to ``~/.gco/missions/`` on the developer's machine.
    """
    from mission import state as mission_state  # noqa: PLC0415
    from mission.state import FilesystemBackend  # noqa: PLC0415

    backend = FilesystemBackend(root=tmp_path)
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", backend)
    yield tmp_path
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", None)


def _write_criteria(tmp_path: Path) -> Path:
    """Write a one-entry ``metric_threshold`` criteria JSON file.

    The criterion targets ``val_loss < 0.1``. The CLI stub dispatcher
    returns ``{"_status": "ok", "_stub": True, ...}`` and never emits a
    ``val_loss`` field, so this criterion is never naturally met — the
    ``--run`` test relies on ``--max-iterations 1`` to force the budget
    cap rather than on completion.
    """
    path = tmp_path / "criteria.json"
    path.write_text(
        json.dumps(
            [
                {
                    "criterion_id": "loss",
                    "kind": "metric_threshold",
                    "required": True,
                    "metric": "val_loss",
                    "op": "<",
                    "target": 0.1,
                }
            ]
        )
    )
    return path


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set ``GCO_ENABLE_MISSION=true`` and clear the umbrella flag.

    The CLI gate accepts either flag; clearing the umbrella keeps the
    test focused on the per-tool flag's behaviour and avoids cross-test
    interference from a developer machine that has the umbrella set.
    """
    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMissionCli:
    """CLI tests for the ``gco mission`` subcommand group."""

    def test_mission_start_creates_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``start`` returns a JSON envelope with a generated session id."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        # Click 8.2+ separates stderr from stdout by default; the
        # ``CliRunner`` constructor no longer accepts a ``mix_stderr``
        # kwarg. ``result.stderr`` is reachable as a plain attribute.
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Drive the test to a stable state.",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        payload = json.loads(result.stdout)
        assert payload["session_id"].startswith("mission-")
        assert payload["status"] == "pending"

        # The session JSON was actually persisted under the isolated root.
        sessions = list(isolated_backend.glob("mission-*.json"))
        assert len(sessions) == 1
        assert sessions[0].stem == payload["session_id"]

    def test_mission_start_run_mode_completes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``start --run`` with ``--max-iterations 1`` produces a Final_Report.

        The CLI's stub dispatcher does not emit fields that match the
        test criterion, so the engine cannot complete on the merits.
        With ``--max-iterations 1`` the Decide_Phase compares
        ``len(iterations) + 1 >= 1`` on the very first iteration and
        terminates with reason ``max_iterations`` — both ``complete``
        and ``terminate`` verdicts trigger the Final_Report writer, so
        the CLI's stdout carries a valid report JSON either way.
        """
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Drive the test to a stable state.",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "1",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
                "--run",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        # The final stdout chunk is the Final_Report JSON: a dict with
        # at least the directive, terminal verdict, and iteration count.
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["directive_text"] == "Drive the test to a stable state."
        assert report["final_verdict"] == "terminate"
        assert report["final_verdict_reason"] == "max_iterations"
        assert report["iterations_run"] == 1

        # The verdict line was also emitted to stderr (one JSON line per
        # iteration). The presence of the verdict on stderr is the
        # ``--run`` mode contract documented on the CLI.
        stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
        assert any('"verdict": "terminate"' in line for line in stderr_lines)

    def test_mission_status_on_nonexistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``status <unknown-id>`` exits 1 with a structured error envelope."""
        _enable_flag(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "status", "mission-does-not-exist"])

        assert result.exit_code == 1
        # The error envelope goes to stderr via ``_emit_error``; Click
        # 8.2+ separates ``result.stderr`` from ``result.stdout`` by
        # default, so the JSON envelope is reachable without any
        # mix-stderr toggle.
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"
        assert envelope["details"]["session_id"] == "mission-does-not-exist"

    def test_mission_list_table_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``list --output table`` renders one header row and one row per session."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        runner = CliRunner()
        for _ in range(2):
            start = runner.invoke(
                cli,
                [
                    "mission",
                    "start",
                    "--directive",
                    "Test directive for list rendering.",
                    "--criteria-file",
                    str(criteria),
                    "--max-iterations",
                    "5",
                    "--max-wall-clock",
                    "60",
                    "--tool-allowlist",
                    "find_examples",
                ],
            )
            assert start.exit_code == 0, (
                f"start failed: stderr={start.stderr} stdout={start.stdout}"
            )

        result = runner.invoke(cli, ["mission", "list", "--output", "table"])

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        lines = result.stdout.splitlines()
        # Header line announces the four columns the renderer emits.
        header = next(line for line in lines if "SESSION ID" in line)
        assert "STATUS" in header
        assert "ITER" in header
        assert "CREATED" in header

        # Two data rows, each carrying a ``mission-`` prefixed id and
        # the ``pending`` status the start command leaves behind.
        data_rows = [line for line in lines if "mission-" in line]
        assert len(data_rows) == 2
        for row in data_rows:
            assert "pending" in row

    def test_mission_feature_flag_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Without the flag, ``start`` exits 2 with a hint naming the flag."""
        # Both flags must be cleared — the gate accepts either as truthy.
        monkeypatch.delenv("GCO_ENABLE_MISSION", raising=False)
        monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
        criteria = _write_criteria(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Test directive.",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
            ],
        )

        assert result.exit_code == 2
        assert "GCO_ENABLE_MISSION=true" in result.stderr
