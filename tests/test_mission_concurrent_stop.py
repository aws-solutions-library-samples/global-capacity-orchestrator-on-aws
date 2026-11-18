"""An abort, pause or completion that lands while an iteration runs is kept.

``MissionEngine.run_iteration`` loads the session, awaits its tool calls, then
saves its in-memory copy. ``mission_abort`` / ``mission_complete`` /
``swarm_abort`` write the session in between, and the iteration's save used to
put it back to ``running`` (the next ``mission_iterate`` carried on, and a
swarm's in-flight children kept going). The engine now keeps a session that
was stopped meanwhile as stored and only adds the iteration's record to it.
Real engine, FilesystemBackend in a temp directory, no AWS.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

from mission import SCHEMA_VERSION
from mission.engine import MissionEngine, MissionEngineError
from mission.state import FilesystemBackend

SESSION_ID = "mission-concurrent-stop"


def _session(*, target: int = 1) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "session_id": SESSION_ID,
        "directive_text": "watch the queue",
        "criteria": [
            {
                "criterion_id": "queued",
                "kind": "metric_threshold",
                "metric": "metrics.queued",
                "op": ">=",
                "target": target,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 3600},
        "tool_allowlist": ["queue_stats"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 5,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2026-11-18T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _stop(backend: FilesystemBackend, status: str) -> None:
    """What mission_abort (or --pause, or mission_complete) writes."""
    current = backend.load_session(SESSION_ID)
    assert current is not None
    current["status"] = status
    if status != "paused":
        current["final_verdict"] = "terminate" if status == "terminated" else "complete"
        current["ended_at"] = "2026-11-18T00:00:05+00:00"
    backend.save_session(current)


def _engine(backend: FilesystemBackend, dispatcher: Any) -> MissionEngine:
    return MissionEngine(
        backend=backend, tool_dispatcher=dispatcher, sampling_callable=None, sandbox_runner=None
    )


@pytest.fixture
def backend(tmp_path: Path) -> FilesystemBackend:
    return FilesystemBackend(root=tmp_path / "missions")


@pytest.mark.parametrize("status", ["terminated", "completed", "paused"])
def test_a_stop_during_the_tool_call_survives_the_iteration(
    backend: FilesystemBackend, status: str
) -> None:
    backend.save_session(_session(target=10))

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        _stop(backend, status)
        return {"metrics": {"queued": 0}}

    record = asyncio.run(_engine(backend, dispatcher).run_iteration(SESSION_ID))
    stored = backend.load_session(SESSION_ID)
    assert stored is not None
    assert stored["status"] == status
    assert [item["iteration_index"] for item in stored["iterations"]] == [0]
    assert stored["iterations"][0]["verdict"] == record["verdict"] == "continue"
    assert stored["no_progress_counter"] == 1  # the iteration still counts
    if status != "paused":
        assert stored["ended_at"] == "2026-11-18T00:00:05+00:00"
        with pytest.raises(MissionEngineError, match="session_terminal"):
            asyncio.run(_engine(backend, dispatcher).run_iteration(SESSION_ID))
    else:
        assert "final_verdict" not in stored
        with pytest.raises(MissionEngineError, match="session_paused"):
            asyncio.run(_engine(backend, dispatcher).run_iteration(SESSION_ID))


def test_a_stopped_session_is_not_finalised_by_the_iteration(
    backend: FilesystemBackend,
) -> None:
    """The iteration would have completed the mission; the operator's abort wins."""
    backend.save_session(_session(target=1))

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        _stop(backend, "terminated")
        return {"metrics": {"queued": 5}}

    record = asyncio.run(_engine(backend, dispatcher).run_iteration(SESSION_ID))
    stored = backend.load_session(SESSION_ID)
    assert stored is not None
    assert record["verdict"] == "complete"
    assert (stored["status"], stored["final_verdict"]) == ("terminated", "terminate")
    assert not list(backend.root.glob("*.report.json"))


def test_a_stop_during_finalisation_keeps_one_record(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.save_session(_session(target=1))

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        return {"metrics": {"queued": 5}}

    engine = _engine(backend, dispatcher)
    finalise = engine._finalise_terminal_session

    async def finalise_then_abort(*args: Any) -> None:
        await finalise(*args)
        _stop(backend, "terminated")

    monkeypatch.setattr(engine, "_finalise_terminal_session", finalise_then_abort)
    asyncio.run(engine.run_iteration(SESSION_ID))
    stored = backend.load_session(SESSION_ID)
    assert stored is not None
    assert stored["status"] == "terminated"
    assert len(stored["iterations"]) == 1


def test_a_failed_phase_after_a_stop_keeps_the_stop(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.save_session(_session(target=10))

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        return {"metrics": {"queued": 0}}

    engine = _engine(backend, dispatcher)

    async def evaluate_after_abort(*args: Any) -> None:
        _stop(backend, "terminated")
        raise RuntimeError("evaluator crashed")

    monkeypatch.setattr(engine, "_evaluate_phase", evaluate_after_abort)
    with pytest.raises(RuntimeError, match="evaluator crashed"):
        asyncio.run(engine.run_iteration(SESSION_ID))
    stored = backend.load_session(SESSION_ID)
    assert stored is not None
    assert stored["status"] == "terminated"
    assert len(stored["iterations"]) == 1


def test_without_a_stop_the_iteration_saves_as_before(backend: FilesystemBackend) -> None:
    backend.save_session(_session(target=10))

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        return {"metrics": {"queued": 0}}

    asyncio.run(_engine(backend, dispatcher).run_iteration(SESSION_ID))
    stored = backend.load_session(SESSION_ID)
    assert stored is not None
    assert stored["status"] == "running"
    assert len(stored["iterations"]) == 1


def test_a_session_deleted_meanwhile_is_saved_again(backend: FilesystemBackend) -> None:
    backend.save_session(_session(target=10))

    async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
        backend.delete_session(SESSION_ID)
        return {"metrics": {"queued": 0}}

    asyncio.run(_engine(backend, dispatcher).run_iteration(SESSION_ID))
    stored = backend.load_session(SESSION_ID)
    assert stored is not None
    assert stored["status"] == "running"
