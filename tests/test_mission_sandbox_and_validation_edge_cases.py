"""Targeted Mission runtime AST, validation, and scaffold trust-boundary tests."""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GCO_MCP_ROOT = str(PROJECT_ROOT / "gco_mcp")
if GCO_MCP_ROOT not in sys.path:
    sys.path.insert(0, GCO_MCP_ROOT)

from mission import criteria_scaffold, predicate, sandbox, validation  # noqa: E402


def _assert_script_rejected(source: str, reason: str) -> sandbox.ScriptRejected:
    with pytest.raises(sandbox.ScriptRejected) as exc_info:
        sandbox.validate_script_ast(source, ["tool"])
    assert exc_info.value.reason == reason
    assert exc_info.value.lineno is not None
    assert exc_info.value.col_offset is not None
    return exc_info.value


def _assert_predicate_node_rejected(node: ast.AST, reason: str) -> None:
    validator = predicate._PredicateValidator()
    with pytest.raises(predicate.PredicateRejected) as exc_info:
        validator.visit(node)
    assert exc_info.value.reason == reason


def test_sandbox_accepts_control_flow_returns_raise_and_signature_paths() -> None:
    source = (
        "for x in [1]:\n"
        "    if x:\n"
        "        continue\n"
        "    else:\n"
        "        break\n"
        "def f(a: int = 1, *, b: int = 2):\n"
        "    if a:\n"
        "        return\n"
        "    return b\n"
        "def g():\n"
        "    raise ValueError('bad') from RuntimeError('cause')\n"
        "x: int\n"
    )
    sandbox.validate_script_ast(source, ["tool"])


def test_sandbox_rejects_augassign_target_walrus_and_operator_paths() -> None:
    _assert_script_rejected("x = 1\nx <<= 1\n", "binop_not_allowed")
    _assert_script_rejected("missing\n", "name_not_allowed")

    validator = sandbox._ScriptValidator(["tool"])
    invalid_walrus = ast.NamedExpr(
        target=ast.Subscript(
            value=ast.Name(id="x", ctx=ast.Load()),
            slice=ast.Constant(0),
            ctx=ast.Store(),
        ),
        value=ast.Constant(1),
    )
    with pytest.raises(sandbox.ScriptRejected) as exc_info:
        validator.visit(invalid_walrus)
    assert exc_info.value.reason == "invalid_target"

    nodes_and_reasons: list[tuple[ast.AST, str]] = [
        (
            ast.BinOp(left=ast.Constant(1), op=ast.LShift(), right=ast.Constant(2)),
            "binop_not_allowed",
        ),
        (ast.UnaryOp(op=ast.Add(), operand=ast.Constant(1)), "unaryop_not_allowed"),
        (ast.BoolOp(op=ast.Add(), values=[ast.Constant(True)]), "boolop_not_allowed"),
        (
            ast.Compare(left=ast.Constant(1), ops=[ast.Add()], comparators=[ast.Constant(2)]),
            "compareop_not_allowed",
        ),
    ]
    for node, reason in nodes_and_reasons:
        with pytest.raises(sandbox.ScriptRejected) as rejected:
            sandbox._ScriptValidator(["tool"]).visit(node)
        assert rejected.value.reason == reason


def test_sandbox_annassign_defensive_shape_and_reject_fallthrough() -> None:
    validator = sandbox._ScriptValidator(["tool"])
    synthetic = ast.AnnAssign(
        target=ast.Name(id="x", ctx=ast.Store()),
        annotation=None,
        value=None,
        simple=1,
    )
    validator.visit(synthetic)

    fallthrough = sandbox._ScriptValidator(["tool"])
    fallthrough._reject = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    target = ast.Attribute(value=ast.Name(id="x", ctx=ast.Load()), attr="field")
    assert fallthrough._collect_target_names(target) == []


def test_sandbox_function_decorator_loop_and_format_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_ALLOWED_DECORATORS", frozenset({"deco"}))
    sandbox.validate_script_ast(
        "def deco(f):\n    return f\n@deco\ndef f():\n    return\nx = f'{1:04d}'\n",
        ["tool"],
    )


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("await value\n", "await_not_allowed"),
        ("await len([])\n", "await_not_allowed"),
        ("x = []\nawait x.observe()\n", "await_not_allowed"),
        ("await (lambda: 1)()\n", "await_not_allowed"),
    ],
)
def test_sandbox_rejects_every_untrusted_await_shape(source: str, reason: str) -> None:
    _assert_script_rejected(source, reason)


def test_sandbox_comprehension_and_generator_paths() -> None:
    _assert_script_rejected(
        "xs = []\nresult = [x async for x in xs]\n",
        "async_comprehension",
    )
    sandbox.validate_script_ast(
        "xs = [1, 2]\nresult = (x for x in xs if x > 0)\n",
        ["tool"],
    )


def test_sandbox_public_validation_errors_preserve_source_location() -> None:
    with pytest.raises(sandbox.ScriptRejected) as not_string:
        sandbox.validate_script_ast(1, ["tool"])  # type: ignore[arg-type]
    assert not_string.value.reason == "not_a_string"

    with pytest.raises(sandbox.ScriptRejected) as syntax:
        sandbox.validate_script_ast("if :\n    pass", ["tool"])
    assert syntax.value.reason == "syntax_error"
    assert syntax.value.lineno == 1
    assert syntax.value.col_offset is not None


def test_sandbox_observation_shape_loops_and_runtime_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = sandbox._build_script_observation(
        script_call_log=[
            {
                "tool_name": "tool",
                "status": "ok",
                "result_summary": {
                    "metrics": {"gpu": 1},
                    "events": ["bad", {"event_name": "ready"}],
                },
            }
        ],
        observe_log=[],
        event_log=[],
        phase_started_at="start",
        phase_ended_at="end",
    )
    assert observation["events"] == [{"event_name": "ready"}]

    class Provider:
        def __init__(self, *, limits: dict[str, Any]) -> None:
            assert set(limits) == {"max_duration_secs", "max_memory"}

    monkeypatch.setattr(sandbox, "_import_provider", lambda: (Provider, RuntimeError))
    session = {
        "session_id": "s",
        "directive_text": "goal",
        "criteria": [],
        "budget": {},
        "iterations": [
            {
                "iteration_index": 0,
                "verdict": "continue",
                "verdict_reason": "in_progress",
                "checkpoint_evaluated": True,
            }
        ],
    }
    runtime = sandbox.MissionSandbox(["tool"], session)
    assert runtime.frozen_mission_ns["iteration_index"] == 1
    assert runtime.frozen_mission_ns["iterations"][0]["verdict"] == "continue"
    copied = runtime.allowlist
    copied.append("other")
    assert runtime.allowlist == ["tool"]


def test_predicate_defensive_target_fallthrough_and_operator_rejections() -> None:
    validator = predicate._PredicateValidator()
    validator._reject = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    target = ast.Attribute(value=ast.Name(id="obs", ctx=ast.Load()), attr="field")
    assert validator._collect_target_names(target) == []

    nodes_and_reasons: list[tuple[ast.AST, str]] = [
        (
            ast.BinOp(left=ast.Constant(1), op=ast.LShift(), right=ast.Constant(2)),
            "binop_not_allowed",
        ),
        (ast.UnaryOp(op=ast.Add(), operand=ast.Constant(1)), "unaryop_not_allowed"),
        (ast.BoolOp(op=ast.Add(), values=[ast.Constant(True)]), "boolop_not_allowed"),
        (
            ast.Compare(left=ast.Constant(1), ops=[ast.Add()], comparators=[ast.Constant(2)]),
            "compareop_not_allowed",
        ),
    ]
    for node, reason in nodes_and_reasons:
        _assert_predicate_node_rejected(node, reason)


def test_predicate_allowed_attribute_slice_call_format_and_async_security() -> None:
    assert isinstance(predicate.parse_predicate("obs.value"), ast.Expression)
    for source in ("obs[:]", "obs[1:]", "obs[:2]", "obs[::2]"):
        assert isinstance(predicate.parse_predicate(source), ast.Expression)
    assert isinstance(predicate.parse_predicate("sorted(obs, reverse=True)"), ast.Expression)
    assert isinstance(predicate.parse_predicate("f'{obs:>10}'"), ast.Expression)

    with pytest.raises(predicate.PredicateRejected) as exc_info:
        predicate.parse_predicate("[x async for x in obs]")
    assert exc_info.value.reason == "async_comprehension"
    assert exc_info.value.lineno is not None


def _criterion(criterion_id: str = "c", **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "criterion_id": criterion_id,
        "kind": "event",
        "required": True,
        "event_name": "ready",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("criteria", "reason"),
    [
        (["bad"], "not_a_dict"),
        (
            [{"kind": "event", "required": True, "event_name": "x"}],
            "criterion_id_missing_or_invalid",
        ),
        ([_criterion(), _criterion()], "duplicate_criterion_id"),
        ([_criterion(required="yes")], "required_missing_or_not_a_bool"),
    ],
)
def test_validation_criteria_rejection_details(criteria: Any, reason: str) -> None:
    with pytest.raises(validation.MissionValidationError) as exc_info:
        validation.validate_criteria(criteria)
    assert exc_info.value.details["reason"] == reason


def test_validation_budget_allowlist_and_cadence_rejection_details() -> None:
    with pytest.raises(validation.MissionValidationError) as budget:
        validation.validate_budget(
            {"max_iterations": 0, "max_wall_clock_seconds": 60},
            [],
            {},
        )
    assert budget.value.details["subfield"] == "max_iterations"

    with pytest.raises(validation.MissionValidationError) as invalid_name:
        validation.validate_tool_allowlist([1], {})  # type: ignore[list-item]
    assert invalid_name.value.details["reason"] == "tool_name_missing_or_invalid"

    with pytest.raises(validation.MissionValidationError) as duplicate:
        validation.validate_tool_allowlist(["tool", "tool"], {"tool": object()})
    assert duplicate.value.details["reason"] == "duplicate_tool_name"

    with pytest.raises(validation.MissionValidationError) as kind:
        validation.validate_cadence({"kind": "whenever"})
    assert kind.value.details["reason"] == "kind_invalid"

    with pytest.raises(validation.MissionValidationError) as event:
        validation.validate_cadence({"kind": "on_event", "event_name": 1})
    assert event.value.details["subfield"] == "event_name"


def test_validation_strategy_error_and_optional_field_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(validation.MissionValidationError) as calls:
        validation.validate_strategy({"tool_calls": []}, ["tool"], False)
    assert calls.value.details["reason"] == "must_be_non_empty_list"

    real_import = builtins.__import__

    def blocked_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "mission.sandbox":
            raise ModuleNotFoundError("sandbox unavailable")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(validation.MissionValidationError) as sandbox_missing:
        validation.validate_strategy({"script": "pass"}, ["tool"], True)
    assert sandbox_missing.value.code == "script_sandbox_not_implemented"
    monkeypatch.setattr(builtins, "__import__", real_import)

    with pytest.raises(validation.MissionValidationError) as keys:
        validation.validate_strategy(
            {"tool_calls": [{"tool_name": "tool"}], "expected_observation_keys": [1]},
            ["tool"],
            False,
        )
    assert keys.value.details["subfield"] == "expected_observation_keys"

    with pytest.raises(validation.MissionValidationError) as rationale:
        validation.validate_strategy(
            {"tool_calls": [{"tool_name": "tool"}], "rationale": 1},
            ["tool"],
            False,
        )
    assert rationale.value.details["subfield"] == "rationale"


def test_scaffold_slug_clamp_and_response_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    assert criteria_scaffold._slugify("!!!", fallback="fallback") == "fallback"
    generated = criteria_scaffold.generate_deterministic_criteria(
        "find capacity",
        allowlist=["one", "two"],
        max_criteria=0,
    )
    assert len(generated) == 1
    assert generated[0]["tool_name"] == "one"

    assert criteria_scaffold._parse_response("```[{}]```") == [{}]
    assert criteria_scaffold._parse_response("```json\n[{}]\ntrailing") == [{}]

    monkeypatch.setattr(criteria_scaffold.json, "loads", lambda _text: {})
    with pytest.raises(ValueError, match="not a list"):
        criteria_scaffold._parse_response("[]")
    monkeypatch.undo()

    with pytest.raises(ValueError, match="not an object"):
        criteria_scaffold._parse_response("[1]")


def test_scaffold_normalize_rewriter_and_unparse_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = {"kind": "metric_threshold"}
    monkeypatch.setitem(
        criteria_scaffold._KIND_ALIASES,
        "metric_threshold",
        "metric_threshold",
    )
    assert criteria_scaffold._normalize_kind_name(canonical) is canonical

    node = ast.parse("other.value", mode="eval").body
    rewritten = criteria_scaffold._AttributeToSubscriptRewriter().visit(node)
    assert isinstance(rewritten, ast.Attribute)

    original = {
        "criterion_id": "p",
        "kind": "predicate",
        "required": True,
        "expression": "obs.metrics.loss < 1",
    }

    def fail_unparse(_tree: ast.AST) -> str:
        raise RuntimeError("unparse unavailable")

    monkeypatch.setattr(criteria_scaffold.ast, "unparse", fail_unparse)
    assert criteria_scaffold._autofix_predicate(original) is original


def test_sandbox_kwargs_raise_without_cause_and_observation_loop_edges() -> None:
    sandbox.validate_script_ast(
        "def f(**kwargs):\n    raise ValueError('bad')\nf()\n",
        ["tool"],
    )
    observation = sandbox._build_script_observation(
        script_call_log=[
            {
                "tool_name": "tool",
                "status": "ok",
                "result_summary": {"events": ["skip", {"event_name": "tool-event"}]},
            },
            {
                "tool_name": "tool",
                "status": "failed",
                "error_message": "boom",
                "result_summary": None,
            },
        ],
        observe_log=[],
        event_log=[{"event_name": "script-event"}],
        phase_started_at="start",
        phase_ended_at="end",
    )
    assert observation["events"] == [
        {"event_name": "tool-event"},
        {"event_name": "script-event"},
    ]
    assert observation["errors"][0]["error_message"] == "boom"


def test_remaining_ast_security_and_validation_boundaries() -> None:
    sandbox.validate_script_ast(
        "def identity(value: int, /):\n"
        "    return value\n"
        "try:\n"
        "    raise ValueError('bad')\n"
        "except ValueError:\n"
        "    raise\n",
        ["tool"],
    )
    _assert_script_rejected("value = __secret\n", "dunder_name")

    with pytest.raises(predicate.PredicateRejected) as dunder_target:
        predicate.parse_predicate("[__item for __item in obs]")
    assert dunder_target.value.reason == "dunder_comprehension_target"

    observation = sandbox._build_script_observation(
        script_call_log=[
            {"tool_name": "first", "status": "ok", "result_summary": None},
            {"tool_name": "second", "status": "ok", "result_summary": {}},
        ],
        observe_log=[],
        event_log=[],
        phase_started_at="start",
        phase_ended_at="end",
    )
    assert len(observation["tool_results"]) == 2

    invalid_inputs = [
        (lambda: validation.validate_criteria(None), "criteria", "not_a_list"),
        (lambda: validation.validate_budget(None, [], {}), "budget", "not_a_dict"),
        (
            lambda: validation.validate_tool_allowlist(None, {}),
            "tool_allowlist",
            "not_a_list",
        ),
        (
            lambda: validation.validate_cadence(None),
            "checkpoint_cadence",
            "not_a_dict",
        ),
    ]
    for validate, field, reason in invalid_inputs:
        with pytest.raises(validation.MissionValidationError) as exc_info:
            validate()
        assert exc_info.value.details == {"field": field, "reason": reason}

    with pytest.raises(validation.MissionValidationError) as empty_script:
        validation.validate_strategy({"script": ""}, ["tool"], True)
    assert empty_script.value.details["reason"] == "must_be_non_empty_string"

    expected_keys = ["metrics.loss"]
    normalized = validation.validate_strategy(
        {
            "tool_calls": [{"tool_name": "tool", "args": {}}],
            "expected_observation_keys": expected_keys,
        },
        ["tool"],
        False,
    )
    assert normalized["expected_observation_keys"] == expected_keys
    assert normalized["expected_observation_keys"] is not expected_keys
