"""Direct behavioral tests for the gated swarm MCP tools.

The module is re-imported under the feature flag for each test and uses an
isolated filesystem backend.  No MCP transport, AWS call, sampling backend, or
long-running child process is involved.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402
from mission import state as mission_state  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.types import SCHEMA_VERSION  # noqa: E402

_TOOL_NAMES = (
    "swarm_start",
    "swarm_iterate",
    "swarm_status",
    "swarm_abort",
    "swarm_list",
    "swarm_plan",
)


def _strip_registrations() -> None:
    for name in _TOOL_NAMES:
        with contextlib.suppress(Exception):
            run_mcp.mcp.local_provider.remove_tool(name)


def _drop_module() -> None:
    sys.modules.pop("tools.swarm", None)
    package = sys.modules.get("tools")
    if package is not None and hasattr(package, "swarm"):
        delattr(package, "swarm")


@pytest.fixture
def tools_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the gated module against isolated state and restore a gated-off cache."""
    previous = mission_state._BACKEND_INSTANCE
    backend = FilesystemBackend(root=tmp_path / "missions")
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", backend)
    monkeypatch.setenv("GCO_ENABLE_SWARM", "true")
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    _strip_registrations()
    _drop_module()
    module = importlib.import_module("tools.swarm")
    registered = SimpleNamespace(name="find_docs", tags={"safe"}, description="Find documentation")
    monkeypatch.setattr(module.mcp, "_list_tools", AsyncMock(return_value=[registered]))
    yield module
    _strip_registrations()
    _drop_module()
    os.environ["GCO_ENABLE_SWARM"] = "false"
    importlib.import_module("tools.swarm")
    _strip_registrations()
    mission_state._BACKEND_INSTANCE = previous


def _criteria() -> list[dict[str, Any]]:
    return [
        {
            "criterion_id": "fleet",
            "kind": "metric_threshold",
            "required": True,
            "metric": "metrics.children_completed",
            "op": ">=",
            "target": 1,
        }
    ]


def _budget() -> dict[str, int]:
    return {"max_iterations": 10, "max_wall_clock_seconds": 300}


def _swarm() -> dict[str, Any]:
    return {
        "max_children": 3,
        "child_iteration_pool": 20,
        "max_concurrent_children": 2,
        "allow_overlapping_mutating_tools": False,
    }


async def _start(module: Any, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "directive": "Research inference capacity and summarize the evidence.",
        "criteria": _criteria(),
        "budget": _budget(),
        "swarm": _swarm(),
        "tool_allowlist": ["find_docs"],
        "use_sampling": False,
    }
    kwargs.update(overrides)
    return json.loads(await module.swarm_start(**kwargs))


def _save_non_orchestrator(backend: FilesystemBackend, session_id: str) -> None:
    backend.save_session(  # type: ignore[arg-type]
        {
            "version": SCHEMA_VERSION,
            "session_id": session_id,
            "status": "pending",
            "role": "standalone",
            "created_at": "2026-01-01T00:00:00Z",
            "iterations": [],
        }
    )


class TestSwarmStartAndHelpers:
    async def test_start_validates_persists_and_strips_private_criteria(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools_module.secrets, "token_hex", lambda _n: "0123456789abcdef")
        monkeypatch.setattr(
            tools_module.mission_sampling,
            "resolve_sampling_state",
            lambda _requested: (False, "none"),
        )

        payload = await _start(tools_module)

        assert payload == {
            "session_id": "mission-0123456789abcdef",
            "status": "pending",
            "use_sampling": False,
            "sampling_backend_resolved": "none",
            "swarm": _swarm(),
        }
        stored = mission_state.get_backend().load_session(payload["session_id"])
        assert stored is not None
        assert stored["role"] == "orchestrator"
        assert stored["tool_allowlist"][0] == "children_status"
        assert set(stored["tool_allowlist"]) == {
            *tools_module.swarm_rules.SUPERVISOR_TOOLS,
            "find_docs",
        }
        assert all("_parsed_ast" not in row for row in stored["criteria"])

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"directive": ""}, "directive"),
            ({"swarm": {"max_children": 0, "child_iteration_pool": 2}}, "swarm"),
            ({"tool_allowlist": ["missing"]}, "tool_allowlist"),
        ],
    )
    async def test_start_returns_structured_validation_errors(
        self, tools_module: Any, overrides: dict[str, Any], field: str
    ) -> None:
        payload = await _start(tools_module, **overrides)
        assert payload["code"] == "validation_error"
        assert payload["details"]["field"] == field

    async def test_registry_helpers_preserve_tags_docs_and_schema(self, tools_module: Any) -> None:
        tool = SimpleNamespace(
            name="find_docs",
            tags={"safe", "docs"},
            description="Find docs",
        )
        tools_module.mcp._list_tools.return_value = [tool]
        assert await tools_module._registered_tools_dict() == {"find_docs": tool}
        assert await tools_module._registered_tool_tags() == {"find_docs": {"safe", "docs"}}
        assert await tools_module._tool_docstrings_dict() == {"find_docs": "Find docs"}

        supervisor, docs = tools_module._supervisor_tool_metadata()
        assert set(supervisor) == set(tools_module.swarm_rules.SUPERVISOR_TOOLS)
        first = next(iter(supervisor.values()))
        assert first.input_schema.model_json_schema()
        assert first.tags == {"swarm", "supervisor"}
        assert docs[first.name] == first.description

    async def test_dependency_builder_adds_supervisor_metadata_only_for_parent(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = SimpleNamespace(tool_dispatcher=AsyncMock())
        factory = AsyncMock(return_value=built)
        monkeypatch.setattr(tools_module, "build_engine_dependencies", factory)
        builder = tools_module._deps_builder_for("ctx")

        assert await builder({"role": "orchestrator"}) is built
        parent_extra = factory.await_args.kwargs["extra_tool_metadata"]
        assert set(parent_extra[0]) == set(tools_module.swarm_rules.SUPERVISOR_TOOLS)
        factory.reset_mock()
        assert await builder({"role": "child"}) is built
        factory.assert_awaited_once_with({"role": "child"}, "ctx", extra_tool_metadata=None)


class TestLookupAndLifecycleTools:
    async def test_missing_and_wrong_role_have_stable_error_envelopes(
        self, tools_module: Any
    ) -> None:
        missing = json.loads(await tools_module.swarm_status("missing"))
        assert missing == {
            "code": "session_not_found",
            "details": {"session_id": "missing"},
        }

        backend = mission_state.get_backend()
        _save_non_orchestrator(backend, "mission-child")
        wrong = json.loads(await tools_module.swarm_status("mission-child"))
        assert wrong["code"] == "validation_error"
        assert wrong["details"]["reason"] == "not_an_orchestrator"

    async def test_status_and_list_forward_complete_documents(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = await _start(tools_module)
        rollup = {
            "session_id": started["session_id"],
            "status": "pending",
            "pool": {"remaining": 20},
            "children": [],
            "findings": [],
        }
        rollup_builder = MagicMock(return_value=rollup)
        lister = MagicMock(return_value=[{"session_id": started["session_id"]}])
        monkeypatch.setattr(tools_module, "build_fleet_rollup", rollup_builder)
        monkeypatch.setattr(tools_module, "list_swarms", lister)

        assert json.loads(await tools_module.swarm_status(started["session_id"])) == rollup
        listed = json.loads(await tools_module.swarm_list(status="running"))
        assert listed == {"swarms": [{"session_id": started["session_id"]}]}
        lister.assert_called_once_with(mission_state.get_backend(), status="running")

    async def test_abort_cascades_live_terminal_missing_and_settled_children(
        self, tools_module: Any
    ) -> None:
        started = await _start(tools_module)
        backend = mission_state.get_backend()
        parent = backend.load_session(started["session_id"])
        assert parent is not None
        live_id = "mission-live"
        terminal_id = "mission-terminal"
        live = {
            "version": SCHEMA_VERSION,
            "session_id": live_id,
            "status": "running",
            "role": "child",
            "iterations": [{"iteration_index": 0}, {"iteration_index": 1}],
        }
        terminal = {
            "version": SCHEMA_VERSION,
            "session_id": terminal_id,
            "status": "completed",
            "role": "child",
            "iterations": [{"iteration_index": 0}],
        }
        backend.save_session(live)  # type: ignore[arg-type]
        backend.save_session(terminal)  # type: ignore[arg-type]
        parent["children"] = [
            {
                "slot": "live",
                "session_id": live_id,
                "spawned_at": "now",
                "reserved_iterations": 5,
                "restart_policy": "never",
                "max_respawns": 0,
                "respawn_count": 0,
                "consumed_iterations": 0,
            },
            {
                "slot": "terminal",
                "session_id": terminal_id,
                "spawned_at": "now",
                "reserved_iterations": 4,
                "restart_policy": "never",
                "max_respawns": 0,
                "respawn_count": 0,
                "consumed_iterations": 0,
            },
            {
                "slot": "missing",
                "session_id": "mission-missing",
                "spawned_at": "now",
                "reserved_iterations": 3,
                "restart_policy": "never",
                "max_respawns": 0,
                "respawn_count": 0,
                "consumed_iterations": 0,
            },
            {
                "slot": "settled",
                "session_id": "mission-old",
                "spawned_at": "now",
                "reserved_iterations": 0,
                "restart_policy": "never",
                "max_respawns": 0,
                "respawn_count": 0,
                "consumed_iterations": 1,
                "settled": True,
            },
        ]
        backend.save_session(parent)

        payload = json.loads(await tools_module.swarm_abort(started["session_id"]))

        assert payload["status"] == "terminated"
        assert payload["children_aborted"] == 3
        saved_parent = backend.load_session(started["session_id"])
        assert saved_parent is not None
        assert saved_parent["status"] == "terminated"
        assert all(row.get("settled") for row in saved_parent["children"])
        saved_live = backend.load_session(live_id)
        assert saved_live is not None
        assert saved_live["status"] == "terminated"
        saved_terminal = backend.load_session(terminal_id)
        assert saved_terminal is not None
        assert saved_terminal["status"] == "completed"

        terminal_error = json.loads(await tools_module.swarm_abort(started["session_id"]))
        assert terminal_error["code"] == "session_terminal"


class TestSwarmIterate:
    async def test_iterate_forwards_bound_and_summarizes_children(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = await _start(tools_module)
        captured: dict[str, Any] = {}
        final = {
            "session_id": started["session_id"],
            "status": "running",
            "final_verdict": None,
            "iterations": [{"iteration_index": 0}],
            "swarm": _swarm(),
            "children": [],
        }

        class Runner:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def run_to_completion(self, **kwargs: Any) -> dict[str, Any]:
                captured.update(kwargs)
                return final

        monkeypatch.setattr(tools_module, "SwarmRunner", Runner)
        monkeypatch.setattr(
            tools_module,
            "build_children_snapshot",
            lambda *_args: {"metrics": {"children_total": 0}},
        )

        payload = json.loads(
            await tools_module.swarm_iterate(started["session_id"], max_orchestrator_iterations=2)
        )

        assert payload == {
            "session_id": started["session_id"],
            "status": "running",
            "final_verdict": None,
            "iterations_run": 1,
            "children": {"children_total": 0},
        }
        assert captured["backend"] is mission_state.get_backend()
        assert captured["orchestrator_id"] == started["session_id"]
        assert captured["max_orchestrator_iterations"] == 2

    @pytest.mark.parametrize("failure", ["busy", "validation"])
    async def test_iterate_maps_runner_failures(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        started = await _start(tools_module)

        class Runner:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def run_to_completion(self, **_kwargs: Any) -> dict[str, Any]:
                if failure == "busy":
                    raise tools_module.SwarmRunnerBusyError(started["session_id"], 4321)
                raise tools_module.MissionValidationError(
                    "validation_error", {"field": "session", "reason": "malformed"}
                )

        monkeypatch.setattr(tools_module, "SwarmRunner", Runner)
        payload = json.loads(await tools_module.swarm_iterate(started["session_id"]))
        if failure == "busy":
            assert payload == {
                "code": "swarm_runner_active",
                "details": {"session_id": started["session_id"], "holder_pid": 4321},
            }
        else:
            assert payload == {
                "code": "validation_error",
                "details": {"field": "session", "reason": "malformed"},
            }

    async def test_iterate_refuses_terminal_session(self, tools_module: Any) -> None:
        started = await _start(tools_module)
        backend = mission_state.get_backend()
        session = backend.load_session(started["session_id"])
        assert session is not None
        session["status"] = "completed"
        backend.save_session(session)

        payload = json.loads(await tools_module.swarm_iterate(started["session_id"]))
        assert payload["code"] == "session_terminal"
        assert payload["details"]["status"] == "completed"


class TestSwarmPlan:
    async def test_sampled_plan_success_reports_sampling_path(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sampled = [{"slot": "researcher", "directive": "Research"}]
        backend_obj = object()
        monkeypatch.setattr(
            tools_module.mission_sampling,
            "resolve_sampling_state",
            lambda *_args: (True, "bedrock"),
        )
        monkeypatch.setattr(
            tools_module.mission_sampling,
            "select_sampling_backend",
            lambda *_args: backend_obj,
        )
        generator = AsyncMock(return_value=sampled)
        deterministic = MagicMock()
        monkeypatch.setattr(tools_module.swarm_scaffold, "generate_sampled_plan", generator)
        monkeypatch.setattr(
            tools_module.swarm_scaffold, "generate_deterministic_plan", deterministic
        )

        payload = json.loads(
            await tools_module.swarm_plan(
                "Research capacity",
                _swarm(),
                tool_allowlist=["find_docs"],
                max_children=1,
                use_sampling=True,
                retries=2,
            )
        )

        assert payload == {
            "plan": sampled,
            "sampling_path": True,
            "sampling_backend_resolved": "bedrock",
            "fallback_reason": None,
        }
        assert generator.await_args.args[0] is backend_obj
        assert generator.await_args.kwargs["max_children"] == 1
        assert generator.await_args.kwargs["tool_allowlist"] == ["find_docs"]
        assert generator.await_args.kwargs["retries"] == 2
        deterministic.assert_not_called()

    @pytest.mark.parametrize("backend_available", [True, False])
    async def test_plan_falls_back_deterministically(
        self,
        tools_module: Any,
        monkeypatch: pytest.MonkeyPatch,
        backend_available: bool,
    ) -> None:
        monkeypatch.setattr(
            tools_module.mission_sampling,
            "resolve_sampling_state",
            lambda *_args: (True, "bedrock"),
        )
        monkeypatch.setattr(
            tools_module.mission_sampling,
            "select_sampling_backend",
            lambda *_args: object() if backend_available else None,
        )
        sampled = AsyncMock(
            side_effect=tools_module.swarm_scaffold.SwarmScaffoldError("invalid_spawn")
        )
        deterministic_plan = [{"slot": "worker-1"}]
        deterministic = MagicMock(return_value=deterministic_plan)
        monkeypatch.setattr(tools_module.swarm_scaffold, "generate_sampled_plan", sampled)
        monkeypatch.setattr(
            tools_module.swarm_scaffold, "generate_deterministic_plan", deterministic
        )

        payload = json.loads(
            await tools_module.swarm_plan(
                "Research capacity", _swarm(), allow_all_tools=True, use_sampling=True
            )
        )

        assert payload["plan"] == deterministic_plan
        assert payload["sampling_path"] is False
        assert payload["fallback_reason"] == (
            "invalid_spawn" if backend_available else "sampling_backend_unavailable"
        )
        if backend_available:
            sampled.assert_awaited_once()
        else:
            sampled.assert_not_awaited()
        assert deterministic.call_args.kwargs["allow_all_tools"] is True

    async def test_plan_maps_validation_failures_from_input_and_fallback(
        self, tools_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        invalid = json.loads(await tools_module.swarm_plan("", _swarm()))
        assert invalid["code"] == "validation_error"

        monkeypatch.setattr(
            tools_module.mission_sampling,
            "resolve_sampling_state",
            lambda *_args: (False, "none"),
        )
        monkeypatch.setattr(
            tools_module.swarm_scaffold,
            "generate_deterministic_plan",
            MagicMock(
                side_effect=tools_module.MissionValidationError(
                    "validation_error", {"field": "plan", "reason": "unsafe"}
                )
            ),
        )
        fallback = json.loads(await tools_module.swarm_plan("Research", _swarm()))
        assert fallback == {
            "code": "validation_error",
            "details": {"field": "plan", "reason": "unsafe"},
        }
