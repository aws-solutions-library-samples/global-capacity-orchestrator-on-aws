"""Targeted baseline-gap coverage for Mission runtime orchestration."""

from __future__ import annotations

import builtins
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GCO_MCP_ROOT = str(PROJECT_ROOT / "gco_mcp")
if GCO_MCP_ROOT not in sys.path:
    sys.path.insert(0, GCO_MCP_ROOT)

from mission import SCHEMA_VERSION, _environment, sampling  # noqa: E402
from mission import _engine_factory as engine_factory  # noqa: E402
from mission.decide import (  # noqa: E402
    _strategy_unproductive,
    _wall_clock_exceeded,
    decide_verdict,
)
from mission.engine import MissionEngine, MissionEngineError  # noqa: E402
from mission.sampling import (  # noqa: E402
    SamplingFallback,
    SamplingPrompt,
    SamplingTransportError,
    SamplingUsed,
)

from gco import bedrock as bedrock_module  # noqa: E402

_ORIGINAL_BUILD_MEMORY_STORE = engine_factory._build_memory_store


async def _dispatcher(_name: str, _args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
    return {}


def _observation(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "tool_results": [],
        "metrics": {},
        "events": [],
        "phase_started_at": "2025-01-01T00:00:00+00:00",
        "phase_ended_at": "2025-01-01T00:00:01+00:00",
    }
    value.update(overrides)
    return value


def _iteration(
    index: int = 0,
    *,
    strategy: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    criteria_evaluation: list[Any] | None = None,
    verdict: str = "continue",
) -> dict[str, Any]:
    return {
        "iteration_index": index,
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:01+00:00",
        "phases": [],
        "strategy": strategy or {"tool_calls": [{"tool_name": "tool", "args": {}}]},
        "observation": observation or _observation(),
        "criteria_evaluation": criteria_evaluation or [],
        "verdict": verdict,
        "verdict_reason": "in_progress",
        "checkpoint_evaluated": True,
    }


def _session(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": "runtime-coverage",
        "directive_text": "Find GPU capacity.",
        "criteria": [
            {
                "criterion_id": "loss",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["tool"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2025-01-01T00:00:00+00:00",
        "started_at": "2025-01-01T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
    }
    value.update(overrides)
    return value


def _engine(**overrides: Any) -> MissionEngine:
    params: dict[str, Any] = {
        "backend": object(),
        "tool_dispatcher": _dispatcher,
        "sampling_callable": None,
        "sandbox_runner": None,
    }
    params.update(overrides)
    return MissionEngine(**params)


def _prompt(*, iterations: list[dict[str, Any]] | None = None) -> SamplingPrompt:
    return SamplingPrompt(
        directive="Find GPU capacity.",
        success_criteria=[],
        criteria_status=[],
        recent_iterations=iterations or [],
        tool_allowlist=["tool"],
        tool_docstrings={"tool": "Run a tool."},
        remaining_iterations=2,
        remaining_wall_clock_secs=30.0,
        allow_scripts=False,
    )


@pytest.mark.asyncio
async def test_engine_missing_session_and_sampling_guards() -> None:
    class MissingBackend:
        def load_session(self, _session_id: str) -> None:
            return None

    engine = _engine(backend=MissingBackend())
    with pytest.raises(MissionEngineError) as exc_info:
        await engine.run_iteration("missing")
    assert exc_info.value.code == "session_not_found"

    async def sampler(**_kwargs: Any) -> None:
        return None

    engine.sampling_callable = sampler
    assert engine._should_attempt_sampling(_session(use_sampling=False)) is False
    assert engine._should_attempt_sampling(_session(use_sampling=True, iterations=[])) is False


@pytest.mark.asyncio
async def test_engine_rejects_malformed_sampling_results_and_empty_rationale() -> None:
    record: dict[str, Any] = {}

    async def malformed_used(**_kwargs: Any) -> SamplingUsed:
        return SamplingUsed(
            output_text="{}",
            parsed={"next_strategy": [], "revision_rationale": "ignored"},
            backend_name="bedrock",
            model_id="model",
        )

    engine = _engine(sampling_callable=malformed_used)
    assert await engine._try_sample_strategy(_session(), None, record) is None
    assert record == {}

    async def malformed_legacy(**_kwargs: Any) -> str:
        return "not-a-strategy"

    engine.sampling_callable = malformed_legacy
    assert await engine._try_sample_strategy(_session(), None, record) is None

    engine._capture_sampled_rationale(record, {})
    engine._capture_sampled_rationale(record, {"revision_rationale": 7})
    assert record == {}


@pytest.mark.asyncio
async def test_engine_accepts_sampled_script_and_scans_all_call_log_shapes() -> None:
    async def runner(
        _script: str, _ctx: Any, _dispatch: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return _observation(), []

    engine = _engine(sandbox_runner=runner)
    assert engine._coerce_strategy_dict({"script": "x = 1"}) == {"script": "x = 1"}

    unchanged: dict[str, Any] = {}
    widened = engine._widen_args(
        unchanged,
        _session(
            iterations=[_iteration(criteria_evaluation=[{"criterion_id": "", "status": "unmet"}])]
        ),
    )
    assert widened is unchanged

    from_script = {
        "iterations": [
            {
                "script_call_log": [
                    {"status": "ok", "tool_name": "script-tool", "args": {"x": 1}},
                    "malformed",
                ],
                "strategy": {},
            }
        ]
    }
    assert engine._find_most_recent_successful_call(from_script) == (
        "script-tool",
        {"x": 1},
    )

    from_direct = {
        "iterations": [
            {
                "script_call_log": [{"status": "failed"}],
                "strategy": {
                    "tool_calls": [
                        {"status": "ok", "tool_name": "direct-tool", "args": {}},
                        42,
                    ]
                },
            }
        ]
    }
    assert engine._find_most_recent_successful_call(from_direct) == ("direct-tool", {})

    exhausted = {
        "iterations": [
            {"script_call_log": [None], "strategy": {"tool_calls": [None]}},
            {"strategy": {"tool_calls": []}},
        ]
    }
    assert engine._find_most_recent_successful_call(exhausted) is None


@pytest.mark.asyncio
async def test_engine_preserves_runner_error_when_sandbox_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_error = RuntimeError("runner exploded")

    async def runner(_script: str, _ctx: Any, _dispatch: Any) -> Any:
        raise original_error

    engine = _engine(sandbox_runner=runner)
    real_import = builtins.__import__

    def blocked_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name in {"sandbox", "mission.sandbox"} or name.endswith(".sandbox"):
            raise ImportError("sandbox unavailable")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(RuntimeError) as exc_info:
        await engine._execute_script(_session(), {"script": "pass"}, None, {})
    assert exc_info.value is original_error


@pytest.mark.asyncio
async def test_engine_reraises_unknown_runner_exception() -> None:
    original_error = RuntimeError("runner exploded")

    async def runner(_script: str, _ctx: Any, _dispatch: Any) -> Any:
        raise original_error

    engine = _engine(sandbox_runner=runner)
    with pytest.raises(RuntimeError) as exc_info:
        await engine._execute_script(_session(), {"script": "pass"}, None, {})
    assert exc_info.value is original_error


def test_engine_defensive_observation_and_cumulative_shapes() -> None:
    engine = _engine(
        observation_augmenters=[
            lambda _session: {"children": "bad", "metrics": "bad"},
            lambda _session: {"children": [{"id": "child"}], "metrics": {"gpu": 1}},
        ]
    )
    record: dict[str, Any] = {"observation": _observation(metrics="not-a-dict")}
    engine._apply_observation_augmenters(_session(), record)
    assert record["observation"]["children"] == [{"id": "child"}]
    assert record["observation"]["metrics"] == "not-a-dict"

    cumulative = engine._build_cumulative_observation(
        {"tool_results": "bad", "metrics": "bad"},
        _session(
            iterations=[_iteration(observation=_observation(tool_results="bad", metrics="bad"))]
        ),
    )
    assert cumulative["tool_results"] == []
    assert cumulative["metric_history"] == {}


def test_engine_criterion_dispatch_and_defensive_evaluators() -> None:
    engine = _engine()
    event_result = engine._evaluate_one_criterion(
        {"criterion_id": "event", "kind": "event", "event_name": "ready"},
        _observation(events=[{"event_name": "ready"}]),
        {},
        _session(),
    )
    assert event_result["status"] == "met"

    unknown = engine._evaluate_one_criterion(
        {"criterion_id": "unknown", "kind": "future-kind"},
        _observation(),
        {},
        _session(),
    )
    assert unknown["status"] == "inconclusive"
    assert "unknown_criterion_kind" in str(unknown["evidence"])

    status, evidence = engine._evaluate_metric_threshold(
        {"metric": "metrics.loss", "op": "?", "target": 1},
        _observation(metrics={"loss": 0.5}),
    )
    assert (status, evidence) == ("inconclusive", 0.5)

    status, evidence = engine._evaluate_metric_trend(
        {"metric": "loss", "direction": "non_decreasing"},
        {"metric_history": {"loss": [1.0, 1.0]}},
    )
    assert status == "met"
    assert evidence["delta"] == 0.0

    status, evidence = engine._evaluate_metric_trend(
        {"metric": "loss", "direction": "sideways"},
        {"metric_history": {"loss": [1.0, 2.0]}},
    )
    assert status == "inconclusive"
    assert evidence == "unknown_direction:'sideways'"

    status, evidence = engine._evaluate_tool_call_succeeded(
        {"tool_name": "tool", "min_count": 2},
        _observation(tool_results=[None, {"tool_name": "tool", "_status": "ok"}]),
        _session(
            iterations=[
                _iteration(
                    observation=_observation(
                        tool_results=["bad", {"tool_name": "tool", "_status": "ok"}]
                    )
                )
            ]
        ),
    )
    assert status == "met"
    assert evidence["successful_call_count"] == 2

    assert (
        engine._criteria_improved(
            [{"criterion_id": "c", "status": "unmet"}],
            [None, {"criterion_id": "c", "status": "met"}],
        )
        is True
    )


@pytest.mark.asyncio
async def test_engine_final_lessons_partial_overlays_and_sampling_disabled() -> None:
    async def never_called(**_kwargs: Any) -> Any:
        raise AssertionError("sampling must be disabled")

    engine = _engine(final_lessons_callable=never_called)
    assert (
        await engine._maybe_sample_final_lessons(_session(use_sampling=False), "complete", "x")
        is None
    )

    results = iter(
        [
            SamplingUsed(
                output_text="{}",
                parsed={"lessons": ["one"], "recommended_followups": "bad"},
                backend_name="bedrock",
                model_id="m",
            ),
            SamplingUsed(
                output_text="{}",
                parsed={"lessons": [1], "recommended_followups": ["next"]},
                backend_name="bedrock",
                model_id="m",
            ),
            SamplingUsed(
                output_text="{}",
                parsed={"lessons": [], "recommended_followups": [1]},
                backend_name="bedrock",
                model_id="m",
            ),
        ]
    )

    async def sampled(**_kwargs: Any) -> SamplingUsed:
        return next(results)

    engine.final_lessons_callable = sampled
    enabled = _session(use_sampling=True)
    assert await engine._maybe_sample_final_lessons(enabled, "complete", "x") == {"lessons": "one"}
    assert await engine._maybe_sample_final_lessons(enabled, "complete", "x") == {
        "recommended_followups": ["next"]
    }
    assert await engine._maybe_sample_final_lessons(enabled, "complete", "x") == {"lessons": ""}


def test_engine_memory_write_ignores_malformed_overlay() -> None:
    class Store:
        def __init__(self) -> None:
            self.args: tuple[Any, ...] | None = None

        def write_memory(self, *args: Any) -> None:
            self.args = args

    store = Store()
    engine = _engine(memory_store=store)
    session = _session(iterations=[])
    engine._maybe_write_memory(
        session,
        "terminate",
        "no_progress",
        {"lessons": ["not", "a string"], "recommended_followups": "bad"},
    )
    assert store.args is not None
    assert isinstance(store.args[3], str)
    assert isinstance(store.args[4], list)


def test_decide_deferred_stagnation_wall_clock_and_unproductive_false_paths() -> None:
    now = datetime(2025, 1, 1, 0, 0, 10, tzinfo=UTC)
    current = _iteration(
        criteria_evaluation=[{"criterion_id": "loss", "status": "unmet"}],
        observation=_observation(),
    )
    session = _session(use_sampling=True, no_progress_counter=3, stagnation_threshold=3)
    assert decide_verdict(session, current, now) == ("terminate", "no_progress")

    finite = _session(
        started_at=(now - timedelta(seconds=5)).isoformat(),
        budget={"max_iterations": 10, "max_wall_clock_seconds": 10},
    )
    assert _wall_clock_exceeded(finite, now) is False
    finite["started_at"] = (now - timedelta(seconds=10)).isoformat()
    assert _wall_clock_exceeded(finite, now) is True

    short_history = _session(no_progress_counter=2, stagnation_threshold=3, iterations=[])
    assert _strategy_unproductive(short_history, current) == (False, "")

    differing = _session(
        no_progress_counter=2,
        stagnation_threshold=3,
        iterations=[
            _iteration(0, strategy={"tool_calls": [{"tool_name": "a"}]}),
            _iteration(1, strategy={"tool_calls": [{"tool_name": "b"}]}),
        ],
    )
    assert _strategy_unproductive(differing, current) == (False, "")

    repeated_error = {"message": "same"}
    errors = _session(
        no_progress_counter=0,
        iterations=[_iteration(observation=_observation(errors=[repeated_error]))],
    )
    current["observation"] = _observation(errors=[repeated_error, repeated_error])
    assert _strategy_unproductive(errors, current) == (False, "")


@pytest.mark.asyncio
async def test_factory_time_import_error_and_result_error_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    remaining = engine_factory.remaining_wall_clock_seconds(
        {"budget": {"max_wall_clock_seconds": 10}, "started_at": started}
    )
    assert remaining is not None and 0.0 <= remaining < 10.0

    real_import = builtins.__import__

    def blocked_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "server":
            raise ImportError("server unavailable")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert await engine_factory.fetch_registered_tool_metadata() == ({}, {})

    assert "no error details" in str(engine_factory.MissionToolResultError("tool", None))
    assert "plain failure" in str(engine_factory.MissionToolResultError("tool", "plain failure"))

    result = SimpleNamespace(
        structured_content=None,
        content=[SimpleNamespace(text=123)],
    )
    assert engine_factory._tool_result_payload(result) is None


@pytest.mark.asyncio
async def test_live_dispatch_uses_active_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastmcp.server.dependencies as dependencies
    import server as server_module

    context_calls: list[str] = []
    monkeypatch.setattr(
        dependencies,
        "get_context",
        lambda: context_calls.append("active") or object(),
    )

    class Tool:
        async def run(self, args: dict[str, Any]) -> Any:
            assert args == {"x": 1}
            return SimpleNamespace(
                structured_content={"ok": True},
                content=[],
                is_error=False,
            )

    class MCP:
        async def get_tool(self, name: str) -> Any:
            assert name == "tool"
            return Tool()

    monkeypatch.setattr(server_module, "mcp", MCP())
    assert await engine_factory._live_dispatch_tool("tool", {"x": 1}, None) == {"ok": True}
    assert context_calls == ["active"]


@pytest.mark.asyncio
async def test_factory_sandbox_import_fallback_and_runner_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted_session = _session(
        allow_scripted_strategies=True,
        tool_allowlist=["tool"],
    )
    real_import = builtins.__import__

    def blocked_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "mission.sandbox":
            raise ImportError("sandbox unavailable")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert engine_factory._build_sandbox_runner(scripted_session) is None
    monkeypatch.setattr(builtins, "__import__", real_import)

    import mission.sandbox as sandbox_module

    class FakeSandbox:
        def __init__(self, allowlist: list[str], session: dict[str, Any]) -> None:
            assert allowlist == ["tool"]
            assert session["session_id"] == "runtime-coverage"

        async def run(self, script: str, ctx: Any, dispatcher: Any) -> Any:
            assert (script, ctx, dispatcher) == ("pass", "ctx", _dispatcher)
            return {"metrics": {"ok": 1}}, [{"tool_name": "tool"}]

    monkeypatch.setattr(sandbox_module, "MissionSandbox", FakeSandbox)
    runner = engine_factory._build_sandbox_runner(scripted_session)
    assert runner is not None
    assert await runner("pass", "ctx", _dispatcher) == (
        {"metrics": {"ok": 1}},
        [{"tool_name": "tool"}],
    )


def test_factory_memory_store_original_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mission.memory as memory_module

    sentinel = object()
    monkeypatch.setattr(memory_module, "MissionMemoryStore", lambda: sentinel)
    assert _ORIGINAL_BUILD_MEMORY_STORE() is sentinel

    def fail() -> Any:
        raise RuntimeError("constructor failed")

    monkeypatch.setattr(memory_module, "MissionMemoryStore", fail)
    assert _ORIGINAL_BUILD_MEMORY_STORE() is None


@pytest.mark.asyncio
async def test_factory_final_lessons_closure_matches_engine_call_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        engine_factory._build_final_lessons_callable(
            _session(use_sampling=True, sampling_backend_resolved="none"),
            None,
            {},
        )
        is None
    )

    backend = object()
    monkeypatch.setattr(sampling, "select_sampling_backend", lambda model_id: backend)
    captured: dict[str, Any] = {}

    async def fake_final(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "sampled"

    monkeypatch.setattr(sampling, "maybe_sample_final_lessons", fake_final)
    session = _session(use_sampling=True, sampling_backend_resolved="bedrock")
    callback = engine_factory._build_final_lessons_callable(session, object(), {"tool": "doc"})
    assert callback is not None
    assert await callback(session=session) == "sampled"
    assert captured == {
        "backend": backend,
        "session": session,
        "tool_docstrings": {"tool": "doc"},
    }


@pytest.mark.asyncio
async def test_factory_merges_extra_tool_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    base_tool = object()
    extra_tool = object()

    async def metadata() -> tuple[dict[str, Any], dict[str, str]]:
        return {"same": base_tool}, {"same": "base"}

    captured: dict[str, Any] = {}

    def sampling_builder(
        _session: Any,
        _ctx: Any,
        *,
        registered_tools: dict[str, Any],
        tool_docstrings: dict[str, str],
    ) -> str:
        captured["tools"] = registered_tools
        captured["docs"] = tool_docstrings
        return "sampler"

    monkeypatch.setattr(engine_factory, "fetch_registered_tool_metadata", metadata)
    monkeypatch.setattr(engine_factory, "_build_sampling_callable", sampling_builder)
    monkeypatch.setattr(engine_factory, "_build_sandbox_runner", lambda _session: None)
    monkeypatch.setattr(
        engine_factory,
        "_build_final_lessons_callable",
        lambda _session, _ctx, _docs: None,
    )
    monkeypatch.setattr(engine_factory, "_build_memory_store", lambda: None)

    deps = await engine_factory.build_engine_dependencies(
        _session(),
        None,
        extra_tool_metadata=(
            {"same": extra_tool, "extra": extra_tool},
            {"same": "override", "extra": "extra doc"},
        ),
    )
    assert deps.sampling_callable == "sampler"
    assert captured["tools"] == {"same": extra_tool, "extra": extra_tool}
    assert captured["docs"] == {"same": "override", "extra": "extra doc"}


def test_environment_checker_and_reservation_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.capacity.checker as checker_module
    import cli.capacity.multi_region as multi_region_module

    checker = object()
    monkeypatch.setattr(
        multi_region_module,
        "get_multi_region_capacity_checker",
        lambda: checker,
    )
    assert _environment._safe_get_checker() is checker

    def fail_checker() -> Any:
        raise RuntimeError("offline")

    monkeypatch.setattr(
        multi_region_module,
        "get_multi_region_capacity_checker",
        fail_checker,
    )
    assert _environment._safe_get_checker() is None

    outer = SimpleNamespace(config=object())

    class FailingConstructor:
        def __init__(self, _config: Any) -> None:
            raise RuntimeError("no config")

    monkeypatch.setattr(checker_module, "CapacityChecker", FailingConstructor)
    assert _environment._summarise_reservations(outer, ["a"]) == {
        "active_count": 0,
        "by_region": {},
        "_error": "reservation_probe_failed",
    }

    class CapacityChecker:
        def __init__(self, _config: Any) -> None:
            pass

        def list_capacity_reservations(self, region: str, *, state: str) -> list[Any]:
            assert state == "active"
            if region == "bad":
                raise RuntimeError("region unavailable")
            return [1, 2]

    monkeypatch.setattr(checker_module, "CapacityChecker", CapacityChecker)
    assert _environment._summarise_reservations(outer, ["good", "bad"]) == {
        "active_count": 2,
        "by_region": {"good": 2, "bad": 0},
    }


def test_sampling_summary_and_prompt_drop_residuals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sampling, "ENVIRONMENT_CONTEXT_BYTE_CAP", 0)
    assert sampling._summarise_environment_context({}) == {}

    assert sampling._summarise_observation(
        {"_original_bytes": {"old": 1}, "metrics": {"loss": 1}}
    ) == {"metrics": {"loss": 1}}

    criteria = [{"criterion_id": "c", "kind": "event", "required": True}]
    paired = sampling._pair_criteria_with_status(
        criteria,
        [{"status": "met"}, {"criterion_id": "c", "status": "unmet"}],
    )
    assert paired[0]["status"]["status"] == "unmet"

    rendered = sampling._render_tool_allowlist(["tool"], {}, {"tool": None})
    assert rendered == [{"tool_name": "tool", "docstring": ""}]

    monkeypatch.setattr(sampling, "PROMPT_BYTE_BUDGET", 1)
    text = _prompt(iterations=[_iteration(98), _iteration(99)]).assemble_final_lessons()
    assert '"iteration_index":98' not in text.replace(" ", "")
    assert '"iteration_index":99' not in text.replace(" ", "")


@pytest.mark.asyncio
async def test_sampling_protocol_default_and_bedrock_malformed_type_path() -> None:
    assert await sampling.SamplingBackend.sample(object(), _prompt()) is None

    class Client:
        def converse(self, **_kwargs: Any) -> dict[str, Any]:
            return {"output": {"message": {"content": {}}}}

    backend = sampling.BedrockSamplingBackend(model_id="explicit", region="us-east-1")
    backend._client = Client()
    with pytest.raises(SamplingTransportError) as exc_info:
        await backend.sample(_prompt())
    assert exc_info.value.code == "bedrock_malformed_response"
    assert isinstance(exc_info.value.__cause__, TypeError)


def test_sampling_catalog_schema_and_validation_defences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoneSchema:
        input_schema = None

    class RaisingSchema:
        @classmethod
        def model_json_schema(cls) -> Any:
            raise RuntimeError("bad schema")

    class RaisingTool:
        input_schema = RaisingSchema

    assert (
        sampling._extract_tool_json_schemas(
            ["missing", "none", "raising"],
            {"none": NoneSchema(), "raising": RaisingTool()},
        )
        == {}
    )

    monkeypatch.setattr(sampling._validation, "validate_strategy", lambda *_args: None)
    with pytest.raises(sampling.MissionValidationError) as exc_info:
        sampling.validate_strategy_against_catalog(
            {"tool_calls": []},
            ["tool"],
            {},
            False,
        )
    assert exc_info.value.details["reason"] == "tool_calls_empty"

    monkeypatch.undo()
    with pytest.raises(sampling.MissionValidationError) as exc_info:
        sampling.validate_strategy_against_catalog(
            {"tool_calls": [{"tool_name": "tool", "args": {}}]},
            ["tool"],
            {},
            False,
        )
    assert exc_info.value.details["reason"] == "tool_not_registered"

    class ValidatingTool:
        input_schema: Any

        def __init__(self, schema: Any) -> None:
            self.input_schema = schema

    class AcceptSchema:
        @classmethod
        def model_validate(cls, _args: Any) -> None:
            return None

    with pytest.raises(sampling.MissionValidationError) as exc_info:
        sampling.validate_strategy_against_catalog(
            {"tool_calls": [{"tool_name": "tool", "args": "bad"}]},
            ["tool"],
            {"tool": ValidatingTool(AcceptSchema)},
            False,
        )
    assert exc_info.value.details["errors"][0]["type"] == "args_not_a_dict"

    class HostileError(Exception):
        def errors(self) -> Any:
            raise RuntimeError("errors unavailable")

    class HostileSchema:
        @classmethod
        def model_validate(cls, _args: Any) -> None:
            raise HostileError("invalid")

    with pytest.raises(sampling.MissionValidationError) as exc_info:
        sampling.validate_strategy_against_catalog(
            {"tool_calls": [{"tool_name": "tool", "args": {}}]},
            ["tool"],
            {"tool": ValidatingTool(HostileSchema)},
            False,
        )
    assert exc_info.value.details["errors"] == [{"type": "unknown", "msg": "invalid"}]

    class PlainSchema:
        @classmethod
        def model_validate(cls, _args: Any) -> None:
            raise RuntimeError("plain invalid")

    with pytest.raises(sampling.MissionValidationError) as exc_info:
        sampling.validate_strategy_against_catalog(
            {"tool_calls": [{"tool_name": "tool", "args": {}}]},
            ["tool"],
            {"tool": ValidatingTool(PlainSchema)},
            False,
        )
    assert exc_info.value.details["errors"] == [{"type": "unknown", "msg": "plain invalid"}]


def test_sampling_json_and_schema_trust_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sampling.json, "loads", lambda _candidate: [])
    with pytest.raises(json.JSONDecodeError, match="top-level"):
        sampling._extract_json_object("{}")
    monkeypatch.undo()

    bad_revisions = [
        {"revision_rationale": "", "next_strategy": {}, "confidence": 0.5},
        {"revision_rationale": "x", "next_strategy": [], "confidence": 0.5},
        {"revision_rationale": "x", "next_strategy": {}, "confidence": True},
        {"revision_rationale": "x", "next_strategy": {}, "confidence": 2.0},
    ]
    for payload in bad_revisions:
        with pytest.raises(ValueError, match="schema_mismatch"):
            sampling._validate_revision_schema(payload)

    bad_lessons = [
        {"lessons": [], "recommended_followups": []},
        {"lessons": [1], "recommended_followups": []},
        {"lessons": ["ok"], "recommended_followups": "bad"},
        {"lessons": ["ok"], "recommended_followups": [1]},
    ]
    for payload in bad_lessons:
        with pytest.raises(ValueError, match="schema_mismatch"):
            sampling._validate_lessons_schema(payload)


class _LessonsBackend:
    backend_name = "bedrock"
    model_id = "model"

    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.rendered: str | None = None

    async def sample(self, prompt: Any) -> str:
        self.rendered = prompt.assemble()
        if self.error is not None:
            raise self.error
        return self.output


@pytest.mark.asyncio
async def test_sampling_final_lessons_fallbacks_and_prerendered_forwarding() -> None:
    session = _session(iterations=[_iteration()])
    transport = _LessonsBackend(error=SamplingTransportError("bedrock_timeout"))
    result = await sampling.maybe_sample_final_lessons(backend=transport, session=session)
    assert isinstance(result, SamplingFallback)
    assert result.reason == "transport_error"
    assert transport.rendered is not None

    malformed = _LessonsBackend(output="not json")
    result = await sampling.maybe_sample_final_lessons(backend=malformed, session=session)
    assert isinstance(result, SamplingFallback)
    assert result.reason == "json_parse"

    wrong_schema = _LessonsBackend(output='{"lessons": []}')
    result = await sampling.maybe_sample_final_lessons(backend=wrong_schema, session=session)
    assert isinstance(result, SamplingFallback)
    assert result.reason == "schema_mismatch"

    success = _LessonsBackend(output="exact")
    assert await sampling._sample_with_assembled_text(success, "exact prompt") == "exact"
    assert success.rendered == "exact prompt"


def test_bedrock_configuration_reasoning_ftu_and_text_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(bedrock_module.BedrockModelConfigurationError, match="non-empty string"):
        bedrock_module._embedding_model_id_from_payload(
            {"context": {"bedrock": {"embedding_model_id": " "}}},
            tmp_path / "cdk.json",
        )

    nova = bedrock_module._nova_reasoning_options(
        {"temperature": 0.2, "maxTokens": 10},
        "medium",
    )
    assert nova["inferenceConfig"] == {"temperature": 0.2, "maxTokens": 10}
    assert nova["additionalModelRequestFields"]["reasoningConfig"]["maxReasoningEffort"] == "medium"

    configuration = SimpleNamespace(
        mission_model_id="amazon.nova-2-lite-v1:0",
        capacity_advisor_model_id="amazon.nova-2-pro-v1:0",
        thinking_effort="high",
    )
    monkeypatch.setattr(
        bedrock_module,
        "get_default_bedrock_configuration",
        lambda _path=None: configuration,
    )
    with pytest.raises(
        bedrock_module.BedrockModelConfigurationError,
        match="changed while building",
    ):
        bedrock_module.build_bedrock_converse_options(
            "amazon.nova-2-sonic-v1:0",
            apply_default_reasoning=True,
        )

    assert (
        bedrock_module.is_bedrock_ftu_form_error(
            RuntimeError(f"request failed: {bedrock_module.BEDROCK_FTU_FORM_ERROR_CODE}")
        )
        is True
    )
    assert str(bedrock_module.BedrockResponseTruncatedError("custom")) == "custom"

    with pytest.raises(TypeError, match="must be a list"):
        bedrock_module.extract_bedrock_converse_text(
            {"output": {"message": {"content": {"text": "bad"}}}}
        )
    with pytest.raises(IndexError, match="no non-empty text"):
        bedrock_module.extract_bedrock_converse_text(
            {"output": {"message": {"content": [1, {"text": " "}, {"text": 3}]}}}
        )


@pytest.mark.asyncio
async def test_engine_successful_script_and_remaining_defensive_branches() -> None:
    async def runner(_script: str, _ctx: Any, _dispatch: Any) -> Any:
        return _observation(metrics={"ok": 1}), [{"tool_name": "tool", "status": "ok"}]

    engine = _engine(sandbox_runner=runner)
    record: dict[str, Any] = {}
    calls = await engine._execute_script(_session(), {"script": "pass"}, None, record)
    assert calls == [{"tool_name": "tool", "status": "ok"}]
    assert record["observation"]["metrics"] == {"ok": 1}

    cumulative = engine._build_cumulative_observation(
        {"tool_results": ["current"], "metrics": {"loss": 1.0, "flag": True}},
        _session(
            iterations=[
                _iteration(
                    observation=_observation(
                        tool_results=["prior"],
                        metrics={"loss": 2.0, "label": "skip"},
                    )
                )
            ]
        ),
    )
    assert cumulative["tool_results"] == ["prior", "current"]
    assert cumulative["metric_history"] == {"loss": [2.0, 1.0]}
    assert engine._evaluate_metric_threshold(
        {"metric": "metrics.loss", "op": "<", "target": 2.0},
        _observation(metrics={"loss": 1.0}),
    ) == ("met", 1.0)


@pytest.mark.asyncio
async def test_engine_final_lessons_fallback_raw_and_unknown_returns() -> None:
    returns = iter(
        [
            SamplingFallback("fallback", "bad", "bedrock", "m"),
            {"lessons": "legacy"},
            42,
        ]
    )

    async def callback(**_kwargs: Any) -> Any:
        return next(returns)

    engine = _engine(final_lessons_callable=callback)
    session = _session(use_sampling=True)
    assert await engine._maybe_sample_final_lessons(session, "terminate", "x") is None
    assert await engine._maybe_sample_final_lessons(session, "terminate", "x") == {
        "lessons": "legacy"
    }
    assert await engine._maybe_sample_final_lessons(session, "terminate", "x") is None


@pytest.mark.asyncio
async def test_remaining_sampling_bedrock_budget_and_context_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TruncatedClient:
        def converse(self, **_kwargs: Any) -> dict[str, Any]:
            return {"stopReason": "max_tokens"}

    backend = sampling.BedrockSamplingBackend(model_id="explicit", region="us-east-1")
    backend._client = TruncatedClient()
    with pytest.raises(SamplingTransportError) as truncated:
        await backend.sample(_prompt())
    assert truncated.value.code == "bedrock_truncated_response"
    assert isinstance(truncated.value.__cause__, bedrock_module.BedrockResponseTruncatedError)

    assert bedrock_module.build_bedrock_converse_options(
        "cohere.command-r-v1:0",
        inference_config={"temperature": 0.2},
    ) == {"inferenceConfig": {"temperature": 0.2}}
    assert bedrock_module.build_bedrock_converse_options(
        "amazon.nova-2-sonic-v1:0",
        inference_config={"temperature": 0.2},
    ) == {"inferenceConfig": {"temperature": 0.2}}

    now = datetime(2025, 1, 1, tzinfo=UTC)
    assert _wall_clock_exceeded(_session(started_at=None), now) is False

    import fastmcp.server.dependencies as dependencies
    import server as server_module

    class Tool:
        async def run(self, _args: dict[str, Any]) -> Any:
            return SimpleNamespace(
                structured_content={"ok": True},
                content=[],
                is_error=False,
            )

    class MCP:
        async def get_tool(self, _name: str) -> Tool:
            return Tool()

    monkeypatch.setattr(server_module, "mcp", MCP())
    monkeypatch.setattr(
        dependencies,
        "get_context",
        lambda: (_ for _ in ()).throw(RuntimeError("no active context")),
    )
    assert await engine_factory._live_dispatch_tool("tool", {}, object()) == {"ok": True}

    real_import = builtins.__import__

    def blocked_context_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "fastmcp.server.dependencies":
            raise ImportError("context dependency unavailable")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_context_import)
    assert await engine_factory._live_dispatch_tool("tool", {}, object()) == {"ok": True}
