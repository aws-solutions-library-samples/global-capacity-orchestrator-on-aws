"""Hermetic lifecycle coverage for :mod:`mission.swarm_runner`.

The tests exercise supervision outcomes rather than touching lines: orphan
adoption, restart state, dispatcher routing, cancellation, cleanup, and fleet
findings all assert their durable or operator-visible behavior.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "gco_mcp"))

from mission._engine_factory import EngineDependencies  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.swarm_runner import (  # noqa: E402
    SwarmRunner,
    build_children_snapshot,
    build_fleet_rollup,
    list_swarms,
)
from mission.types import SCHEMA_VERSION  # noqa: E402
from mission.validation import MissionValidationError  # noqa: E402

runner_module = sys.modules["mission.swarm_runner"]


@pytest.fixture(autouse=True)
def _task_status_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)


@pytest.fixture
def backend(tmp_path: Path) -> FilesystemBackend:
    return FilesystemBackend(root=tmp_path / "missions")


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        "max_children": 4,
        "child_iteration_pool": 20,
        "max_concurrent_children": 2,
        "allow_overlapping_mutating_tools": False,
        **overrides,
    }


def _parent(
    backend: FilesystemBackend,
    *,
    session_id: str = "mission-orchestrator",
    status: str = "pending",
    children: list[dict[str, Any]] | None = None,
    swarm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Supervise the fleet.",
        "criteria": [
            {
                "criterion_id": "fleet_done",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_completed",
                "op": ">=",
                "target": 1,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 300},
        "tool_allowlist": ["children_status", "mission_spawn", "child_abort"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 10,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
        "role": "orchestrator",
        "swarm": dict(swarm or _config()),
        "children": list(children or []),
    }
    if status == "completed":
        session["final_verdict"] = "complete"
    elif status in {"terminated", "failed"}:
        session["final_verdict"] = "terminate"
    backend.save_session(session)  # type: ignore[arg-type]
    return session


def _entry(
    slot: str = "worker-a",
    *,
    session_id: str = "mission-child-a",
    settled: bool = False,
    restart_policy: str = "never",
    max_respawns: int = 0,
    respawn_count: int = 0,
    reserved_iterations: int = 4,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "slot": slot,
        "session_id": session_id,
        "spawned_at": "2026-01-01T00:00:00+00:00",
        "reserved_iterations": reserved_iterations,
        "restart_policy": restart_policy,
        "max_respawns": max_respawns,
        "respawn_count": respawn_count,
        "consumed_iterations": 0,
    }
    if settled:
        entry["settled"] = True
    return entry


def _child(
    backend: FilesystemBackend,
    *,
    session_id: str = "mission-child-a",
    parent_id: str = "mission-orchestrator",
    status: str = "running",
    iterations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Perform child work.",
        "criteria": [
            {
                "criterion_id": "done",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
        ],
        "budget": {"max_iterations": 4, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["find_docs"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "iterations": list(iterations or []),
        "no_progress_counter": 0,
        "role": "child",
        "parent_session_id": parent_id,
    }
    if status == "completed":
        session["final_verdict"] = "complete"
    elif status in {"terminated", "failed"}:
        session["final_verdict"] = "terminate"
    backend.save_session(session)  # type: ignore[arg-type]
    return session


async def _dispatcher(_name: str, _args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
    return {"ok": True}


async def _deps(_session: Any) -> EngineDependencies:
    return EngineDependencies(
        tool_dispatcher=_dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
    )


def _runner(
    backend: FilesystemBackend,
    *,
    parent_id: str = "mission-orchestrator",
    reviser: Any = None,
    observer: Any = None,
) -> SwarmRunner:
    return SwarmRunner(
        backend=backend,
        orchestrator_id=parent_id,
        deps_builder=_deps,
        registered_tools={"find_docs": object(), "mutating": object()},
        registered_tags={"find_docs": {"safe"}, "mutating": {"low-risk"}},
        revise_directive=reviser,
        on_orchestrator_iteration=observer,
    )


def test_snapshot_marks_restartable_failure_as_respawning() -> None:
    entry = _entry(
        restart_policy="on_failure",
        max_respawns=1,
        respawn_count=0,
    )
    failed = {
        "status": "failed",
        "final_verdict": "terminate",
        "iterations": [{"iteration_index": 1}],
    }

    snapshot = build_children_snapshot(
        _config(),
        [entry],
        lambda _session_id: failed,  # type: ignore[arg-type]
    )

    assert snapshot["children"][0]["status"] == "respawning"
    assert snapshot["children"][0]["final_verdict"] == "terminate"
    assert snapshot["metrics"]["children_running"] == 1
    assert snapshot["metrics"]["children_failed"] == 0


@pytest.mark.parametrize("kind", ["missing", "standalone"])
def test_runner_constructor_rejects_missing_or_non_orchestrator(
    backend: FilesystemBackend, kind: str
) -> None:
    if kind == "standalone":
        backend.save_session(  # type: ignore[arg-type]
            {
                "version": SCHEMA_VERSION,
                "session_id": "mission-orchestrator",
                "status": "pending",
                "role": "standalone",
                "iterations": [],
            }
        )
    with pytest.raises(MissionValidationError) as exc_info:
        _runner(backend)
    assert exc_info.value.code == ("session_not_found" if kind == "missing" else "validation_error")


def test_registry_unknown_slot_and_disappeared_flush(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    runner = _runner(backend)
    with pytest.raises(KeyError, match="unknown"):
        runner._entry_index("unknown")

    runner._registry.append(_entry())
    runner._dirty = True
    save = MagicMock()
    monkeypatch.setattr(backend, "load_session", lambda _session_id: None)
    monkeypatch.setattr(backend, "save_session", save)
    runner._flush_registry()
    save.assert_not_called()
    assert runner._dirty is True


def test_reconcile_adopts_only_matching_orphan(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _parent(backend)
    known = _child(backend, session_id="mission-known")
    wrong = _child(backend, session_id="mission-wrong", parent_id="other-parent")
    orphan = _child(backend, session_id="mission-orphan")
    parent["children"] = [_entry(session_id=known["session_id"])]
    backend.save_session(parent)  # type: ignore[arg-type]
    runner = _runner(backend)

    monkeypatch.setattr(
        backend,
        "list_sessions",
        lambda *_args, **_kwargs: [
            {},
            {"session_id": known["session_id"]},
            {"session_id": wrong["session_id"]},
            {"session_id": orphan["session_id"]},
        ],
    )
    audit = MagicMock()
    monkeypatch.setattr(runner_module.mission_audit, "emit_child_lifecycle_event", audit)

    runner._reconcile_orphans()

    adopted = [row for row in runner._registry if row["session_id"] == orphan["session_id"]]
    assert len(adopted) == 1
    assert adopted[0]["slot"] == "adopted-n-orphan"
    assert adopted[0]["restart_policy"] == "never"
    assert runner._dirty is True
    audit.assert_called_once_with(
        "mission-orchestrator",
        "mission-orphan",
        "adopted-n-orphan",
        "spawned",
        reason="adopted_orphan",
    )


def test_child_writer_cache_and_optional_heartbeat(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    runner = _runner(backend)
    writer = MagicMock()
    writer_factory = MagicMock(return_value=writer)
    monkeypatch.setattr(runner_module, "TaskStatusWriter", writer_factory)

    runner._heartbeat("ignored without an acquired writer")
    assert runner._child_writer("worker-a") is writer
    assert runner._child_writer("worker-a") is writer
    writer_factory.assert_called_once()

    runner._swarm_writer = writer
    runner._heartbeat("fleet progressed")
    writer.record_line.assert_called_once_with("fleet progressed", stream="stdout")


async def test_dispatcher_routes_all_supervisor_names_and_falls_through(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    runner = _runner(backend)
    spawn = AsyncMock(return_value={"spawned": True})
    abort = AsyncMock(return_value={"aborted": True})
    status = MagicMock(return_value={"metrics": {"children_total": 0}})
    inner = AsyncMock(return_value={"inner": True})
    monkeypatch.setattr(runner, "spawn", spawn)
    monkeypatch.setattr(runner, "abort_child", abort)
    monkeypatch.setattr(runner, "children_status", status)
    dispatch = runner.wrap_dispatcher(inner)

    assert await dispatch("mission_spawn", {"slot": "a"}, "ctx") == {"spawned": True}
    assert await dispatch("children_status", {}, "ctx") == {"metrics": {"children_total": 0}}
    assert await dispatch("child_abort", {"slot": 7}, "ctx") == {"aborted": True}
    assert await dispatch("find_docs", {"q": "x"}, "ctx") == {"inner": True}
    spawn.assert_awaited_once_with({"slot": "a"})
    abort.assert_awaited_once_with("7")
    inner.assert_awaited_once_with("find_docs", {"q": "x"}, "ctx")


async def test_rejected_respawn_is_audited(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    runner = _runner(backend)
    rejection = MissionValidationError(
        "validation_error", details={"field": "spawn", "reason": "pool_exhausted"}
    )
    monkeypatch.setattr(
        runner_module.swarm_rules, "validate_spawn", MagicMock(side_effect=rejection)
    )
    audit = MagicMock()
    monkeypatch.setattr(runner_module.mission_audit, "emit_child_lifecycle_event", audit)

    result = await runner.spawn({}, respawn_of_slot="worker-a")

    assert result == {"code": "validation_error", "details": rejection.details}
    audit.assert_called_once_with(
        "mission-orchestrator",
        None,
        "worker-a",
        "respawn_denied",
        reason="pool_exhausted",
    )


async def test_abort_unknown_settled_and_running_slots(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    runner = _runner(backend)
    unknown = await runner.abort_child("unknown")
    assert unknown["details"]["reason"] == "unknown_slot"

    settled = _entry("settled", session_id="mission-settled", settled=True)
    runner._registry.append(settled)
    original_terminate = runner._terminate_child_session
    terminate = MagicMock()
    monkeypatch.setattr(runner, "_terminate_child_session", terminate)
    assert await runner.abort_child("settled") == {"aborted": True, "slot": "settled"}
    terminate.assert_not_called()
    monkeypatch.setattr(runner, "_terminate_child_session", original_terminate)

    child = _child(backend)
    runner._registry.append(_entry(session_id=child["session_id"]))
    started = asyncio.Event()

    async def sleeper() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(sleeper())
    await started.wait()
    runner._tasks["worker-a"] = task
    result = await runner.abort_child("worker-a")

    assert result == {"aborted": True, "slot": "worker-a"}
    assert task.cancelled()
    saved = backend.load_session(child["session_id"])
    assert saved is not None and saved["status"] == "terminated"
    assert runner._registry[1]["settled"] is True


def test_terminate_missing_and_already_terminal_children(backend: FilesystemBackend) -> None:
    _parent(backend)
    runner = _runner(backend)
    assert runner._terminate_child_session("missing") == 0

    child = _child(
        backend,
        session_id="mission-terminal",
        status="completed",
        iterations=[{"iteration_index": 1}, {"iteration_index": 2}],
    )
    assert runner._terminate_child_session(child["session_id"]) == 2
    saved = backend.load_session(child["session_id"])
    assert saved is not None
    assert saved["status"] == "completed"
    assert saved["final_verdict"] == "complete"


async def test_child_driver_failure_terminates_and_settles_slot(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _child(backend)
    _parent(backend, children=[_entry(session_id=child["session_id"])])
    runner = _runner(backend)
    writer = MagicMock()
    monkeypatch.setattr(runner, "_child_writer", lambda _slot: writer)
    monkeypatch.setattr(
        runner,
        "_build_child_engine",
        AsyncMock(side_effect=RuntimeError("engine construction failed")),
    )

    await runner._drive_child("worker-a")

    saved = backend.load_session(child["session_id"])
    assert saved is not None and saved["status"] == "terminated"
    assert runner._registry[0]["settled"] is True
    writer.finish.assert_called_once_with(state="failed", error="engine construction failed")


def test_settle_is_idempotent_for_already_settled_slot(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend, children=[_entry(settled=True)])
    runner = _runner(backend)
    progress = MagicMock()
    audit = MagicMock()
    monkeypatch.setattr(runner, "_note_fleet_progress", progress)
    monkeypatch.setattr(runner_module.mission_audit, "emit_child_lifecycle_event", audit)

    runner._settle_slot("worker-a", consumed=4, status="failed")

    progress.assert_not_called()
    audit.assert_not_called()
    assert runner._dirty is False


@pytest.mark.parametrize(
    ("revision", "expected"),
    [("Try a narrower query.", "Try a narrower query."), ("", "Perform child work.")],
)
async def test_respawn_revision_is_advisory(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
    expected: str,
) -> None:
    child = _child(backend, status="failed")
    entry = _entry(
        session_id=child["session_id"],
        settled=True,
        restart_policy="on_failure_with_revision",
        max_respawns=1,
    )
    _parent(backend, children=[entry])
    reviser = AsyncMock(return_value=revision)
    runner = _runner(backend, reviser=reviser)
    spawn = AsyncMock(return_value={"spawned": True})
    monkeypatch.setattr(runner, "spawn", spawn)

    await runner._maybe_respawn("worker-a", "failed", child)  # type: ignore[arg-type]

    request = spawn.await_args.args[0]
    assert request["directive"] == expected
    assert request["criteria"][0]["criterion_id"] == "done"
    assert spawn.await_args.kwargs == {"respawn_of_slot": "worker-a"}


async def test_run_to_completion_handles_terminal_and_paused_sessions(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _parent(backend, status="completed")
    runner = _runner(backend)
    engine = SimpleNamespace(run_iteration=AsyncMock())
    monkeypatch.setattr(runner, "_build_orchestrator_engine", AsyncMock(return_value=engine))
    final = await runner.run_to_completion()
    assert final["session_id"] == terminal["session_id"]
    assert final["status"] == "completed"
    engine.run_iteration.assert_not_awaited()

    paused = _parent(backend, session_id="mission-paused", status="paused")
    paused_runner = _runner(backend, parent_id=paused["session_id"])
    writer = MagicMock()

    def acquire() -> None:
        paused_runner._swarm_writer = writer

    monkeypatch.setattr(paused_runner, "_acquire_guard", acquire)
    monkeypatch.setattr(paused_runner, "_build_orchestrator_engine", AsyncMock(return_value=engine))
    detached = await paused_runner.run_to_completion()
    assert detached["status"] == "paused"
    writer.finish.assert_called_once_with(state="cancelled")


async def test_run_to_completion_reports_orchestrator_disappearance(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _parent(backend)
    runner = _runner(backend)
    monkeypatch.setattr(runner, "_acquire_guard", lambda: None)
    monkeypatch.setattr(runner, "_reconcile_orphans", lambda: None)
    monkeypatch.setattr(
        runner,
        "_build_orchestrator_engine",
        AsyncMock(return_value=SimpleNamespace(run_iteration=AsyncMock())),
    )
    loads = iter([parent, None])
    monkeypatch.setattr(backend, "load_session", lambda _session_id: next(loads))

    with pytest.raises(MissionValidationError) as exc_info:
        await runner.run_to_completion()

    assert exc_info.value.code == "session_not_found"
    assert exc_info.value.details == {"session_id": "mission-orchestrator"}


async def test_orchestrator_iteration_observer_and_terminal_writer(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    observed: list[dict[str, Any]] = []
    runner = _runner(backend, observer=lambda record: observed.append(dict(record)))
    writer = MagicMock()

    def acquire() -> None:
        runner._swarm_writer = writer

    class Engine:
        async def run_iteration(self, session_id: str) -> dict[str, Any]:
            parent = backend.load_session(session_id)
            assert parent is not None
            parent["status"] = "completed"
            parent["final_verdict"] = "complete"
            backend.save_session(parent)
            return {"iteration_index": 1, "verdict": "complete", "verdict_reason": "done"}

    monkeypatch.setattr(runner, "_acquire_guard", acquire)
    monkeypatch.setattr(runner, "_build_orchestrator_engine", AsyncMock(return_value=Engine()))
    final = await runner.run_to_completion()

    assert final["final_verdict"] == "complete"
    assert observed == [{"iteration_index": 1, "verdict": "complete", "verdict_reason": "done"}]
    writer.finish.assert_called_once_with(state="succeeded")


def test_report_refresh_noops_without_path_or_registry(backend: FilesystemBackend) -> None:
    _parent(backend)
    runner = _runner(backend)
    runner._refresh_report_children({"children": []})  # type: ignore[arg-type]
    runner._refresh_report_children({"final_report_path": "unused"})  # type: ignore[arg-type]


async def test_cascade_cancels_driver_and_aborts_live_child(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _child(backend)
    _parent(backend, children=[_entry(session_id=child["session_id"])])
    runner = _runner(backend)
    writer = MagicMock()
    runner._child_writers["worker-a"] = writer
    audit = MagicMock()
    monkeypatch.setattr(runner_module.mission_audit, "emit_child_lifecycle_event", audit)

    started = asyncio.Event()

    async def sleeper() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(sleeper())
    await started.wait()
    runner._tasks["worker-a"] = task

    await runner._cascade_shutdown()

    assert runner._tasks == {}
    assert task.cancelled()
    saved = backend.load_session(child["session_id"])
    assert saved is not None and saved["status"] == "terminated"
    assert runner._registry[0]["settled"] is True
    writer.finish.assert_called_once_with(state="cancelled")
    audit.assert_called_once()


def test_rollup_reports_unreadable_child_and_exhausted_pool(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(reserved_iterations=3)
    parent = _parent(
        backend,
        children=[entry],
        swarm=_config(child_iteration_pool=3),
    )
    monkeypatch.setattr(runner_module, "get_task", lambda _task_id: None)

    rollup = build_fleet_rollup(backend, parent)  # type: ignore[arg-type]

    assert rollup["children"][0]["status"] == "unreadable"
    assert rollup["pool"]["remaining"] == 0
    assert any("unreadable child sessions: worker-a" in item for item in rollup["findings"])
    assert any("iteration pool exhausted" in item for item in rollup["findings"])


def test_list_swarms_skips_missing_and_non_orchestrator_candidates() -> None:
    valid = {
        "session_id": "mission-valid",
        "status": "running",
        "created_at": "now",
        "role": "orchestrator",
        "children": [_entry(), _entry("settled", settled=True)],
    }
    sessions = {
        "mission-missing": None,
        "mission-child": {"session_id": "mission-child", "role": "child"},
        "mission-valid": valid,
    }
    backend = SimpleNamespace(
        list_sessions=MagicMock(
            return_value=[
                {"session_id": "mission-missing"},
                {"session_id": "mission-child"},
                {"session_id": "mission-valid"},
            ]
        ),
        load_session=lambda session_id: sessions[session_id],
    )

    rows = list_swarms(backend, status="running")

    assert rows == [
        {
            "session_id": "mission-valid",
            "status": "running",
            "created_at": "now",
            "children_total": 2,
            "children_live": 1,
        }
    ]
    backend.list_sessions.assert_called_once_with(filter={"status": "running"})


# ---------------------------------------------------------------------------
# Remaining baseline-only cancellation and loop edges
# ---------------------------------------------------------------------------


async def test_child_driver_cancellation_finishes_heartbeat_and_propagates(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _child(backend)
    _parent(backend, children=[_entry(session_id=child["session_id"])])
    runner = _runner(backend)
    writer = MagicMock()
    monkeypatch.setattr(runner, "_child_writer", lambda _slot: writer)
    entered = asyncio.Event()

    class BlockingEngine:
        async def run_iteration(self, _session_id: str) -> dict[str, Any]:
            entered.set()
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    monkeypatch.setattr(
        runner,
        "_build_child_engine",
        AsyncMock(return_value=BlockingEngine()),
    )
    task = asyncio.create_task(runner._drive_child("worker-a"))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    writer.finish.assert_called_once_with(state="cancelled")
    saved = backend.load_session(child["session_id"])
    assert saved is not None and saved["status"] == "running"
    assert runner._registry[0].get("settled") is not True


async def test_terminal_iteration_without_observer_and_detached_cleanup(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _parent(backend)
    runner = _runner(backend)
    monkeypatch.setattr(runner, "_acquire_guard", lambda: None)

    class CompletingEngine:
        async def run_iteration(self, session_id: str) -> dict[str, Any]:
            parent = backend.load_session(session_id)
            assert parent is not None
            parent["status"] = "completed"
            parent["final_verdict"] = "complete"
            backend.save_session(parent)
            return {"iteration_index": 1, "verdict": "complete", "verdict_reason": "done"}

    monkeypatch.setattr(
        runner,
        "_build_orchestrator_engine",
        AsyncMock(return_value=CompletingEngine()),
    )
    final = await runner.run_to_completion()
    assert final["status"] == "completed"
    assert runner._on_orchestrator_iteration is None
    assert runner._swarm_writer is None

    paused = _parent(backend, session_id="mission-paused-cleanup", status="paused")
    detached = _runner(backend, parent_id=paused["session_id"])
    monkeypatch.setattr(detached, "_acquire_guard", lambda: None)
    monkeypatch.setattr(
        detached,
        "_build_orchestrator_engine",
        AsyncMock(return_value=SimpleNamespace(run_iteration=AsyncMock())),
    )
    started = asyncio.Event()

    async def sleeper() -> None:
        started.set()
        await asyncio.sleep(60)

    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    live = asyncio.create_task(sleeper())
    await started.wait()
    detached._tasks = {"completed": completed, "live": live}
    result = await detached.run_to_completion()

    assert result["status"] == "paused"
    assert detached._swarm_writer is None
    assert completed.done() and not completed.cancelled()
    assert live.cancelled()


async def test_cascade_handles_multiple_tasks_and_children(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _child(backend, session_id="mission-child-one")
    second = _child(backend, session_id="mission-child-two")
    _parent(
        backend,
        children=[
            _entry("one", session_id=first["session_id"]),
            _entry("two", session_id=second["session_id"]),
        ],
    )
    runner = _runner(backend)
    first_writer = MagicMock()
    runner._child_writers["one"] = first_writer
    audit = MagicMock()
    monkeypatch.setattr(runner_module.mission_audit, "emit_child_lifecycle_event", audit)
    started = [asyncio.Event(), asyncio.Event()]

    async def sleeper(index: int) -> None:
        started[index].set()
        await asyncio.sleep(60)

    tasks = [asyncio.create_task(sleeper(index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in started))
    runner._tasks = {"one": tasks[0], "two": tasks[1]}

    await runner._cascade_shutdown()

    assert all(task.cancelled() for task in tasks)
    assert all(row["settled"] for row in runner._registry)
    assert backend.load_session(first["session_id"])["status"] == "terminated"  # type: ignore[index]
    assert backend.load_session(second["session_id"])["status"] == "terminated"  # type: ignore[index]
    assert audit.call_count == 2
    first_writer.finish.assert_called_once_with(state="cancelled")


async def test_revised_directive_without_original_is_not_sampled() -> None:
    scaffold = __import__("mission.swarm_scaffold", fromlist=["sample_revised_directive"])
    backend = SimpleNamespace(sample=AsyncMock(side_effect=AssertionError("must not sample")))
    assert await scaffold.sample_revised_directive(backend, {"directive_text": "   "}) is None
    backend.sample.assert_not_awaited()


@pytest.mark.parametrize("stage", ["initial", "terminal", "detached"])
async def test_run_to_completion_maps_every_concurrent_parent_deletion(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    session_id = f"mission-disappears-{stage}"
    parent = _parent(backend, session_id=session_id)
    runner = _runner(backend, parent_id=session_id)
    monkeypatch.setattr(runner, "_acquire_guard", lambda: None)
    monkeypatch.setattr(runner, "_reconcile_orphans", lambda: None)
    monkeypatch.setattr(runner, "_flush_registry", lambda: None)
    monkeypatch.setattr(runner, "_cascade_shutdown", AsyncMock())
    monkeypatch.setattr(
        runner,
        "_build_orchestrator_engine",
        AsyncMock(return_value=SimpleNamespace(run_iteration=AsyncMock())),
    )

    if stage == "initial":
        snapshots: list[dict[str, Any] | None] = [None]
    elif stage == "terminal":
        snapshots = [parent, {**parent, "status": "completed"}, None]
    else:
        snapshots = [parent, {**parent, "status": "paused"}, None]
    loads = iter(snapshots)
    monkeypatch.setattr(backend, "load_session", lambda _session_id: next(loads))

    with pytest.raises(MissionValidationError) as exc_info:
        await runner.run_to_completion()

    assert exc_info.value.code == "session_not_found"
    assert exc_info.value.details == {"session_id": session_id}
