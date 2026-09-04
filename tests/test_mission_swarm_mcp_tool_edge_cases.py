"""Direct, hermetic coverage for the gated Mission and Swarm MCP wrappers."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "gco_mcp"))

import run_mcp  # noqa: E402
from mission import state as mission_state  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.types import SCHEMA_VERSION  # noqa: E402

MISSION_TOOLS = (
    "mission_start",
    "mission_status",
    "mission_iterate",
    "mission_checkpoint",
    "mission_complete",
    "mission_abort",
    "mission_resume",
    "mission_history",
    "mission_list",
    "mission_memory_search",
)
SWARM_TOOLS = (
    "swarm_start",
    "swarm_iterate",
    "swarm_status",
    "swarm_abort",
    "swarm_list",
    "swarm_plan",
)


def _remove_tools() -> None:
    for name in (*MISSION_TOOLS, *SWARM_TOOLS):
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)


def _drop_module(name: str) -> None:
    sys.modules.pop(name, None)
    parent_name, child = name.rsplit(".", 1)
    parent = sys.modules.get(parent_name)
    if parent is not None and hasattr(parent, child):
        delattr(parent, child)


@pytest.fixture
def tool_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, FilesystemBackend]:
    """Fresh gated modules over one isolated backend, restored gated-off."""
    previous = mission_state._BACKEND_INSTANCE
    backend = FilesystemBackend(root=tmp_path / "missions")
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", backend)
    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    monkeypatch.setenv("GCO_ENABLE_SWARM", "true")
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    _remove_tools()
    _drop_module("tools.mission")
    _drop_module("tools.swarm")
    mission_tools = importlib.import_module("tools.mission")
    swarm_tools = importlib.import_module("tools.swarm")
    registered = SimpleNamespace(name="find_docs", tags={"safe"}, description="Find docs")
    monkeypatch.setattr(mission_tools.mcp, "_list_tools", AsyncMock(return_value=[registered]))
    # Both modules share the singleton, so the second assignment is enough but
    # keeping the seam explicit documents what each helper sees.
    monkeypatch.setattr(swarm_tools.mcp, "_list_tools", AsyncMock(return_value=[registered]))
    yield mission_tools, swarm_tools, backend
    _remove_tools()
    _drop_module("tools.mission")
    _drop_module("tools.swarm")
    os.environ["GCO_ENABLE_MISSION"] = "false"
    os.environ["GCO_ENABLE_SWARM"] = "false"
    importlib.import_module("tools.mission")
    importlib.import_module("tools.swarm")
    _remove_tools()
    mission_state._BACKEND_INSTANCE = previous


def _mission_session(backend: FilesystemBackend, session_id: str) -> dict[str, Any]:
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Inspect the mission.",
        "criteria": [
            {
                "criterion_id": "done",
                "kind": "predicate",
                "required": True,
                "expression": "True",
            }
        ],
        "budget": {"max_iterations": 3, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["find_docs"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2026-01-01T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
    }
    backend.save_session(session)  # type: ignore[arg-type]
    return session


def _swarm_criteria() -> list[dict[str, Any]]:
    return [
        {
            "criterion_id": "fleet_done",
            "kind": "metric_threshold",
            "required": True,
            "metric": "metrics.children_completed",
            "op": ">=",
            "target": 1,
        }
    ]


def _swarm_config() -> dict[str, Any]:
    return {
        "max_children": 2,
        "child_iteration_pool": 8,
        "max_concurrent_children": 2,
        "allow_overlapping_mutating_tools": False,
    }


async def test_mission_context_registry_and_docstring_fallbacks(
    tool_modules: tuple[Any, Any, FilesystemBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mission_tools, _swarm_tools, _backend = tool_modules

    # Unit tests have no active FastMCP request; the optional context helper
    # must degrade to None rather than leak RuntimeError.
    assert mission_tools._try_get_context() is None

    monkeypatch.setattr(
        mission_tools.mcp,
        "_list_tools",
        AsyncMock(side_effect=RuntimeError("registry unavailable")),
    )
    assert await mission_tools._registered_tools_dict() == {}

    entries = {
        "documented": SimpleNamespace(description="Useful docs"),
        "undocumented": SimpleNamespace(description=None),
    }
    monkeypatch.setattr(mission_tools, "_registered_tools_dict", AsyncMock(return_value=entries))
    assert await mission_tools._tool_docstrings_dict() == {
        "documented": "Useful docs",
        "undocumented": "",
    }


async def test_mission_abort_without_request_context_still_persists(
    tool_modules: tuple[Any, Any, FilesystemBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mission_tools, _swarm_tools, backend = tool_modules
    session = _mission_session(backend, "mission-abort-no-context")
    monkeypatch.setattr(mission_tools, "_try_get_context", lambda: None)

    payload = json.loads(await mission_tools.mission_abort(session["session_id"]))

    assert payload == {"session_id": session["session_id"], "status": "terminated"}
    stored = backend.load_session(session["session_id"])
    assert stored is not None
    assert stored["status"] == "terminated"
    assert stored["final_verdict"] == "terminate"


async def test_swarm_private_criteria_strip_and_start_without_extra_allowlist(
    tool_modules: tuple[Any, Any, FilesystemBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mission_tools, swarm_tools, backend = tool_modules
    criteria = [
        {"criterion_id": "visible", "_parsed_ast": object(), "_private": "drop"},
        "persist-corruption",
    ]
    assert swarm_tools._strip_private_criteria(criteria) == [
        {"criterion_id": "visible"},
        "persist-corruption",
    ]

    monkeypatch.setattr(
        swarm_tools.mission_sampling,
        "resolve_sampling_state",
        lambda _value: (False, "none"),
    )
    payload = json.loads(
        await swarm_tools.swarm_start(
            directive="Supervise the fleet.",
            criteria=_swarm_criteria(),
            budget={"max_iterations": 5, "max_wall_clock_seconds": 60},
            swarm=_swarm_config(),
            tool_allowlist=None,
            allow_all_tools=False,
            use_sampling=False,
        )
    )

    assert payload["status"] == "pending"
    stored = backend.load_session(payload["session_id"])
    assert stored is not None
    assert stored["tool_allowlist"] == [
        "children_status",
        "mission_spawn",
        "child_abort",
    ]


async def test_swarm_terminal_iterate_and_abort_are_rejected_before_runner(
    tool_modules: tuple[Any, Any, FilesystemBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mission_tools, swarm_tools, backend = tool_modules
    monkeypatch.setattr(
        swarm_tools.mission_sampling,
        "resolve_sampling_state",
        lambda _value: (False, "none"),
    )
    started = json.loads(
        await swarm_tools.swarm_start(
            directive="Supervise the fleet.",
            criteria=_swarm_criteria(),
            budget={"max_iterations": 5, "max_wall_clock_seconds": 60},
            swarm=_swarm_config(),
            use_sampling=False,
        )
    )
    parent = backend.load_session(started["session_id"])
    assert parent is not None
    parent["status"] = "completed"
    parent["final_verdict"] = "complete"
    backend.save_session(parent)

    runner = AsyncMock(side_effect=AssertionError("runner must not be built"))
    monkeypatch.setattr(swarm_tools, "SwarmRunner", runner)
    iterate = json.loads(await swarm_tools.swarm_iterate(started["session_id"]))
    abort = json.loads(await swarm_tools.swarm_abort(started["session_id"]))

    assert iterate == {
        "code": "session_terminal",
        "details": {"session_id": started["session_id"], "status": "completed"},
    }
    assert abort == iterate
    runner.assert_not_called()


async def test_swarm_iterate_and_abort_forward_missing_session_envelopes(
    tool_modules: tuple[Any, Any, FilesystemBackend],
) -> None:
    _mission_tools, swarm_tools, _backend = tool_modules

    iterate = json.loads(await swarm_tools.swarm_iterate("mission-missing"))
    abort = json.loads(await swarm_tools.swarm_abort("mission-missing"))

    expected = {
        "code": "session_not_found",
        "details": {"session_id": "mission-missing"},
    }
    assert iterate == expected
    assert abort == expected
