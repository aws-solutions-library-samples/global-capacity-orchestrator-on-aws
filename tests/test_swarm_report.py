"""Tests for the orchestrator Final_Report's per-child outcome table.

The table appears only on sessions carrying a child registry; standalone
and child session reports are unchanged. The runner's happy path already
finalizes through the ordinary engine terminal path, so this file also
pins the end-to-end presence of the table (and the mission-memory write)
on a real swarm run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))
sys.path.insert(0, str(Path(__file__).parent))

from mission.final_report import build_deterministic_report  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from test_swarm_runner import (  # noqa: E402
    child_request,
    make_orchestrator,
    make_runner,
)


def minimal_session(**overrides: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "version": 1,
        "session_id": "mission-report01",
        "directive_text": "Do the thing.",
        "criteria": [],
        "budget": {"max_iterations": 5, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["find_docs"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "completed",
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }
    session.update(overrides)
    return session


def test_standalone_report_carries_no_swarm_table() -> None:
    """Sessions without a registry produce the pre-swarm report shape."""
    report = build_deterministic_report(minimal_session(), "complete", "criteria_met")  # type: ignore[arg-type]
    assert "swarm_children" not in report


def test_orchestrator_report_rows_are_slot_ordered() -> None:
    """Registry entries land as slot-ordered outcome rows with lineage."""
    session = minimal_session(
        role="orchestrator",
        children=[
            {
                "slot": "zeta",
                "session_id": "mission-z",
                "spawned_at": "2026-01-01T01:00:00Z",
                "reserved_iterations": 0,
                "restart_policy": "never",
                "max_respawns": 0,
                "respawn_count": 0,
                "consumed_iterations": 2,
                "settled": True,
            },
            {
                "slot": "alpha",
                "session_id": "mission-a2",
                "spawned_at": "2026-01-01T02:00:00Z",
                "reserved_iterations": 0,
                "restart_policy": "on_failure",
                "max_respawns": 1,
                "respawn_count": 1,
                "consumed_iterations": 3,
                "settled": True,
                "prior_session_ids": ["mission-a1"],
            },
        ],
    )
    report = build_deterministic_report(session, "complete", "criteria_met")  # type: ignore[arg-type]
    rows = report["swarm_children"]
    assert [row["slot"] for row in rows] == ["alpha", "zeta"]
    assert rows[0]["prior_session_ids"] == ["mission-a1"]
    assert rows[0]["respawn_count"] == 1
    assert rows[1]["iterations_consumed"] == 2
    assert json.loads(json.dumps(rows)) == rows


async def test_live_swarm_report_lands_on_disk(tmp_path: Path, monkeypatch: Any) -> None:
    """A real swarm run writes a report whose swarm table matches the fleet."""
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    backend = FilesystemBackend(root=tmp_path / "missions")
    make_orchestrator(backend)
    runner = make_runner(backend)
    await runner.spawn(child_request("worker-a"))
    await runner.spawn(child_request("worker-b"))
    final = await runner.run_to_completion()

    report_path = Path(final["final_report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = report["swarm_children"]
    assert [row["slot"] for row in rows] == ["worker-a", "worker-b"]
    assert all(row["settled"] for row in rows)
    assert all(row["iterations_consumed"] >= 1 for row in rows)
