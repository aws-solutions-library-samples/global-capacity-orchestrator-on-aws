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


# ---------------------------------------------------------------------------
# Coverage backfill — error envelopes and table-format paths
# ---------------------------------------------------------------------------


class TestMissionCliCoverage:
    """Backfill the error and table-output branches in mission_cmd.py."""

    def test_start_without_criteria_file_or_defaults_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``start`` without ``--criteria-file`` or ``--with-defaults`` errors."""
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "x",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
            ],
        )
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"

    def test_start_with_defaults_uses_placeholder_predicate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``--with-defaults`` synthesises the ``True`` predicate criterion."""
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "x",
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
                "--with-defaults",
                "--output",
                "table",
            ],
        )
        assert result.exit_code == 0, result.stderr
        # Table output prints labelled rows.
        assert "Session ID:" in result.stdout
        assert "pending" in result.stdout

    def test_start_with_invalid_criteria_file_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """An unparseable criteria file surfaces a validation error envelope."""
        _enable_flag(monkeypatch)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "x",
                "--criteria-file",
                str(bad),
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
            ],
        )
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"

    def test_start_with_invalid_stagnation_threshold_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """A non-positive stagnation-threshold is rejected at the CLI."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "x",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
                "--stagnation-threshold",
                "0",
            ],
        )
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"

    def _make_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_backend: Path
    ) -> str:
        """Create a Mission session via ``start`` and return its id."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "x",
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
        assert result.exit_code == 0, result.stderr
        return json.loads(result.stdout)["session_id"]

    def test_status_table_output_shows_summary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``status --output table`` prints labelled summary rows."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "status", sid, "--output", "table"])
        assert result.exit_code == 0
        assert "Session ID:" in result.stdout
        assert sid in result.stdout
        assert "Allowlist:" in result.stdout

    def test_iterate_with_invalid_max_iterations_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``iterate --max-iterations 0`` is rejected."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "iterate", sid, "--max-iterations", "0"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"

    def test_iterate_table_output_renders_rows(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``iterate --output table`` prints one line per iteration."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["mission", "iterate", sid, "--max-iterations", "1", "--output", "table"]
        )
        assert result.exit_code == 0, result.stderr
        # Table output emits one indented line per iteration.
        assert "Iteration" in result.stdout

    def test_iterate_unknown_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``iterate`` against a missing session id surfaces an error envelope."""
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "iterate", "mission-no-such-id"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"

    def test_checkpoint_no_iterations_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``checkpoint`` on a fresh session with no iterations errors."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "checkpoint", sid])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "no_iterations"

    def test_checkpoint_table_output_after_iteration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``checkpoint --output table`` prints the verdict after iterating."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "iterate", sid, "--max-iterations", "1"])
        result = runner.invoke(cli, ["mission", "checkpoint", sid, "--output", "table"])
        assert result.exit_code == 0, result.stderr
        assert "Iteration" in result.stdout

    def test_checkpoint_unknown_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``checkpoint`` on an unknown id surfaces ``session_not_found``."""
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "checkpoint", "mission-missing"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"

    def test_complete_table_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``complete --output table`` prints a one-line summary."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "complete", sid, "--output", "table"])
        assert result.exit_code == 0, result.stderr
        assert sid in result.stdout

    def test_complete_unknown_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """``complete`` on a missing id errors."""
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "complete", "mission-missing"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"

    def test_complete_terminal_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``complete`` on an already-terminal session errors."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "complete", sid])
        result = runner.invoke(cli, ["mission", "complete", sid])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_terminal"

    def test_abort_pause_table_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``abort --pause --output table`` transitions to paused."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "abort", sid, "--pause", "--output", "table"])
        assert result.exit_code == 0
        assert "paused" in result.stdout

    def test_abort_terminate_table_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``abort --output table`` transitions to terminated."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "abort", sid, "--output", "table"])
        assert result.exit_code == 0
        assert "terminated" in result.stdout

    def test_abort_unknown_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "abort", "mission-missing"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"

    def test_abort_terminal_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "complete", sid])
        result = runner.invoke(cli, ["mission", "abort", sid])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_terminal"

    def test_resume_after_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``abort --pause`` then ``resume --output table`` round-trips."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "abort", sid, "--pause"])
        result = runner.invoke(cli, ["mission", "resume", sid, "--output", "table"])
        assert result.exit_code == 0
        assert "running" in result.stdout

    def test_resume_unknown_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "resume", "mission-missing"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"

    def test_resume_non_paused_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """Resume on a non-paused session errors with ``invalid_state``."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "resume", sid])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "invalid_state"

    def test_history_unknown_session_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "history", "mission-missing"])
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "session_not_found"

    def test_history_summary_after_iteration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``history`` summary prints one row per iteration."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "iterate", sid, "--max-iterations", "1"])
        result = runner.invoke(cli, ["mission", "history", sid, "--output", "table"])
        assert result.exit_code == 0, result.stderr
        assert "Iteration" in result.stdout

    def test_history_full_format_returns_iterations(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``history --format full`` carries the full iteration record."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "iterate", sid, "--max-iterations", "1"])
        result = runner.invoke(cli, ["mission", "history", sid, "--format", "full"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert "iterations" in payload
        assert len(payload["iterations"]) == 1

    def test_history_full_table_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``history --format full --output table`` prints rows."""
        sid = self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        runner.invoke(cli, ["mission", "iterate", sid, "--max-iterations", "1"])
        result = runner.invoke(
            cli,
            ["mission", "history", sid, "--format", "full", "--output", "table"],
        )
        assert result.exit_code == 0
        assert "Iteration" in result.stdout

    def test_list_with_status_filter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``list --status pending`` filters by status."""
        self._make_session(monkeypatch, tmp_path, isolated_backend)
        runner = CliRunner()
        result = runner.invoke(cli, ["mission", "list", "--status", "pending"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert "sessions" in payload


# ---------------------------------------------------------------------------
# scaffold-criteria
# ---------------------------------------------------------------------------


class TestMissionScaffoldCriteriaCli:
    """CLI tests for ``gco mission scaffold-criteria``."""

    def test_scaffold_criteria_no_sampling_stdout_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With ``--no-sampling`` the deterministic generator runs; JSON to stdout."""
        _enable_flag(monkeypatch)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Drive validation loss below 0.1.",
                "--allowlist",
                "find_examples",
                "--no-sampling",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        criteria = json.loads(result.stdout)
        assert isinstance(criteria, list)
        assert len(criteria) == 1
        # A loss-keyword directive yields a metric_threshold criterion.
        assert criteria[0]["kind"] == "metric_threshold"
        assert criteria[0]["op"] == "<="

    def test_scaffold_criteria_no_sampling_writes_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """With ``--output-file`` the JSON lands at the path; stdout summarises."""
        _enable_flag(monkeypatch)
        out = tmp_path / "criteria.json"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Drive validation loss below 0.1.",
                "--no-sampling",
                "--output-file",
                str(out),
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert out.exists()
        # The file content parses as JSON and validates through the
        # Mission validators (contract: scaffolded files are immediately
        # usable with ``mission start --criteria-file``).
        loaded = json.loads(out.read_text())
        assert isinstance(loaded, list)

        # Validator runs without raising.
        sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
        from mission import validation  # noqa: PLC0415

        validation.validate_criteria(loaded)

        # The summary envelope on stdout carries the file path and the
        # criteria count.
        envelope = json.loads(result.stdout)
        assert envelope["output_file"] == str(out)
        assert envelope["criteria_count"] == len(loaded)
        assert envelope["sampling_path"] is False

    def test_scaffold_criteria_with_sampling_mocked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A mocked sampling backend's response becomes the scaffolded output."""
        _enable_flag(monkeypatch)
        sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
        from mission import sampling as mission_sampling  # noqa: PLC0415

        canned = (
            "["
            '{"criterion_id": "loss", "kind": "metric_threshold", '
            '"required": true, "metric": "val_loss", "op": "<=", '
            '"target": 0.1}'
            "]"
        )

        class _MockBackend:
            backend_name = "mcp"
            model_id = "test-model"

            async def sample(self, prompt: Any) -> str:
                return canned

        # Force the resolver to claim sampling is on, and the backend
        # selector to return our stub.
        monkeypatch.setattr(
            mission_sampling,
            "resolve_sampling_state",
            lambda _ctx, _explicit: (True, "mcp"),
        )
        monkeypatch.setattr(
            mission_sampling,
            "select_sampling_backend",
            lambda _ctx, model_id=None, prefs=None: _MockBackend(),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Drive validation loss below 0.1.",
                "--use-sampling",
                "--output-file",
                str(tmp_path / "out.json"),
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        envelope = json.loads(result.stdout)
        assert envelope["sampling_path"] is True
        loaded = json.loads((tmp_path / "out.json").read_text())
        assert loaded[0]["criterion_id"] == "loss"

    def test_scaffold_criteria_falls_back_when_sampling_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the sampling backend raises, the deterministic path runs.

        Patches the backend to raise; the CLI emits a one-line warning
        to stderr and falls back to the keyword-template generator.
        """
        _enable_flag(monkeypatch)
        sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
        from mission import sampling as mission_sampling  # noqa: PLC0415

        class _BrokenBackend:
            backend_name = "bedrock"
            model_id = "test-model"

            async def sample(self, prompt: Any) -> str:
                raise RuntimeError("simulated transport failure")

        monkeypatch.setattr(
            mission_sampling,
            "resolve_sampling_state",
            lambda _ctx, _explicit: (True, "bedrock"),
        )
        monkeypatch.setattr(
            mission_sampling,
            "select_sampling_backend",
            lambda _ctx, model_id=None, prefs=None: _BrokenBackend(),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Drive validation loss below 0.1.",
                "--use-sampling",
                "--retries",
                "1",
                "--output-file",
                str(tmp_path / "out.json"),
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        # The fallback warning shows up on stderr.
        assert "falling back" in result.stderr
        envelope = json.loads(result.stdout)
        assert envelope["sampling_path"] is False
        loaded = json.loads((tmp_path / "out.json").read_text())
        # Deterministic path returned a single metric_threshold for "loss".
        assert loaded[0]["kind"] == "metric_threshold"

    def test_scaffold_criteria_table_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--output table`` prints a per-entry summary instead of JSON."""
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Find documentation about inference.",
                "--no-sampling",
                "--output",
                "table",
            ],
        )

        assert result.exit_code == 0
        # Table mode prints the criterion id and kind on stdout.
        assert "kind=predicate" in result.stdout

    def test_scaffold_criteria_zero_max_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Test.",
                "--no-sampling",
                "--max-criteria",
                "0",
            ],
        )
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["field"] == "max-criteria"

    def test_scaffold_criteria_negative_retries_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_flag(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "scaffold-criteria",
                "--directive",
                "Test.",
                "--no-sampling",
                "--retries",
                "-1",
            ],
        )
        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["details"]["field"] == "retries"


# ---------------------------------------------------------------------------
# -1 (uncapped) sentinel CLI coverage
# ---------------------------------------------------------------------------


class TestMissionStartUncappedCli:
    """CLI surfaces the new ``-1`` sentinel for ``--max-iterations`` and ``--max-wall-clock``."""

    def test_uncapped_iterations_via_cli(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,  # noqa: ARG002
    ) -> None:
        """``--max-iterations -1`` is accepted; the persisted budget reflects it."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        runner = CliRunner()
        # Click parses ``-1`` correctly when the option is typed
        # ``int`` and the value is supplied separately.
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Iteration cap disabled.",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "-1",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        payload = json.loads(result.stdout)
        # The session id is enough to confirm the validator accepted
        # the negative-one cap; the persisted JSON carries the
        # exact sentinel.
        assert payload["session_id"].startswith("mission-")

    def test_uncapped_wall_clock_via_cli(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,  # noqa: ARG002
    ) -> None:
        """``--max-wall-clock -1`` is accepted."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Wall-clock cap disabled.",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "-1",
                "--tool-allowlist",
                "find_examples",
            ],
        )

        assert result.exit_code == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        payload = json.loads(result.stdout)
        assert payload["session_id"].startswith("mission-")

    def test_zero_iterations_rejected_at_cli(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,  # noqa: ARG002
    ) -> None:
        """``--max-iterations 0`` is rejected with the new reason code."""
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "Test.",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "0",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
            ],
        )

        assert result.exit_code == 1
        envelope = json.loads(result.stderr)
        assert envelope["code"] == "validation_error"
        assert envelope["details"]["reason"] == "missing_or_not_positive_int_or_minus_one"
        assert envelope["details"]["subfield"] == "max_iterations"


class TestScriptedStrategiesWiring:
    """The CLI honours the per-session ``allow_scripted_strategies`` flag.

    Sessions started with ``--allow-scripted-strategies`` carry
    ``allow_scripted_strategies=true`` on their persisted state.
    Subsequent ``iterate`` and ``start --run`` invocations need to
    wire a real sandbox runner so a scripted Strategy actually
    executes; leaving ``sandbox_runner=None`` would raise
    ``script_rejected`` from the engine's script dispatcher, silently
    dropping the operator's opt-in.

    Two assertions per scenario:

    1. **Scripted-on**: ``--allow-scripted-strategies`` set at start
       → :func:`mission_cmd._maybe_make_sandbox_runner` returns a
       non-``None`` callable (the bound :meth:`MissionSandbox.run`).
    2. **Scripted-off** (default): the helper returns ``None`` so the
       cheap fast path stays cheap and ``mcp.mission.sandbox`` is
       never imported on the default iterate path.
    """

    def test_helper_returns_none_when_flag_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """The default opt-out keeps ``_maybe_make_sandbox_runner`` cheap."""
        _enable_flag(monkeypatch)
        # ``cli.commands.mission_cmd`` resolves to the Click ``Group``
        # (re-exported by ``cli/commands/__init__.py``); reach the
        # underlying module via ``importlib.import_module`` so we hit
        # the real module namespace.
        import importlib

        mission_cmd_module = importlib.import_module("cli.commands.mission_cmd")
        session = {
            "allow_scripted_strategies": False,
            "tool_allowlist": ["any_tool"],
        }
        assert mission_cmd_module._maybe_make_sandbox_runner(session) is None

    def test_helper_returns_runner_when_flag_on(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_backend: Path,
    ) -> None:
        """Opting in returns a real bound ``MissionSandbox.run`` callable."""
        _enable_flag(monkeypatch)
        import importlib

        mission_cmd_module = importlib.import_module("cli.commands.mission_cmd")
        session = {
            "allow_scripted_strategies": True,
            "tool_allowlist": ["any_tool"],
            # The factory snapshots additional fields onto the sandbox
            # namespace (directive, criteria, budget); fill them with
            # placeholders so the constructor accepts the dict.
            "directive_text": "smoke",
            "criteria": [],
            "budget": {"max_iterations": 1, "max_wall_clock_seconds": 60},
            "iterations": [],
            "session_id": "smoke",
        }
        try:
            runner = mission_cmd_module._maybe_make_sandbox_runner(session)
        except (SystemError, ModuleNotFoundError) as exc:
            # MissionSandbox transitively imports fastmcp → pydantic
            # and the Code Mode sandbox provider's pydantic_monty
            # dependency. Skip cleanly when the local env hits either
            # the pydantic / pydantic-core ABI mismatch or a missing
            # transitive package; CI installs the full lock file.
            # The pin is the test's actual contract — the helper not
            # returning None when the flag is on — so a transitive-
            # import error is environmental rather than a regression.
            msg = str(exc).lower()
            if "pydantic" in msg or "pydantic_monty" in msg:
                pytest.skip(f"local sandbox-runtime env not installed: {exc}")
            raise
        assert runner is not None
        # The runner is the bound run method on a MissionSandbox instance.
        assert callable(runner)

    def test_iterate_path_wires_runner_from_persisted_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        isolated_backend: Path,
    ) -> None:
        """``iterate`` reads ``allow_scripted_strategies`` off the persisted session.

        Pin the wiring point so a regression that goes back to the
        ``sandbox_runner=None`` shape (silently dropping scripted
        strategies on the floor) fails here. Spy on
        :func:`mission_cmd._maybe_make_sandbox_runner` and assert it
        was called with the loaded session.
        """
        _enable_flag(monkeypatch)
        criteria = _write_criteria(tmp_path)

        # First, start a session with the opt-in flag.
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "mission",
                "start",
                "--directive",
                "scripted-on",
                "--criteria-file",
                str(criteria),
                "--max-iterations",
                "5",
                "--max-wall-clock",
                "60",
                "--tool-allowlist",
                "find_examples",
                "--allow-scripted-strategies",
            ],
        )
        assert result.exit_code == 0, result.stderr
        sid = json.loads(result.stdout)["session_id"]

        # Spy on the helper so we can pin the wiring without booting
        # the sandbox runtime.
        import importlib

        mission_cmd_module = importlib.import_module("cli.commands.mission_cmd")

        captured: list[Any] = []

        def _spy(session: Any) -> Any:
            captured.append(session)
            # Return None so the engine takes the no-runner path; the
            # test only cares that the helper was invoked with the
            # correct session shape, not that the sandbox actually ran.
            return None

        monkeypatch.setattr(mission_cmd_module, "_maybe_make_sandbox_runner", _spy)

        result = runner.invoke(cli, ["mission", "iterate", sid, "--max-iterations", "1"])
        assert result.exit_code == 0, result.stderr
        # The helper was called exactly once, with the persisted
        # session whose allow_scripted_strategies is True.
        assert len(captured) == 1
        assert captured[0]["allow_scripted_strategies"] is True
