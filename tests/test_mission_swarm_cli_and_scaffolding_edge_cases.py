"""Behavioral coverage for Mission/Swarm CLI, pure rules, and resources.

Every test is hermetic: state lives under ``tmp_path``; tool registries,
sampling backends, and engines are small in-process fakes; no AWS, MCP
transport, home-directory state, or network call is used.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import click
import pytest
from click.testing import CliRunner

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "gco_mcp"))

mission_cmd_mod = importlib.import_module("cli.commands.mission_cmd")
swarm_cmd_mod = importlib.import_module("cli.commands.swarm_cmd")

from mission import state as mission_state  # noqa: E402
from mission.engine import MissionEngineError  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402
from mission.types import SCHEMA_VERSION  # noqa: E402
from mission.validation import MissionValidationError  # noqa: E402

from gco.bedrock import BedrockFTUFormNotAcceptedError  # noqa: E402


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FilesystemBackend:
    """Pin both command groups to isolated state and enable only their gates."""
    fs = FilesystemBackend(root=tmp_path / "missions")
    monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", fs)
    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    monkeypatch.setenv("GCO_ENABLE_SWARM", "true")
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
    monkeypatch.setenv("GCO_TASK_STATUS_DIR", str(tmp_path / "tasks"))
    return fs


def _mission_session(
    backend: FilesystemBackend,
    *,
    session_id: str = "mission-coverage",
    status: str = "running",
    iterations: list[Any] | None = None,
) -> dict[str, Any]:
    session: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": "Inspect mission behavior.",
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
        "sampling_backend_resolved": "none",
        "allow_scripted_strategies": False,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "iterations": list(iterations or []),
        "no_progress_counter": 0,
    }
    backend.save_session(session)  # type: ignore[arg-type]
    return session


def _swarm_config(**overrides: Any) -> dict[str, Any]:
    return {
        "max_children": 3,
        "child_iteration_pool": 12,
        "max_concurrent_children": 2,
        "allow_overlapping_mutating_tools": False,
        **overrides,
    }


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


def _valid_plan_entry(slot: str = "worker-1") -> dict[str, Any]:
    return {
        "slot": slot,
        "directive": "Find the relevant documentation.",
        "criteria": [
            {
                "criterion_id": "docs_found",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
        ],
        "budget": {"max_iterations": 3, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["find_docs"],
    }


# ---------------------------------------------------------------------------
# Mission CLI helpers and lifecycle paths
# ---------------------------------------------------------------------------


def test_mission_helper_fallbacks_are_observable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = ["persisted", "corruption"]
    assert mission_cmd_mod._strip_iteration(corrupt) is corrupt

    mission_cmd_mod._emit_error("plain_error")
    assert json.loads(capsys.readouterr().err) == {"code": "plain_error"}

    sentinel = object()
    factory = importlib.import_module("mission._engine_factory")
    monkeypatch.setattr(factory, "make_stub_dispatcher", lambda: sentinel)
    assert mission_cmd_mod._make_stub_dispatcher() is sentinel


def test_mission_registry_snapshot_derives_control_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = [
        SimpleNamespace(name="find_docs", tags={"safe"}),
        SimpleNamespace(name="mission_status", tags={"mission", "safe"}),
        SimpleNamespace(name="untagged", tags=None),
    ]
    server = importlib.import_module("server")
    ensure = MagicMock()
    monkeypatch.setattr(mission_cmd_mod, "_ensure_tool_registry", ensure)
    monkeypatch.setattr(server.mcp, "_list_tools", AsyncMock(return_value=tools))

    registered, controls = mission_cmd_mod._resolve_registered_tools_for_cli()

    ensure.assert_called_once_with()
    assert list(registered) == ["find_docs", "mission_status", "untagged"]
    assert controls == {"mission_status"}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--cadence", "every_n_iterations", "--cadence-n", "2"), ("n", 2)),
        (("--cadence", "every_t_seconds", "--cadence-t", "9"), ("t", 9)),
        (("--cadence", "on_event", "--cadence-event", "ready"), ("event_name", "ready")),
    ],
)
def test_mission_start_persists_optional_cadence_fields_and_model(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    expected: tuple[str, Any],
) -> None:
    sampling = importlib.import_module("mission.sampling")
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (False, "none"))

    result = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        [
            "start",
            "--directive",
            "Verify cadence persistence.",
            "--with-defaults",
            "--max-iterations",
            "3",
            "--max-wall-clock",
            "60",
            "--tool-allowlist",
            "find_docs",
            "--bedrock-model-id",
            "model-test",
            *args,
        ],
    )

    assert result.exit_code == 0, result.output
    session_id = json.loads(result.stdout)["session_id"]
    stored = backend.load_session(session_id)
    assert stored is not None
    key, value = expected
    assert stored["checkpoint_cadence"][key] == value
    assert stored["bedrock_model_id"] == "model-test"


def test_mission_status_resume_checkpoint_and_history_edges(
    backend: FilesystemBackend,
) -> None:
    iteration = {
        "iteration_index": 1,
        "verdict": "continue",
        "verdict_reason": "more_work",
        "started_at": "start",
        "ended_at": "end",
        "checkpoint_evaluated": True,
        "observation": {"tool_results": [], "errors": []},
        "strategy": {"rationale": "inspect", "tool_calls": []},
    }
    session = _mission_session(
        backend,
        session_id="mission-history",
        status="paused",
        iterations=[iteration, "corrupt-entry"],
    )
    runner = CliRunner()

    status = runner.invoke(mission_cmd_mod.mission_cmd, ["status", session["session_id"]])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["status"] == "paused"

    resume = runner.invoke(
        mission_cmd_mod.mission_cmd,
        ["resume", session["session_id"], "--output", "table"],
    )
    assert resume.exit_code == 0
    assert f"Session {session['session_id']}: running" in resume.stdout

    full = runner.invoke(
        mission_cmd_mod.mission_cmd,
        ["history", session["session_id"], "--format", "full", "--output", "table"],
    )
    assert full.exit_code == 0
    assert "Iteration 1: continue (more_work)" in full.stdout
    assert "corrupt-entry" not in full.stdout

    summary = runner.invoke(
        mission_cmd_mod.mission_cmd,
        ["history", session["session_id"], "--format", "summary"],
    )
    assert summary.exit_code == 0, summary.output
    assert json.loads(summary.stdout)["iterations"] == [
        {
            "iteration_index": 1,
            "verdict": "continue",
            "verdict_reason": "more_work",
            "started_at": "start",
            "ended_at": "end",
            "checkpoint_evaluated": True,
        }
    ]

    empty = _mission_session(backend, session_id="mission-empty", iterations=[])
    checkpoint = runner.invoke(mission_cmd_mod.mission_cmd, ["checkpoint", empty["session_id"]])
    assert checkpoint.exit_code == 1
    assert json.loads(checkpoint.stderr)["code"] == "no_iterations"


def test_mission_iterate_maps_engine_error(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _mission_session(backend, session_id="mission-iterate-error")

    class BrokenEngine:
        async def run_iteration(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise MissionEngineError("session_failed")

    factory = importlib.import_module("mission._engine_factory")
    monkeypatch.setattr(factory, "build_mission_engine", AsyncMock(return_value=BrokenEngine()))

    result = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        ["iterate", session["session_id"], "--dry-run"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {
        "code": "session_failed",
        "details": {"session_id": session["session_id"]},
    }


def test_run_to_completion_loops_then_falls_back_to_session_json(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _mission_session(backend, session_id="mission-loop")
    records = iter(
        [
            {"iteration_index": 1, "verdict": "continue", "verdict_reason": "again"},
            {"iteration_index": 2, "verdict": "complete", "verdict_reason": "done"},
        ]
    )

    class Engine:
        async def run_iteration(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return next(records)

    factory = importlib.import_module("mission._engine_factory")
    monkeypatch.setattr(factory, "build_mission_engine", AsyncMock(return_value=Engine()))
    monkeypatch.setattr(mission_cmd_mod, "_ensure_tool_registry", MagicMock())

    @click.command()
    def drive() -> None:
        mission_cmd_mod._run_to_completion(session["session_id"])

    result = CliRunner().invoke(drive)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["session_id"] == session["session_id"]
    verdicts = [json.loads(line)["verdict"] for line in result.stderr.splitlines()]
    assert verdicts == ["continue", "complete"]


@pytest.mark.parametrize("mode", ["missing_initially", "engine_error", "disappeared_after_run"])
def test_run_to_completion_failure_envelopes(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    session_id = "mission-run-failure"
    if mode != "missing_initially":
        _mission_session(backend, session_id=session_id)

    class Engine:
        async def run_iteration(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            if mode == "engine_error":
                raise MissionEngineError("session_failed")
            return {"iteration_index": 1, "verdict": "complete", "verdict_reason": "done"}

    factory = importlib.import_module("mission._engine_factory")
    monkeypatch.setattr(factory, "build_mission_engine", AsyncMock(return_value=Engine()))
    monkeypatch.setattr(mission_cmd_mod, "_ensure_tool_registry", MagicMock())

    if mode == "disappeared_after_run":
        persisted = backend.load_session(session_id)
        assert persisted is not None

        class VanishingBackend:
            def __init__(self) -> None:
                self.loads = 0

            def load_session(self, _session_id: str) -> dict[str, Any] | None:
                self.loads += 1
                return dict(persisted) if self.loads == 1 else None

        monkeypatch.setattr(mission_state, "_BACKEND_INSTANCE", VanishingBackend())

    @click.command()
    def drive() -> None:
        mission_cmd_mod._run_to_completion(session_id)

    result = CliRunner().invoke(drive)

    assert result.exit_code == 1
    envelope = json.loads(result.stderr.splitlines()[-1])
    expected = {
        "missing_initially": "session_not_found",
        "engine_error": "session_failed",
        "disappeared_after_run": "session_disappeared",
    }[mode]
    assert envelope == {"code": expected, "details": {"session_id": session_id}}


def test_mission_scaffold_ftu_and_table_file_paths(
    backend: FilesystemBackend,  # noqa: ARG001 - forces hermetic fixture
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sampling = importlib.import_module("mission.sampling")
    scaffold = importlib.import_module("mission.criteria_scaffold")
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (True, "bedrock"))
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda **_kwargs: object())
    monkeypatch.setattr(
        scaffold,
        "generate_sampled_criteria",
        AsyncMock(side_effect=BedrockFTUFormNotAcceptedError("submit the FTU form")),
    )

    ftu = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        ["scaffold-criteria", "--directive", "Find docs.", "--use-sampling"],
    )
    assert ftu.exit_code == 1
    assert "submit the FTU form" in ftu.stderr

    criteria = [
        {
            "criterion_id": "done",
            "kind": "predicate",
            "required": True,
            "expression": "True",
        }
    ]
    output = tmp_path / "criteria.json"
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (False, "none"))
    deterministic = MagicMock(return_value=criteria)
    monkeypatch.setattr(scaffold, "generate_deterministic_criteria", deterministic)

    table = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        [
            "scaffold-criteria",
            "--directive",
            "Find docs.",
            "--no-sampling",
            "--output-file",
            str(output),
            "--output",
            "table",
        ],
    )
    assert table.exit_code == 0
    assert "kind=predicate" in table.stdout
    assert f"written to {output}" in table.stdout
    assert json.loads(output.read_text(encoding="utf-8")) == criteria


def test_mission_run_sampling_fallback_persists_model(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampling = importlib.import_module("mission.sampling")
    scaffold = importlib.import_module("mission.criteria_scaffold")
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (True, "bedrock"))
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda **_kwargs: object())
    monkeypatch.setattr(
        scaffold,
        "generate_sampled_criteria",
        AsyncMock(side_effect=scaffold.ScaffoldSamplingError("invalid_shape")),
    )
    deterministic = [
        {
            "criterion_id": "done",
            "kind": "predicate",
            "required": True,
            "expression": "True",
        }
    ]
    monkeypatch.setattr(
        scaffold, "generate_deterministic_criteria", lambda *_a, **_k: deterministic
    )
    driven: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        mission_cmd_mod,
        "_run_to_completion",
        lambda session_id, *, dry_run=False: driven.append((session_id, dry_run)),
    )

    result = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        [
            "run",
            "--directive",
            "Find docs.",
            "--tool-allowlist",
            "find_docs",
            "--use-sampling",
            "--bedrock-model-id",
            "model-override",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "falling back" in result.stderr
    assert len(driven) == 1
    stored = backend.load_session(driven[0][0])
    assert stored is not None
    assert stored["bedrock_model_id"] == "model-override"
    assert json.loads(result.stderr.splitlines()[-1])["sampling_path"] is False


def test_mission_run_ftu_validation_and_threshold_failures(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampling = importlib.import_module("mission.sampling")
    scaffold = importlib.import_module("mission.criteria_scaffold")
    validation = importlib.import_module("mission.validation")
    base = ["run", "--directive", "Find docs.", "--tool-allowlist", "find_docs"]

    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (True, "bedrock"))
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda **_kwargs: object())
    monkeypatch.setattr(
        scaffold,
        "generate_sampled_criteria",
        AsyncMock(side_effect=BedrockFTUFormNotAcceptedError("FTU required")),
    )
    ftu = CliRunner().invoke(mission_cmd_mod.mission_cmd, [*base, "--use-sampling"])
    assert ftu.exit_code == 1
    assert "FTU required" in ftu.stderr

    criteria = [
        {
            "criterion_id": "done",
            "kind": "predicate",
            "required": True,
            "expression": "True",
        }
    ]
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (False, "none"))
    monkeypatch.setattr(scaffold, "generate_deterministic_criteria", lambda *_a, **_k: criteria)
    monkeypatch.setattr(
        validation,
        "validate_budget",
        MagicMock(
            side_effect=MissionValidationError(
                "validation_error", details={"field": "budget", "reason": "bad"}
            )
        ),
    )
    invalid = CliRunner().invoke(mission_cmd_mod.mission_cmd, [*base, "--no-sampling"])
    assert invalid.exit_code == 1
    assert json.loads(invalid.stderr)["details"]["field"] == "budget"

    monkeypatch.setattr(validation, "validate_budget", lambda budget, *_args: budget)
    threshold = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        [*base, "--no-sampling", "--stagnation-threshold", "0"],
    )
    assert threshold.exit_code == 1
    assert json.loads(threshold.stderr)["details"] == {
        "field": "stagnation-threshold",
        "reason": "must_be_positive_int",
    }
    assert backend.list_sessions() == []


def test_mission_memory_list_maps_unexpected_store_failure(
    backend: FilesystemBackend,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SimpleNamespace(list_memories=MagicMock(side_effect=RuntimeError("index broke")))
    monkeypatch.setattr(mission_cmd_mod, "_build_memory_store", lambda: store)

    result = CliRunner().invoke(mission_cmd_mod.mission_cmd, ["memory", "list"])

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {
        "code": "mission_memory_list_failed",
        "details": {"message": "index broke"},
    }


# ---------------------------------------------------------------------------
# Swarm CLI helpers and fallback/error paths
# ---------------------------------------------------------------------------


def test_swarm_registry_helpers_and_dependency_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools_package = importlib.import_module("tools")
    register = MagicMock()
    monkeypatch.setattr(tools_package, "register_all_tools", register)
    swarm_cmd_mod._ensure_tool_registry()
    register.assert_called_once_with()

    tools = [
        SimpleNamespace(name="find_docs", tags={"safe"}, description="Find docs"),
        SimpleNamespace(name="plain", tags=None, description=None),
    ]
    server = importlib.import_module("server")
    monkeypatch.setattr(swarm_cmd_mod, "_ensure_tool_registry", MagicMock())
    monkeypatch.setattr(server.mcp, "_list_tools", AsyncMock(return_value=tools))
    registered, tags = swarm_cmd_mod._resolve_registered_tools_for_cli()
    assert registered == {"find_docs": tools[0], "plain": tools[1]}
    assert tags == {"find_docs": {"safe"}, "plain": set()}
    assert swarm_cmd_mod._tool_docstrings(registered) == {
        "find_docs": "Find docs",
        "plain": "",
    }

    factory_module = importlib.import_module("mission._engine_factory")
    built = object()
    factory = AsyncMock(return_value=built)
    monkeypatch.setattr(factory_module, "build_engine_dependencies", factory)
    builder = swarm_cmd_mod._deps_builder(dry_run=True)
    assert asyncio.run(builder({"role": "orchestrator"})) is built
    extra = factory.await_args.kwargs["extra_tool_metadata"]
    first = next(iter(extra[0].values()))
    assert first.input_schema.model_json_schema()
    assert first.tags == {"swarm", "supervisor"}


def test_swarm_plain_error_and_persist_allowlist(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    swarm_cmd_mod._emit_error("plain_swarm_error")
    assert json.loads(capsys.readouterr().err) == {"code": "plain_swarm_error"}

    registered = {"find_docs": object()}
    monkeypatch.setattr(
        swarm_cmd_mod,
        "_resolve_registered_tools_for_cli",
        lambda: (registered, {"find_docs": {"safe"}}),
    )
    sampling = importlib.import_module("mission.sampling")
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (False, "none"))

    session = swarm_cmd_mod._persist_orchestrator(
        directive="Supervise docs research.",
        criteria=_swarm_criteria(),
        budget={"max_iterations": 5, "max_wall_clock_seconds": 60},
        swarm_config=_swarm_config(),
        tool_allowlist=("find_docs",),
        allow_all_tools=False,
        stagnation_threshold=3,
        use_sampling=False,
    )
    assert backend.load_session(session["session_id"]) is not None
    assert "find_docs" in session["tool_allowlist"]

    with pytest.raises(SystemExit):
        swarm_cmd_mod._persist_orchestrator(
            directive="",
            criteria=_swarm_criteria(),
            budget={"max_iterations": 5, "max_wall_clock_seconds": 60},
            swarm_config=_swarm_config(),
            tool_allowlist=(),
            allow_all_tools=False,
            stagnation_threshold=3,
            use_sampling=False,
        )
    assert json.loads(capsys.readouterr().err)["details"]["field"] == "directive"


def test_swarm_scaffold_plan_sampling_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sampling = importlib.import_module("mission.sampling")
    scaffold = importlib.import_module("mission.swarm_scaffold")
    registered = {"find_docs": SimpleNamespace(description="Find docs")}
    tags = {"find_docs": {"safe"}}
    monkeypatch.setattr(
        swarm_cmd_mod, "_resolve_registered_tools_for_cli", lambda: (registered, tags)
    )
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (True, "bedrock"))

    sampled_plan = [_valid_plan_entry()]
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda _value: object())
    sampled = AsyncMock(return_value=sampled_plan)
    monkeypatch.setattr(scaffold, "generate_sampled_plan", sampled)
    deterministic = MagicMock(return_value=sampled_plan)
    monkeypatch.setattr(scaffold, "generate_deterministic_plan", deterministic)
    result = swarm_cmd_mod._scaffold_plan(
        directive="Find docs.",
        swarm_config=_swarm_config(),
        tool_allowlist=("find_docs",),
        allow_all_tools=False,
        max_children=1,
        use_sampling=True,
        retries=2,
    )
    assert result["sampling_path"] is True
    assert result["plan"] == sampled_plan
    assert sampled.await_args.kwargs["tool_allowlist"] == ["find_docs"]
    deterministic.assert_not_called()

    monkeypatch.setattr(
        scaffold,
        "generate_sampled_plan",
        AsyncMock(side_effect=scaffold.SwarmScaffoldError("invalid_spawn")),
    )
    fallback = swarm_cmd_mod._scaffold_plan(
        directive="Find docs.",
        swarm_config=_swarm_config(),
        tool_allowlist=("find_docs",),
        allow_all_tools=False,
        max_children=None,
        use_sampling=True,
        retries=1,
    )
    assert fallback["sampling_path"] is False
    assert fallback["fallback_reason"] == "invalid_spawn"
    assert "falling back" in capsys.readouterr().err

    monkeypatch.setattr(sampling, "select_sampling_backend", lambda _value: None)
    unavailable = swarm_cmd_mod._scaffold_plan(
        directive="Find docs.",
        swarm_config=_swarm_config(),
        tool_allowlist=("find_docs",),
        allow_all_tools=False,
        max_children=None,
        use_sampling=True,
        retries=1,
    )
    assert unavailable["sampling_path"] is False
    assert unavailable["fallback_reason"] == "sampling_backend_unavailable"


def test_swarm_scaffold_plan_maps_ftu_and_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sampling = importlib.import_module("mission.sampling")
    scaffold = importlib.import_module("mission.swarm_scaffold")
    monkeypatch.setattr(
        swarm_cmd_mod,
        "_resolve_registered_tools_for_cli",
        lambda: ({"find_docs": object()}, {"find_docs": {"safe"}}),
    )
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (True, "bedrock"))
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda _value: object())
    monkeypatch.setattr(
        scaffold,
        "generate_sampled_plan",
        AsyncMock(side_effect=BedrockFTUFormNotAcceptedError("FTU missing")),
    )
    with pytest.raises(SystemExit):
        swarm_cmd_mod._scaffold_plan(
            directive="Find docs.",
            swarm_config=_swarm_config(),
            tool_allowlist=("find_docs",),
            allow_all_tools=False,
            max_children=None,
            use_sampling=True,
            retries=1,
        )
    assert json.loads(capsys.readouterr().err)["code"] == "bedrock_ftu_form_not_accepted"

    with pytest.raises(SystemExit):
        swarm_cmd_mod._scaffold_plan(
            directive="Find docs.",
            swarm_config=_swarm_config(max_children=0),
            tool_allowlist=(),
            allow_all_tools=False,
            max_children=None,
            use_sampling=False,
            retries=1,
        )
    assert json.loads(capsys.readouterr().err)["details"]["field"] == "swarm"

    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (False, "none"))
    monkeypatch.setattr(
        scaffold,
        "generate_deterministic_plan",
        MagicMock(
            side_effect=MissionValidationError(
                "validation_error", details={"field": "plan", "reason": "unsafe"}
            )
        ),
    )
    with pytest.raises(SystemExit):
        swarm_cmd_mod._scaffold_plan(
            directive="Find docs.",
            swarm_config=_swarm_config(),
            tool_allowlist=(),
            allow_all_tools=False,
            max_children=None,
            use_sampling=False,
            retries=1,
        )
    assert json.loads(capsys.readouterr().err)["details"]["field"] == "plan"


async def test_swarm_prime_rejects_partial_fleet() -> None:
    runner = SimpleNamespace(
        spawn=AsyncMock(return_value={"code": "validation_error", "spawned": False}),
        run_to_completion=AsyncMock(),
    )
    with pytest.raises(SystemExit):
        await swarm_cmd_mod._prime_and_run(runner, [_valid_plan_entry()])
    runner.run_to_completion.assert_not_awaited()


@pytest.mark.parametrize("failure", ["busy", "validation"])
def test_swarm_drive_maps_runner_errors(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    runner_module = importlib.import_module("mission.swarm_runner")

    class Runner:
        async def spawn(self, _request: dict[str, Any]) -> dict[str, Any]:
            return {"spawned": True}

        async def run_to_completion(self, **_kwargs: Any) -> dict[str, Any]:
            if failure == "busy":
                raise runner_module.SwarmRunnerBusyError("mission-orch", 4321)
            raise MissionValidationError(
                "validation_error", details={"field": "session", "reason": "malformed"}
            )

    monkeypatch.setattr(swarm_cmd_mod, "_make_runner", lambda *_a, **_k: Runner())

    @click.command()
    def drive() -> None:
        swarm_cmd_mod._drive("mission-orch")

    result = CliRunner().invoke(drive)
    assert result.exit_code == 1
    envelope = json.loads(result.stderr)
    if failure == "busy":
        assert envelope == {
            "code": "swarm_runner_active",
            "details": {"session_id": "mission-orch", "holder_pid": 4321},
        }
    else:
        assert envelope["details"]["reason"] == "malformed"


def test_swarm_report_start_iterate_load_and_status_paths(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    @click.command()
    def report() -> None:
        swarm_cmd_mod._emit_report(
            {"session_id": "mission-orch", "status": "paused", "final_verdict": None}
        )

    summary = CliRunner().invoke(report)
    assert json.loads(summary.stdout) == {
        "session_id": "mission-orch",
        "status": "paused",
        "final_verdict": None,
    }

    bad = tmp_path / "bad-criteria.json"
    bad.write_text("not json", encoding="utf-8")
    invalid = CliRunner().invoke(
        swarm_cmd_mod.swarm_cmd,
        ["start", "--directive", "Supervise.", "--criteria-file", str(bad)],
    )
    assert invalid.exit_code == 1
    assert json.loads(invalid.stderr)["details"]["field"] == "criteria_file"

    good = tmp_path / "criteria.json"
    good.write_text(json.dumps(_swarm_criteria()), encoding="utf-8")
    original_read = Path.read_text

    def fail_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == good:
            raise OSError("permission denied")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    unreadable = CliRunner().invoke(
        swarm_cmd_mod.swarm_cmd,
        ["start", "--directive", "Supervise.", "--criteria-file", str(good)],
    )
    assert unreadable.exit_code == 1
    assert "permission denied" in unreadable.stderr
    monkeypatch.setattr(Path, "read_text", original_read)

    monkeypatch.setattr(
        swarm_cmd_mod,
        "_drive",
        MagicMock(
            return_value={
                "session_id": "mission-orch",
                "status": "running",
                "final_verdict": None,
                "iterations": [{"iteration_index": 1}],
            }
        ),
    )
    iterate = CliRunner().invoke(
        swarm_cmd_mod.swarm_cmd,
        ["iterate", "mission-orch", "--max-orchestrator-iterations", "1", "--dry-run"],
    )
    assert iterate.exit_code == 0
    assert json.loads(iterate.stdout)["iterations_run"] == 1

    missing = CliRunner().invoke(swarm_cmd_mod.swarm_cmd, ["status", "missing"])
    assert missing.exit_code == 1
    assert json.loads(missing.stderr)["code"] == "session_not_found"

    parent = {
        "version": SCHEMA_VERSION,
        "session_id": "mission-orch",
        "directive_text": "Supervise.",
        "criteria": _swarm_criteria(),
        "budget": {"max_iterations": 5, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["children_status", "mission_spawn", "child_abort"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2026-01-01T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
        "role": "orchestrator",
        "swarm": _swarm_config(),
        "children": [],
    }
    backend.save_session(parent)  # type: ignore[arg-type]
    rollup = {
        "session_id": "mission-orch",
        "status": "pending",
        "pool": {"remaining": 4, "pool": 12, "reserved": 8, "consumed": 0},
        "runner_state": None,
        "children": [
            {
                "slot": "worker-a",
                "status": "running",
                "final_verdict": None,
                "respawn_count": 1,
            }
        ],
        "findings": ["inspect worker-a"],
    }
    runner_module = importlib.import_module("mission.swarm_runner")
    monkeypatch.setattr(runner_module, "build_fleet_rollup", lambda *_args: rollup)
    table = CliRunner().invoke(
        swarm_cmd_mod.swarm_cmd, ["status", "mission-orch", "--output", "table"]
    )
    assert table.exit_code == 0
    assert "worker-a" in table.stdout
    assert "finding: inspect worker-a" in table.stdout


# ---------------------------------------------------------------------------
# Pure Swarm scaffold/rule and judge behavior
# ---------------------------------------------------------------------------


def test_swarm_plan_validation_and_deterministic_allow_all() -> None:
    scaffold = importlib.import_module("mission.swarm_scaffold")
    config = importlib.import_module("mission.swarm").validate_swarm_config(_swarm_config())
    kwargs = {
        "config": config,
        "registered_tools": {"find_docs": object()},
        "registered_tags": {"find_docs": {"safe"}},
    }
    with pytest.raises(MissionValidationError) as exc_info:
        scaffold.validate_plan([_valid_plan_entry(), "not-a-dict"], **kwargs)
    assert exc_info.value.details == {
        "field": "plan",
        "reason": "entry_not_a_dict",
        "plan_index": 1,
    }

    plan = scaffold.generate_deterministic_plan("Find docs.", allow_all_tools=True, **kwargs)
    assert plan[0]["tool_allowlist"] == ["find_docs"]


async def test_swarm_scaffold_parsing_ftu_and_lessons() -> None:
    scaffold = importlib.import_module("mission.swarm_scaffold")
    assert scaffold._parse_json_array("```json\n[]\n```") == []
    with pytest.raises(ValueError, match="expected a JSON array"):
        scaffold._parse_json_array('{"slot": "worker"}')

    class FTUBackend:
        async def sample(self, _prompt: Any) -> str:
            raise BedrockFTUFormNotAcceptedError("FTU required")

    config = importlib.import_module("mission.swarm").validate_swarm_config(_swarm_config())
    with pytest.raises(BedrockFTUFormNotAcceptedError, match="FTU required"):
        await scaffold.generate_sampled_plan(
            FTUBackend(),
            "Find docs.",
            config=config,
            registered_tools={"find_docs": object()},
            registered_tags={"find_docs": {"safe"}},
        )

    class CapturingBackend:
        def __init__(self) -> None:
            self.prompt = ""

        async def sample(self, prompt: Any) -> str:
            self.prompt = prompt.assemble()
            return "Narrow the query."

    backend = CapturingBackend()
    revised = await scaffold.sample_revised_directive(
        backend,
        {"directive_text": "Search everything."},  # type: ignore[arg-type]
        lessons=["", "Use a narrower region.", "   "],
    )
    assert revised == "Narrow the query."
    assert "Use a narrower region." in backend.prompt
    assert "\n- \n" not in backend.prompt


def test_respawn_overlap_check_skips_only_the_replaced_slot() -> None:
    swarm = importlib.import_module("mission.swarm")
    config = swarm.validate_swarm_config(_swarm_config())
    child = {
        "slot": "worker-a",
        "session_id": "mission-old",
        "spawned_at": "now",
        "reserved_iterations": 0,
        "restart_policy": "never",
        "max_respawns": 0,
        "respawn_count": 0,
        "consumed_iterations": 1,
        "settled": True,
    }
    request = _valid_plan_entry("worker-a")
    request["tool_allowlist"] = ["mutating_tool"]
    spec = swarm.validate_spawn(
        parent_role="orchestrator",
        config=config,
        children=[child],
        request=request,
        registered_tools={"mutating_tool": object(), "safe_tool": object()},
        registered_tags={"mutating_tool": {"low-risk"}, "safe_tool": {"safe"}},
        sibling_allowlists={
            "worker-a": ["mutating_tool"],
            "worker-b": ["safe_tool"],
            "worker-c": ["safe_tool"],
        },
        respawn_of_slot="worker-a",
    )
    assert spec["slot"] == "worker-a"
    assert spec["tool_allowlist"] == ["mutating_tool"]


def test_judge_degenerate_truncation_and_non_string_name() -> None:
    from mission_judge.prompt import truncate_context
    from mission_judge.shape import ErrorCode, JudgeError, validate_output_name

    assert truncate_context("abcdef", limit=3) == "def"
    assert truncate_context("abcdef", limit=0) == ""
    with pytest.raises(JudgeError) as exc_info:
        validate_output_name(None)  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.INVALID_OUTPUT_NAME
    assert exc_info.value.details == {"reason": "not_a_string", "supplied": None}


# ---------------------------------------------------------------------------
# Mission resource not-found compatibility paths
# ---------------------------------------------------------------------------


def test_resource_not_found_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    exceptions = importlib.import_module("fastmcp.exceptions")
    resource = importlib.import_module("resources.mission")

    with monkeypatch.context() as patch:
        patch.delattr(exceptions, "NotFoundError")
        exc = resource._make_not_found("resource missing")
        assert isinstance(exc, exceptions.ResourceError)

    with monkeypatch.context() as patch:
        patch.delattr(exceptions, "NotFoundError")
        patch.delattr(exceptions, "ResourceError")
        exc = resource._make_not_found("resource missing")
        assert isinstance(exc, KeyError)
        assert "resource missing" in str(exc)


def test_terminal_filesystem_resource_without_report_is_not_found(
    backend: FilesystemBackend,
) -> None:
    resource = importlib.import_module("resources.mission")
    session = _mission_session(backend, session_id="mission-no-report", status="completed")

    with pytest.raises(Exception) as exc_info:
        resource._session_report_resource(session["session_id"])

    assert "terminal but report not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Remaining baseline-only Mission output and sampling branches
# ---------------------------------------------------------------------------


def test_mission_resume_and_checkpoint_json_success(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = _mission_session(backend, session_id="mission-resume-json", status="paused")
    runner = CliRunner()
    resumed = runner.invoke(mission_cmd_mod.mission_cmd, ["resume", paused["session_id"]])
    assert resumed.exit_code == 0
    assert json.loads(resumed.stdout) == {
        "session_id": paused["session_id"],
        "status": "running",
    }

    iteration = {"iteration_index": 4}
    checkpointed = _mission_session(
        backend,
        session_id="mission-checkpoint-json",
        iterations=[iteration],
    )
    decide = importlib.import_module("mission.decide")
    monkeypatch.setattr(decide, "decide_verdict", lambda *_args: ("continue", "more_work"))
    checkpoint = runner.invoke(
        mission_cmd_mod.mission_cmd, ["checkpoint", checkpointed["session_id"]]
    )
    assert checkpoint.exit_code == 0
    assert json.loads(checkpoint.stdout) == {
        "session_id": checkpointed["session_id"],
        "iteration_index": 4,
        "verdict": "continue",
        "verdict_reason": "more_work",
    }


def test_mission_sampling_enabled_without_backend_falls_back_deterministically(
    backend: FilesystemBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampling = importlib.import_module("mission.sampling")
    scaffold = importlib.import_module("mission.criteria_scaffold")
    criteria = [
        {
            "criterion_id": "fallback",
            "kind": "predicate",
            "required": True,
            "expression": "True",
        }
    ]
    monkeypatch.setattr(sampling, "resolve_sampling_state", lambda _value: (True, "bedrock"))
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda **_kwargs: None)
    deterministic = MagicMock(return_value=criteria)
    monkeypatch.setattr(scaffold, "generate_deterministic_criteria", deterministic)

    scaffold_result = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        ["scaffold-criteria", "--directive", "Fallback safely.", "--use-sampling"],
    )
    assert scaffold_result.exit_code == 0
    assert json.loads(scaffold_result.stdout) == criteria

    driven: list[str] = []
    monkeypatch.setattr(
        mission_cmd_mod,
        "_run_to_completion",
        lambda session_id, **_kwargs: driven.append(session_id),
    )
    run_result = CliRunner().invoke(
        mission_cmd_mod.mission_cmd,
        [
            "run",
            "--directive",
            "Fallback safely.",
            "--tool-allowlist",
            "find_docs",
            "--use-sampling",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    assert len(driven) == 1
    persisted = backend.load_session(driven[0])
    assert persisted is not None
    assert persisted["criteria"][0]["criterion_id"] == "fallback"
    assert json.loads(run_result.stderr)["sampling_path"] is False
    assert deterministic.call_count == 2
