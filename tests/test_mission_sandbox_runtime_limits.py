"""Mission script sandbox runtime limits, run against the real Monty provider.

* The script cap (``GCO_MCP_CODE_MODE_MAX_DURATION_SECS``) is a wall-clock
  limit that includes time spent waiting on tool calls. Monty's own feed-time
  limit counts only time inside the VM, so a script looping over tool calls
  used to run for as long as it liked.
* A tool call still in flight when the cap fires is recorded as failed in the
  partial script-call log and in the audit stream.
* Errors raised inside the script still end the run through Monty's error
  path.
* Each stop carries the verdict reason the session terminates with:
  ``script_time_limit``, ``script_memory_limit`` or ``script_error``, never
  the session-budget reason ``max_wall_clock``.
* The ``mission_script_call_event`` audit rows carry the index of the
  iteration whose script made the call, across every iteration an engine
  runs (one sandbox per iteration, not per engine).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from mission import sandbox as sandbox_module


def _session(iterations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "session_id": "sess-limits",
        "directive_text": "goal",
        "criteria": [],
        "budget": {},
        "iterations": list(iterations or []),
    }


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the ``mission_script_call_event`` rows the tool wrappers emit."""
    rows: list[dict[str, Any]] = []

    def record(
        session_id: str,
        iteration_index: int,
        tool_name: str,
        status: str,
        duration_ms: int,
        error_message: str | None = None,
    ) -> None:
        rows.append(
            {
                "session_id": session_id,
                "iteration_index": iteration_index,
                "tool_name": tool_name,
                "status": status,
                "error_message": error_message,
            }
        )

    monkeypatch.setattr(sandbox_module._audit, "emit_script_call_event", record)
    return rows


class TestWallClockCap:
    async def test_cap_counts_time_spent_waiting_on_tool_calls(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        """300 calls of 20 ms each would take 6 s; a 0.5 s cap stops the script."""
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 0.5)
        calls = 0

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"ok": True}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())
        script = "i = 0\nwhile i < 300:\n    r = await list_jobs()\n    i = i + 1\n"

        started = time.monotonic()
        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run(script, None, dispatcher)
        elapsed = time.monotonic() - started

        assert elapsed < 4.0, f"script ran {elapsed:.2f}s past a 0.5s cap"
        assert exc_info.value.cause == "TimeoutError"
        assert exc_info.value.reason == "script_time_limit"
        assert exc_info.value.detail == (
            "TimeoutError: script exceeded its 0.5-second wall-clock cap"
        )
        partial = exc_info.value.partial_script_call_log
        assert 0 < len(partial) < 300
        assert calls < 300
        assert len(audit_rows) == len(partial)

    async def test_a_call_in_flight_when_the_cap_fires_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 0.3)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {"ok": True}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run("r = await list_jobs(region='us-east-1')\n", None, dispatcher)

        [entry] = exc_info.value.partial_script_call_log
        assert entry["tool_name"] == "list_jobs"
        assert entry["args"] == {"region": "us-east-1"}
        assert entry["status"] == "failed"
        assert entry["error_message"].startswith("CancelledError")
        assert [(row["tool_name"], row["status"]) for row in audit_rows] == [
            ("list_jobs", "failed")
        ]

    async def test_a_script_that_finishes_inside_the_cap_returns_its_observation(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 5.0)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"metrics": {"loss": 0.1}}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        observation, calls = await box.run("r = await list_jobs()\n", None, dispatcher)

        assert observation["metrics"] == {"loss": 0.1}
        assert [call["status"] for call in calls] == ["ok"]

    async def test_a_script_error_still_terminates_through_the_monty_error_path(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run("r = await list_jobs()\nd = {}\nx = d['missing']\n", None, dispatcher)

        assert exc_info.value.cause == "MontyRuntimeError"
        assert exc_info.value.reason == "script_error"
        assert exc_info.value.detail == "KeyError: 'missing'"
        assert [call["status"] for call in exc_info.value.partial_script_call_log] == ["ok"]

    async def test_the_memory_cap_reports_script_memory_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)
        monkeypatch.setattr(sandbox_module, "_MEMORY_LIMIT_BYTES", 5_000_000)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> None:
            return None

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())
        script = "x = []\nwhile True:\n    x = x + ['a' * 100000]\n"

        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run(script, None, dispatcher)

        assert exc_info.value.reason == "script_memory_limit"
        assert exc_info.value.detail.startswith("MemoryError: memory limit exceeded")

    async def test_a_timeout_error_the_cap_did_not_raise_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only an expired cap becomes SandboxTerminated; any other TimeoutError
        out of the provider is a sandbox failure the engine must see."""

        class TimingOutProvider:
            def __init__(self, *, limits: dict[str, Any]) -> None:
                self.limits = limits

            async def run(self, **_kwargs: Any) -> None:
                raise TimeoutError("provider transport timed out")

        class _NeverRaised(Exception):
            pass

        monkeypatch.setattr(
            sandbox_module, "_import_provider", lambda: (TimingOutProvider, _NeverRaised)
        )
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)
        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> None:
            return None

        with pytest.raises(TimeoutError, match="provider transport timed out"):
            await box.run("r = await list_jobs()\n", None, dispatcher)


class _FakeMontyError(Exception):
    """Monty-shaped error whose ``exception()`` returns the script-side error."""

    def __init__(self, inner: object) -> None:
        super().__init__("monty failed")
        self._inner = inner

    def exception(self) -> object:
        if isinstance(self._inner, type) and issubclass(self._inner, Exception):
            raise self._inner("no inner exception")
        return self._inner


class TestTerminationReasons:
    @pytest.mark.parametrize(
        ("error", "reason", "detail"),
        [
            (
                _FakeMontyError(TimeoutError("feed time limit exceeded: 301ms > 300ms")),
                "script_time_limit",
                "TimeoutError: feed time limit exceeded: 301ms > 300ms",
            ),
            (
                # A TimeoutError the script (or a tool) raised is a script error.
                _FakeMontyError(TimeoutError("connect timed out")),
                "script_error",
                "TimeoutError: connect timed out",
            ),
            (
                _FakeMontyError(MemoryError("memory limit exceeded: 6 bytes > 5 bytes")),
                "script_memory_limit",
                "MemoryError: memory limit exceeded: 6 bytes > 5 bytes",
            ),
            (_FakeMontyError(SyntaxError("bad syntax")), "script_error", "SyntaxError: bad syntax"),
            # exception() failing or returning a non-exception falls back to
            # the Monty error itself.
            (_FakeMontyError(RuntimeError), "script_error", "_FakeMontyError: monty failed"),
            (_FakeMontyError("not an exception"), "script_error", "_FakeMontyError: monty failed"),
            (RuntimeError("crashed worker"), "script_error", "RuntimeError: crashed worker"),
        ],
    )
    def test_monty_errors_map_to_the_reason_that_stopped_the_script(
        self, error: Exception, reason: str, detail: str
    ) -> None:
        assert sandbox_module._monty_termination(error) == (reason, detail)

    def test_detail_is_bounded(self) -> None:
        error = _FakeMontyError(ValueError("x" * 500))

        reason, detail = sandbox_module._monty_termination(error)

        assert reason == "script_error"
        assert len(detail) == 200

    def test_sandbox_terminated_defaults_keep_older_callers_working(self) -> None:
        stopped = sandbox_module.SandboxTerminated("MontyRuntimeError")

        assert stopped.reason == "script_error"
        assert stopped.detail == "MontyRuntimeError"


class TestEngineReportsTheRealReason:
    async def test_a_script_error_terminates_the_session_with_script_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain KeyError used to end the session as ``max_wall_clock``."""
        from mission import SCHEMA_VERSION
        from mission.engine import MissionEngine
        from mission.sandbox import make_default_sandbox_runner
        from mission.state import FilesystemBackend

        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)
        backend = FilesystemBackend(root=tmp_path)
        session: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "session_id": "sess-script-error",
            "directive_text": "Script-error fixture session.",
            "criteria": [
                {
                    "criterion_id": "c1",
                    "kind": "metric_threshold",
                    "required": True,
                    "metric": "loss",
                    "op": "<",
                    "target": -1.0,
                }
            ],
            "budget": {"max_iterations": 10, "max_wall_clock_seconds": 600},
            "tool_allowlist": ["find_examples"],
            "checkpoint_cadence": {"kind": "every_iteration"},
            "stagnation_threshold": 10,
            "use_sampling": False,
            "allow_scripted_strategies": True,
            "status": "pending",
            "created_at": "2025-01-01T00:00:00Z",
            "iterations": [],
            "no_progress_counter": 0,
        }
        backend.save_session(session)  # type: ignore[arg-type]

        async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True}

        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=dispatcher,
            sampling_callable=None,
            sandbox_runner=make_default_sandbox_runner(["find_examples"]),
        )
        monkeypatch.setattr(
            MissionEngine,
            "_deterministic_strategy",
            lambda self, sess: {
                "script": "r = await find_examples()\nd = {}\nx = d['missing']\n",
                "rationale": "fixture: script raises KeyError",
            },
        )

        record = await engine.run_iteration("sess-script-error")

        assert record["verdict"] == "terminate"
        assert record["verdict_reason"] == "script_error"
        assert record["sandbox_terminated_detail"] == "KeyError: 'missing'"
        assert [call["status"] for call in record["script_call_log"]] == ["ok"]
        persisted = backend.load_session("sess-script-error")
        assert persisted is not None
        assert persisted["status"] == "terminated"


def test_final_report_followups_name_the_script_limit_that_fired() -> None:
    from mission import final_report

    session: Any = {}
    time_limit = final_report._build_followups_template(session, "terminate", "script_time_limit")
    memory_limit = final_report._build_followups_template(
        session, "terminate", "script_memory_limit"
    )
    script_error = final_report._build_followups_template(session, "terminate", "script_error")

    assert "GCO_MCP_CODE_MODE_MAX_DURATION_SECS" in time_limit[0]
    assert "GCO_MCP_CODE_MODE_MAX_MEMORY" in memory_limit[0]
    assert "sandbox_terminated_detail" in script_error[0]
    assert all("max_wall_clock_seconds" not in line for line in time_limit)


class TestScriptCallAuditRowsCarryTheRunningIteration:
    async def test_the_default_runner_stamps_each_run_with_its_iteration(
        self, audit_rows: list[dict[str, Any]]
    ) -> None:
        from mission.sandbox import make_default_sandbox_runner

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True}

        runner = make_default_sandbox_runner(["list_jobs"])
        done = {"iteration_index": 0, "verdict": "continue", "verdict_reason": "in_progress"}
        for completed in range(3):
            session = _session([dict(done, iteration_index=i) for i in range(completed)])
            await runner("r = await list_jobs()\n", None, dispatcher, session)

        assert [row["iteration_index"] for row in audit_rows] == [0, 1, 2]

    async def test_engine_iterations_emit_rows_with_their_own_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        """Through the production runner wiring, three iterations of one engine
        used to emit every script-call row with iteration_index 0."""
        from mission import SCHEMA_VERSION
        from mission._engine_factory import _build_sandbox_runner
        from mission.engine import MissionEngine
        from mission.state import FilesystemBackend

        backend = FilesystemBackend(root=tmp_path)
        session: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "session_id": "sess-audit-index",
            "directive_text": "Audit-index fixture session.",
            "criteria": [
                {
                    "criterion_id": "c1",
                    "kind": "metric_threshold",
                    "required": True,
                    "metric": "loss",
                    "op": "<",
                    "target": -1.0,
                }
            ],
            "budget": {"max_iterations": 10, "max_wall_clock_seconds": 600},
            "tool_allowlist": ["find_examples"],
            "checkpoint_cadence": {"kind": "every_iteration"},
            "stagnation_threshold": 10,
            "use_sampling": False,
            "allow_scripted_strategies": True,
            "status": "pending",
            "created_at": "2025-01-01T00:00:00Z",
            "iterations": [],
            "no_progress_counter": 0,
        }
        backend.save_session(session)  # type: ignore[arg-type]

        async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True}

        runner = _build_sandbox_runner(session)
        assert runner is not None
        engine = MissionEngine(
            backend=backend,
            tool_dispatcher=dispatcher,
            sampling_callable=None,
            sandbox_runner=runner,
        )
        monkeypatch.setattr(
            MissionEngine,
            "_deterministic_strategy",
            lambda self, sess: {"script": "r = await find_examples()\n", "rationale": "fixture"},
        )

        records = [await engine.run_iteration("sess-audit-index") for _ in range(3)]

        assert [record["iteration_index"] for record in records] == [0, 1, 2]
        assert [row["iteration_index"] for row in audit_rows] == [0, 1, 2]
        assert {row["session_id"] for row in audit_rows} == {"sess-audit-index"}
