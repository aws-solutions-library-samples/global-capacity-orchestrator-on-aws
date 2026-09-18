"""Deterministic Mission/Swarm runtime edge contracts."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission.engine import MissionEngine
from mission.sandbox import (
    _build_script_observation,
    _make_tool_wrapper,
    validate_script_ast,
)
from mission.state import FilesystemBackend
from mission.swarm_runner import _strip_parsed_asts

from tests.test_swarm_runner import (
    child_request,
    load,
    make_orchestrator,
    make_runner,
)


@pytest.fixture(autouse=True)
def _task_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)


@pytest.fixture
def backend(tmp_path: Path) -> FilesystemBackend:
    return FilesystemBackend(root=tmp_path / "missions")


class TestSwarmRunnerBoundaries:
    async def test_iteration_bound_detaches_and_cancels_driver_without_aborting_child(
        self, backend: FilesystemBackend
    ) -> None:
        make_orchestrator(backend)
        runner = make_runner(backend)
        with patch.object(runner, "_schedule_child"):
            spawned = await runner.spawn(child_request("worker-a", iterations=5))
        child_id = spawned["child_session_id"]
        started = asyncio.Event()
        cancelled = asyncio.Event()
        never = asyncio.Event()

        async def drive(_slot: str) -> None:
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        class Engine:
            async def run_iteration(self, _session_id: str) -> dict[str, str]:
                await started.wait()
                return {"verdict": "continue", "verdict_reason": "in_progress"}

        runner._drive_child = drive  # type: ignore[method-assign]
        runner._build_orchestrator_engine = AsyncMock(return_value=Engine())  # type: ignore[method-assign]

        final = await runner.run_to_completion(max_orchestrator_iterations=1)

        assert final["status"] == "pending"
        assert cancelled.is_set()
        child = load(backend, child_id)
        assert child["status"] == "pending"
        entry = final["children"][0]
        assert entry.get("settled") is not True
        assert entry["reserved_iterations"] == 5

    async def test_paused_orchestrator_detaches_without_building_iteration(
        self, backend: FilesystemBackend
    ) -> None:
        session = make_orchestrator(backend)
        session["status"] = "paused"
        backend.save_session(session)  # type: ignore[arg-type]
        runner = make_runner(backend)
        engine = MagicMock()
        runner._build_orchestrator_engine = AsyncMock(return_value=engine)  # type: ignore[method-assign]

        final = await runner.run_to_completion()

        assert final["status"] == "paused"
        engine.run_iteration.assert_not_called()

    async def test_fleet_progress_timeout_returns_without_spinning(
        self, backend: FilesystemBackend
    ) -> None:
        make_orchestrator(backend)
        runner = make_runner(backend)
        with patch.object(runner, "_schedule_child"):
            await runner.spawn(child_request("worker-a"))
        blocker = asyncio.create_task(asyncio.Event().wait())
        runner._tasks["worker-a"] = blocker
        runner._fleet_progress_timeout = 0
        before = runner._progress_ticks
        try:
            await runner._await_fleet_progress(before)
        finally:
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)
        assert runner._progress_ticks == before

    async def test_fleet_progress_returns_for_new_tick_and_runnerless_live_slot(
        self, backend: FilesystemBackend
    ) -> None:
        make_orchestrator(backend)
        runner = make_runner(backend)
        with patch.object(runner, "_schedule_child"):
            await runner.spawn(child_request("worker-a"))
        runner._progress_ticks = 2
        wait_for = AsyncMock(side_effect=AssertionError("runnerless fleet must not wait"))
        with patch("mission.swarm_runner.asyncio.wait_for", new=wait_for):
            await runner._await_fleet_progress(1)
            await runner._await_fleet_progress(2)
        wait_for.assert_not_awaited()

    async def test_child_engine_setup_failure_terminalizes_then_settles_child(
        self, backend: FilesystemBackend
    ) -> None:
        make_orchestrator(backend)
        runner = make_runner(backend)
        with patch.object(runner, "_schedule_child"):
            spawned = await runner.spawn(child_request("worker-a", iterations=4))
        runner._build_child_engine = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("dependency setup failed")
        )

        await runner._drive_child("worker-a")

        child = load(backend, spawned["child_session_id"])
        assert child["status"] == "terminated"
        assert child["final_verdict"] == "terminate"
        assert child["ended_at"]
        entry = runner._registry[0]
        assert entry["settled"] is True
        assert entry["reserved_iterations"] == 0
        assert entry["consumed_iterations"] == 0

    async def test_missing_child_conservatively_consumes_reserved_budget(
        self, backend: FilesystemBackend
    ) -> None:
        make_orchestrator(backend)
        runner = make_runner(backend)
        with patch.object(runner, "_schedule_child"):
            spawned = await runner.spawn(child_request("worker-a", iterations=4))
        Path(backend.root / f"{spawned['child_session_id']}.json").unlink()

        await runner._drive_child("worker-a")

        entry = runner._registry[0]
        assert entry["settled"] is True
        assert entry["consumed_iterations"] == 4
        assert entry["reserved_iterations"] == 0

    def test_strip_cached_asts_preserves_non_mapping_criteria(self) -> None:
        marker = object()
        assert _strip_parsed_asts([{"criterion_id": "x", "_parsed_ast": marker}, "legacy"]) == [
            {"criterion_id": "x"},
            "legacy",
        ]


class TestScriptObservationAssembly:
    def test_observation_merges_metrics_events_errors_and_observations(self) -> None:
        calls = [
            {
                "tool_name": "capacity",
                "status": "ok",
                "result_summary": {
                    "metrics": {"score": 8, "shared": "tool"},
                    "events": [{"event_name": "ready"}, "ignored"],
                },
            },
            {
                "tool_name": "docs",
                "status": "ok",
                "result_summary": "plain result",
            },
            {
                "tool_name": "submit",
                "status": "failed",
                "result_summary": None,
                "error_message": "denied",
            },
        ]
        observation = _build_script_observation(
            script_call_log=calls,
            observe_log=[
                {"key": "shared", "value": "script"},
                {"key": "loss", "value": 0.1},
            ],
            event_log=[{"event_name": "script-event"}],
            phase_started_at="start",
            phase_ended_at="end",
        )

        assert observation["metrics"] == {
            "score": 8,
            "shared": "tool",
            "observations": {"shared": "script", "loss": 0.1},
        }
        assert observation["events"] == [
            {"event_name": "ready"},
            {"event_name": "script-event"},
        ]
        assert observation["errors"] == [
            {
                "tool_name": "submit",
                "status": "failed",
                "error_message": "denied",
            }
        ]
        assert observation["tool_results"][0]["_status"] == "ok"
        assert observation["tool_results"][1]["result"] == "plain result"

    def test_empty_observation_omits_errors_and_observation_bucket(self) -> None:
        observation = _build_script_observation(
            script_call_log=[],
            observe_log=[],
            event_log=[],
            phase_started_at="start",
            phase_ended_at="end",
        )
        assert observation == {
            "tool_results": [],
            "metrics": {},
            "events": [],
            "phase_started_at": "start",
            "phase_ended_at": "end",
        }

    async def test_tool_wrapper_records_success_and_failure_with_bounded_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = iter([1.0, 1.125, 2.0, 2.5])
        monkeypatch.setattr(
            "mission.sandbox.time",
            SimpleNamespace(monotonic=lambda: next(clock)),
        )
        audit = MagicMock()
        monkeypatch.setattr("mission.sandbox._audit.emit_script_call_event", audit)
        records: list[dict[str, Any]] = []

        async def dispatcher(tool: str, args: dict[str, Any], ctx: Any) -> Any:
            args["mutated"] = True
            if tool == "bad":
                raise RuntimeError("x" * 300)
            return {"ok": True}

        good = _make_tool_wrapper("good", "ctx", dispatcher, records, "session", 3)
        bad = _make_tool_wrapper("bad", "ctx", dispatcher, records, "session", 3)
        assert good.__name__ == "good"
        assert await good(value=1) == {"ok": True}
        with pytest.raises(RuntimeError):
            await bad(value=2)
        assert records[0]["args"] == {"value": 1}
        assert records[0]["duration_ms"] == 125
        assert records[1]["status"] == "failed"
        assert len(records[1]["error_message"]) == 200
        assert audit.call_count == 2

    @pytest.mark.parametrize(
        "source",
        [
            "if True and not False:\n    await find_docs()",
            "value = 1 + 2\nvalue += 1",
            "try:\n    raise ValueError('x')\nexcept ValueError:\n    pass",
            "text = f'{1 < 2}'",
        ],
    )
    def test_validator_accepts_supported_control_flow(self, source: str) -> None:
        validate_script_ast(source, ["find_docs"])


class TestMissionEngineEdges:
    @staticmethod
    def _engine(dispatcher: Any | None = None) -> MissionEngine:
        async def default_dispatcher(_name: str, _args: dict[str, Any], _ctx: Any) -> Any:
            return {"ok": True}

        return MissionEngine(
            backend=MagicMock(),
            tool_dispatcher=dispatcher or default_dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def test_execute_tool_calls_isolates_malformed_disallowed_and_failed_entries(
        self,
    ) -> None:
        dispatcher = AsyncMock(side_effect=RuntimeError("z" * 300))
        engine = self._engine(dispatcher)
        session = {"tool_allowlist": ["allowed"]}
        strategy = {
            "tool_calls": [
                "not-a-mapping",
                {"tool_name": "", "args": "bad"},
                {"tool_name": "blocked", "args": {"x": 1}},
                {"tool_name": "allowed", "args": "normalized"},
            ]
        }
        record: dict[str, Any] = {"strategy": {}}

        calls = await engine._execute_tool_calls(session, strategy, None, record)  # type: ignore[arg-type]

        assert [row["status"] for row in calls] == [
            "failed",
            "failed",
            "skipped_not_allowed",
            "failed",
        ]
        assert calls[0]["tool_name"] == "<unknown>"
        assert calls[1]["args"] == {}
        assert calls[2]["args"] == {"x": 1}
        assert calls[3]["args"] == {}
        assert len(calls[3]["error_message"]) == 200
        dispatcher.assert_awaited_once_with("allowed", {}, None)
        assert record["strategy"]["tool_calls"] == calls

    def test_build_observation_lifts_only_well_shaped_metrics_and_events(self) -> None:
        engine = self._engine()
        observation = engine._build_observation(
            [
                {
                    "tool_name": "a",
                    "args": {},
                    "status": "ok",
                    "result_summary": {
                        "metrics": {"score": 7},
                        "events": [{"event_name": "ok"}, "ignored"],
                    },
                    "duration_ms": 1,
                },
                {
                    "tool_name": "b",
                    "args": {},
                    "status": "ok",
                    "result_summary": {"metrics": "bad", "events": "bad"},
                    "duration_ms": 1,
                },
                {
                    "tool_name": "c",
                    "args": {},
                    "status": "skipped_not_allowed",
                    "result_summary": None,
                    "duration_ms": 0,
                },
            ],
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert observation["metrics"] == {"score": 7}
        assert observation["events"] == [{"event_name": "ok"}]
        assert observation["errors"] == [
            {
                "tool_name": "c",
                "status": "skipped_not_allowed",
                "error_message": None,
            }
        ]

    @pytest.mark.parametrize(
        ("criterion", "observation", "session", "status", "count"),
        [
            (
                {"tool_name": "find_docs"},
                {},
                None,
                "inconclusive",
                None,
            ),
            (
                {"tool_name": "find_docs", "min_count": 2},
                {"tool_results": [{"tool_name": "find_docs", "_status": "ok"}, "bad"]},
                {
                    "iterations": [
                        {
                            "observation": {
                                "tool_results": [
                                    {"tool_name": "find_docs", "_status": "ok"},
                                    {"tool_name": "other", "_status": "ok"},
                                ]
                            }
                        },
                        {"observation": {"tool_results": "bad"}},
                    ]
                },
                "met",
                2,
            ),
            (
                {"tool_name": "find_docs", "min_count": 3},
                {"tool_results": [{"tool_name": "find_docs", "_status": "failed"}]},
                {"iterations": []},
                "unmet",
                0,
            ),
        ],
    )
    def test_tool_success_evaluation_is_cumulative_and_shape_safe(
        self,
        criterion: dict[str, Any],
        observation: dict[str, Any],
        session: dict[str, Any] | None,
        status: str,
        count: int | None,
    ) -> None:
        result, evidence = self._engine()._evaluate_tool_call_succeeded(  # type: ignore[arg-type]
            criterion, observation, session
        )
        assert result == status
        if count is None:
            assert evidence == "tool_results_field_missing"
        else:
            assert evidence["successful_call_count"] == count
