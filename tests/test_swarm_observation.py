"""Tests for the engine's observation-augmenter seam.

The seam is the one engine change swarm makes: an optional sequence of
callables applied at the end of every Observe phase. A contribution's
``children`` list lands on the Observation verbatim and its ``metrics``
dict merges exactly like a tool result's. With no augmenters wired
(every session that exists today) the Observe phase output is
byte-identical to the pre-seam engine.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.types import SCHEMA_VERSION  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_session(**overrides: Any) -> dict[str, Any]:
    """A minimal engine-consumable session dict (validators bypassed)."""
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": "sess-augmenter-001",
        "directive_text": "Supervise the fleet to completion.",
        "criteria": [
            {
                "criterion_id": "all_done",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.children_completed",
                "op": ">=",
                "target": 2,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["fake_tool"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 10,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }
    session.update(overrides)
    return session


async def dispatcher(tool_name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Canned tool result carrying one metric so merges are visible."""
    return {"metrics": {"loss": 0.5}, "payload": "ok"}


def fixed_clock() -> datetime:
    """A pinned clock so observations are comparable across engines."""
    return datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


def make_engine(
    backend: FilesystemBackend,
    augmenters: list[Any] | None,
) -> MissionEngine:
    return MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
        now=fixed_clock,
        observation_augmenters=augmenters,
    )


def latest_observation(backend: FilesystemBackend, session_id: str) -> dict[str, Any]:
    loaded = backend.load_session(session_id)
    assert loaded is not None
    return dict(loaded["iterations"][-1]["observation"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_augmenter_merges_children_and_metrics(tmp_path: Path) -> None:
    """The contribution lands: children verbatim, metrics merged."""
    backend = FilesystemBackend(root=tmp_path)
    session = make_session()
    backend.save_session(session)  # type: ignore[arg-type]

    def augmenter(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "children": [
                {"slot": "a", "status": "completed"},
                {"slot": "b", "status": "completed"},
            ],
            "metrics": {"children_completed": 2, "children_total": 2},
        }

    engine = make_engine(backend, [augmenter])
    result = await engine.run_iteration(session["session_id"])

    observation = latest_observation(backend, session["session_id"])
    assert observation["children"] == [
        {"slot": "a", "status": "completed"},
        {"slot": "b", "status": "completed"},
    ]
    # Tool-result metrics and augmenter metrics coexist in one dict.
    assert observation["metrics"]["loss"] == 0.5
    assert observation["metrics"]["children_completed"] == 2
    # The criterion over the augmented metric completes the session.
    assert result["verdict"] == "complete"
    assert result["verdict_reason"] == "criteria_met"


async def test_no_augmenters_is_byte_identical(tmp_path: Path) -> None:
    """The default engine produces byte-identical observations.

    Two engines — one with ``observation_augmenters=None``, one with an
    empty list — run the same session shape under a pinned clock; their
    persisted Observations must serialize identically, which is the
    no-behavior-change contract for every existing session.
    """
    backend_none = FilesystemBackend(root=tmp_path / "none")
    backend_empty = FilesystemBackend(root=tmp_path / "empty")
    session_none = make_session(session_id="sess-none")
    session_empty = make_session(session_id="sess-empty")
    backend_none.save_session(session_none)  # type: ignore[arg-type]
    backend_empty.save_session(session_empty)  # type: ignore[arg-type]

    await make_engine(backend_none, None).run_iteration("sess-none")
    await make_engine(backend_empty, []).run_iteration("sess-empty")

    obs_none = latest_observation(backend_none, "sess-none")
    obs_empty = latest_observation(backend_empty, "sess-empty")
    assert json.dumps(obs_none, sort_keys=True) == json.dumps(obs_empty, sort_keys=True)
    assert "children" not in obs_none


async def test_raising_augmenter_degrades_to_observation_error(tmp_path: Path) -> None:
    """A crashing augmenter records an error; the phase never fails."""
    backend = FilesystemBackend(root=tmp_path)
    session = make_session()
    backend.save_session(session)  # type: ignore[arg-type]

    def broken(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("registry unreadable")

    engine = make_engine(backend, [broken])
    result = await engine.run_iteration(session["session_id"])

    # The iteration ran to a verdict — no phase failure, no failed session.
    assert result["verdict"] in ("continue", "adjust")
    loaded = backend.load_session(session["session_id"])
    assert loaded is not None
    assert loaded["status"] == "running"
    observation = latest_observation(backend, session["session_id"])
    errors = observation.get("errors", [])
    assert any(
        e.get("tool_name") == "_observation_augmenter"
        and "registry unreadable" in str(e.get("error_message"))
        for e in errors
    )
    assert "children" not in observation


async def test_later_augmenter_wins_and_non_dict_ignored(tmp_path: Path) -> None:
    """Ordering is last-writer-wins; junk contributions are skipped."""
    backend = FilesystemBackend(root=tmp_path)
    session = make_session()
    backend.save_session(session)  # type: ignore[arg-type]

    def first(state: dict[str, Any]) -> dict[str, Any]:
        return {"children": [{"slot": "old"}], "metrics": {"children_completed": 1}}

    def junk(state: dict[str, Any]) -> Any:
        return "not a dict"

    def second(state: dict[str, Any]) -> dict[str, Any]:
        return {"children": [{"slot": "new"}], "metrics": {"children_completed": 2}}

    engine = make_engine(backend, [first, junk, second])
    await engine.run_iteration(session["session_id"])

    observation = latest_observation(backend, session["session_id"])
    assert observation["children"] == [{"slot": "new"}]
    assert observation["metrics"]["children_completed"] == 2
