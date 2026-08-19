"""Tests for the swarm runner: drivers, supervisor tools, cascade, guard.

Everything runs against the filesystem state backend in a tmp dir, a
stub dispatcher, and the disk task-status channel redirected via
``GCO_TASK_STATUS_DIR`` — no MCP server, no AWS, no sampling.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission._engine_factory import EngineDependencies  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.swarm_runner import (  # noqa: E402
    SwarmRunner,
    SwarmRunnerBusyError,
    build_children_snapshot,
)
from mission.types import SCHEMA_VERSION  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

REGISTERED_TOOLS: dict[str, Any] = {"fake_tool": object(), "other_tool": object()}
REGISTERED_TAGS: dict[str, set[str]] = {
    "fake_tool": {"safe"},
    "other_tool": {"low-risk"},
}


@pytest.fixture(autouse=True)
def _isolated_task_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect heartbeat files away from the developer's ~/.gco."""
    tasks_dir = tmp_path / "tasks"
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tasks_dir))
    monkeypatch.delenv("GCO_DISABLE_TASK_STATUS", raising=False)
    return tasks_dir


@pytest.fixture()
def backend(tmp_path: Path) -> FilesystemBackend:
    return FilesystemBackend(root=tmp_path / "missions")


def make_orchestrator(
    backend: FilesystemBackend,
    *,
    session_id: str = "mission-orch01",
    max_iterations: int = 30,
    criteria: list[dict[str, Any]] | None = None,
    swarm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a minimal orchestrator session and return it."""
    if criteria is None:
        criteria = [
            {
                "criterion_id": "fleet_done",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_completed",
                "op": ">=",
                "target": 2,
            }
        ]
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Supervise the fleet.",
        "criteria": criteria,
        "budget": {"max_iterations": max_iterations, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["children_status"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 50,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "role": "orchestrator",
        "swarm": {
            "max_children": 4,
            "child_iteration_pool": 40,
            "max_concurrent_children": 2,
            "allow_overlapping_mutating_tools": False,
            **(swarm or {}),
        },
        "children": [],
    }
    backend.save_session(session)  # type: ignore[arg-type]
    return session


def make_deps_builder(dispatcher: Any) -> Any:
    async def build(session: Any) -> EngineDependencies:
        del session
        return EngineDependencies(
            tool_dispatcher=dispatcher,
            sampling_callable=None,
            sandbox_runner=None,
        )

    return build


async def ok_dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return {"payload": "ok"}


def make_runner(
    backend: FilesystemBackend,
    *,
    orchestrator_id: str = "mission-orch01",
    dispatcher: Any = ok_dispatcher,
    revise_directive: Any = None,
) -> SwarmRunner:
    return SwarmRunner(
        backend=backend,
        orchestrator_id=orchestrator_id,
        deps_builder=make_deps_builder(dispatcher),
        registered_tools=REGISTERED_TOOLS,
        registered_tags=REGISTERED_TAGS,
        revise_directive=revise_directive,
    )


def child_request(slot: str, *, iterations: int = 3, **overrides: Any) -> dict[str, Any]:
    """A spawn request whose child completes on its first iteration."""
    request: dict[str, Any] = {
        "slot": slot,
        "directive": f"Do the {slot} work.",
        "criteria": [
            {
                "criterion_id": "tool_ran",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "fake_tool",
            }
        ],
        "budget": {"max_iterations": iterations, "max_wall_clock_seconds": 120},
        "tool_allowlist": ["fake_tool"],
    }
    request.update(overrides)
    return request


def failing_child_request(slot: str, *, iterations: int = 2, **overrides: Any) -> dict[str, Any]:
    """A spawn request whose child terminates unmet at its iteration cap."""
    request = child_request(slot, iterations=iterations, **overrides)
    request["criteria"] = [
        {
            "criterion_id": "unreachable",
            "kind": "metric_threshold",
            "required": True,
            "metric": "metrics.never_present",
            "op": ">=",
            "target": 1,
        }
    ]
    return request


def load(backend: FilesystemBackend, session_id: str) -> dict[str, Any]:
    session = backend.load_session(session_id)
    assert session is not None
    return dict(session)


# ---------------------------------------------------------------------------
# Happy path: fleet drives to orchestrator completion
# ---------------------------------------------------------------------------


async def test_swarm_completes_when_children_complete(
    backend: FilesystemBackend, _isolated_task_status: Path
) -> None:
    """Two completing children flip the orchestrator's criterion."""
    make_orchestrator(backend)
    runner = make_runner(backend)
    first = await runner.spawn(child_request("worker-a"))
    second = await runner.spawn(child_request("worker-b"))
    assert first["spawned"] is True
    assert second["spawned"] is True

    final = await runner.run_to_completion()

    assert final["status"] == "completed"
    assert final["final_verdict"] == "complete"
    children = final["children"]
    assert {entry["slot"] for entry in children} == {"worker-a", "worker-b"}
    assert all(entry["settled"] for entry in children)
    for entry in children:
        child = load(backend, entry["session_id"])
        assert child["status"] == "completed"
        assert child["role"] == "child"
        assert child["parent_session_id"] == "mission-orch01"
        # Consumption folded, reservation refunded.
        assert entry["consumed_iterations"] == len(child["iterations"])
        assert entry["reserved_iterations"] == 0
    # Heartbeats exist for the swarm and each slot.
    stems = {path.stem for path in _isolated_task_status.glob("*.json")}
    assert "swarm-mission-orch01" in stems
    assert "swarm-mission-orch01-worker-a" in stems
    assert "swarm-mission-orch01-worker-b" in stems


async def test_orchestrator_observation_carries_children(
    backend: FilesystemBackend,
) -> None:
    """The augmenter lands the fleet snapshot on orchestrator iterations."""
    make_orchestrator(backend)
    runner = make_runner(backend)
    await runner.spawn(child_request("worker-a"))
    await runner.spawn(child_request("worker-b"))
    final = await runner.run_to_completion()

    last_obs = final["iterations"][-1]["observation"]
    slots = [row["slot"] for row in last_obs["children"]]
    assert slots == sorted(slots)
    assert last_obs["metrics"]["children_completed"] == 2
    assert last_obs["metrics"]["children_total"] == 2


# ---------------------------------------------------------------------------
# Terminal cascade
# ---------------------------------------------------------------------------


async def test_orchestrator_budget_exhaustion_aborts_children(
    backend: FilesystemBackend,
) -> None:
    """A terminating orchestrator takes its live children down with it."""
    make_orchestrator(backend, max_iterations=2)
    runner = make_runner(backend)
    # Children that can never complete and have generous budgets.
    await runner.spawn(failing_child_request("slow-a", iterations=30))
    final = await runner.run_to_completion()

    assert final["status"] == "terminated"
    entry = final["children"][0]
    assert entry["settled"] is True
    child = load(backend, entry["session_id"])
    assert child["status"] in ("terminated", "failed")
    # Refund happened: consumption cannot exceed what actually ran.
    assert entry["consumed_iterations"] <= 30


# ---------------------------------------------------------------------------
# Restart policy through the runner
# ---------------------------------------------------------------------------


async def test_on_failure_respawns_once_with_lineage(
    backend: FilesystemBackend,
) -> None:
    """A failing slot respawns up to max_respawns, then stays settled."""
    make_orchestrator(
        backend,
        max_iterations=25,
        criteria=[
            {
                "criterion_id": "fleet_settled",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_failed",
                "op": ">=",
                "target": 1,
            }
        ],
    )
    runner = make_runner(backend)
    await runner.spawn(
        failing_child_request("flaky", iterations=1, restart_policy="on_failure", max_respawns=1)
    )
    final = await runner.run_to_completion()

    entry = next(e for e in final["children"] if e["slot"] == "flaky")
    assert entry["respawn_count"] == 1
    assert len(entry["prior_session_ids"]) == 1
    assert entry["prior_session_ids"][0] != entry["session_id"]
    # Both lineage sessions exist and are terminal.
    for session_id in [*entry["prior_session_ids"], entry["session_id"]]:
        assert load(backend, session_id)["status"] in ("terminated", "failed")


async def test_revision_callable_feeds_respawn_directive(
    backend: FilesystemBackend,
) -> None:
    """on_failure_with_revision rewrites the replacement directive."""
    make_orchestrator(
        backend,
        max_iterations=25,
        criteria=[
            {
                "criterion_id": "fleet_settled",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_failed",
                "op": ">=",
                "target": 1,
            }
        ],
    )

    async def reviser(failed_session: dict[str, Any]) -> str:
        return f"Retry differently: {failed_session['directive_text']}"

    runner = make_runner(backend, revise_directive=reviser)
    await runner.spawn(
        failing_child_request(
            "flaky",
            iterations=1,
            restart_policy="on_failure_with_revision",
            max_respawns=1,
        )
    )
    final = await runner.run_to_completion()

    entry = next(e for e in final["children"] if e["slot"] == "flaky")
    replacement = load(backend, entry["session_id"])
    assert replacement["directive_text"].startswith("Retry differently:")


# ---------------------------------------------------------------------------
# Concurrency bound
# ---------------------------------------------------------------------------


async def test_semaphore_bounds_concurrent_child_iterations(
    backend: FilesystemBackend,
) -> None:
    """No more than max_concurrent_children iterate simultaneously."""
    make_orchestrator(
        backend,
        criteria=[
            {
                "criterion_id": "fleet_done",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_completed",
                "op": ">=",
                "target": 3,
            }
        ],
        swarm={"max_concurrent_children": 1},
    )
    active = 0
    peak = 0

    async def counting_dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"payload": "ok"}

    runner = make_runner(backend, dispatcher=counting_dispatcher)
    for slot in ("w-1", "w-2", "w-3"):
        result = await runner.spawn(child_request(slot))
        assert result.get("spawned") is True
    final = await runner.run_to_completion()

    assert final["status"] == "completed"
    assert peak == 1


# ---------------------------------------------------------------------------
# Supervisor tool envelopes and admission through the runner
# ---------------------------------------------------------------------------


async def test_spawn_rejection_envelope_surfaces(backend: FilesystemBackend) -> None:
    """Admission failures come back as structured envelopes, not raises."""
    make_orchestrator(backend, swarm={"max_children": 1})
    runner = make_runner(backend)
    assert (await runner.spawn(child_request("a")))["spawned"] is True
    rejected = await runner.spawn(child_request("b"))
    assert rejected["code"] == "validation_error"
    assert rejected["details"]["reason"] == "fleet_cap_exceeded"


async def test_child_abort_settles_and_terminates(backend: FilesystemBackend) -> None:
    """child_abort ends a live slot and refunds its reservation."""
    make_orchestrator(backend)
    runner = make_runner(backend)
    spawned = await runner.spawn(failing_child_request("doomed", iterations=20))
    result = await runner.abort_child("doomed")
    assert result == {"aborted": True, "slot": "doomed"}
    child = load(backend, spawned["child_session_id"])
    assert child["status"] == "terminated"
    snapshot = runner.children_status()
    assert snapshot["metrics"]["iteration_pool_remaining"] == 40


# ---------------------------------------------------------------------------
# Single-runner guard
# ---------------------------------------------------------------------------


async def test_guard_refuses_live_foreign_pid(
    backend: FilesystemBackend, _isolated_task_status: Path
) -> None:
    """A running heartbeat under a live foreign PID refuses startup."""
    make_orchestrator(backend)
    _isolated_task_status.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": "swarm-mission-orch01",
        "tool": "swarm_run",
        "argv": ["mission-orch01"],
        "pid": 1,  # launchd: alive, definitely not us
        "started_at": "2026-08-19T00:00:00+00:00",
        "updated_at": "2026-08-19T00:00:00+00:00",
        "elapsed_seconds": 1,
        "state": "running",
        "stacks_completed": 0,
        "last_stack": None,
        "last_message": None,
        "tail": [],
        "log_path": str(_isolated_task_status / "swarm-mission-orch01.log"),
    }
    path = _isolated_task_status / "swarm-mission-orch01.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(path, 0o600)

    runner = make_runner(backend)
    with pytest.raises(SwarmRunnerBusyError) as excinfo:
        await runner.run_to_completion()
    assert excinfo.value.holder_pid == 1


async def test_guard_takes_over_dead_pid(
    backend: FilesystemBackend, _isolated_task_status: Path
) -> None:
    """An orphaned record (dead PID) is taken over, not refused."""
    make_orchestrator(backend)
    runner_a = make_runner(backend)
    await runner_a.spawn(child_request("worker-a"))
    await runner_a.spawn(child_request("worker-b"))
    # Forge a stale running record under a PID that cannot exist.
    _isolated_task_status.mkdir(parents=True, exist_ok=True)
    stale = {
        "task_id": "swarm-mission-orch01",
        "tool": "swarm_run",
        "argv": ["mission-orch01"],
        "pid": 4_100_000,
        "started_at": "2026-08-19T00:00:00+00:00",
        "updated_at": "2026-08-19T00:00:00+00:00",
        "elapsed_seconds": 1,
        "state": "running",
        "stacks_completed": 0,
        "last_stack": None,
        "last_message": None,
        "tail": [],
        "log_path": str(_isolated_task_status / "swarm-mission-orch01.log"),
    }
    path = _isolated_task_status / "swarm-mission-orch01.json"
    path.write_text(json.dumps(stale), encoding="utf-8")
    os.chmod(path, 0o600)

    final = await runner_a.run_to_completion()
    assert final["status"] == "completed"


# ---------------------------------------------------------------------------
# Snapshot unit behavior
# ---------------------------------------------------------------------------


def test_snapshot_marks_unreadable_children() -> None:
    """A missing child session surfaces as the distinct unreadable token."""
    config = {
        "max_children": 2,
        "child_iteration_pool": 10,
        "max_concurrent_children": 2,
        "allow_overlapping_mutating_tools": False,
    }
    registry = [
        {
            "slot": "ghost",
            "session_id": "mission-gone",
            "spawned_at": "2026-08-19T00:00:00+00:00",
            "reserved_iterations": 5,
            "restart_policy": "never",
            "max_respawns": 0,
            "respawn_count": 0,
            "consumed_iterations": 0,
        }
    ]
    snapshot = build_children_snapshot(config, registry, lambda _sid: None)  # type: ignore[arg-type]
    assert snapshot["children"][0]["status"] == "unreadable"
    assert snapshot["metrics"]["children_failed"] == 1
    assert snapshot["metrics"]["iteration_pool_remaining"] == 5
