"""
Property tests for the Mission predicate sandbox.

The Mission predicate evaluator runs operator-supplied Python expressions
against an ``Observation`` dict. The source crosses MCP, the CLI, and
disk before it reaches the evaluator, so the parser is the single trust
boundary. Two invariants must hold:

1. **No forbidden source ever reaches the evaluator.** Hypothesis
   synthesises source strings that combine known-dangerous Python
   constructs (``__import__("os")``, ``().__class__``, ``eval("1")``,
   ``lambda x: x``, walrus assignments, attribute walks, subscript-then-
   call, ``getattr(obs, ...)`` and friends) and optionally wraps them in
   arithmetic, comparison, or conditional contexts to look superficially
   like a valid predicate. ``parse_predicate`` must raise
   ``PredicateRejected`` for every drawn input. The evaluator is
   monkey-patched to raise on entry so a parser regression that lets a
   bad string through would surface as a hard failure rather than a
   silent escape.

2. **Well-formed expressions evaluate without raising.** A second
   strategy synthesises safe predicates over a fixed observation dict —
   subscripts on ``obs``, the allowlisted callables (``len``, ``min``,
   ``max``, ``sum``, ``abs``, ``any``, ``all``, ``sorted``), comparisons
   against numeric literals, comprehensions over ``obs["..."]`` lists —
   and asserts the result is a numeric or boolean value.

Example tests pin the size bounds: ``10 ** 10 ** 8``, ``'x' * 10 ** 12``,
``'%0999999999d' % 1``, ``f"{1:>999999999}"`` and callbacks such as
``key=obs.update`` are rejected at parse time, and bounded arithmetic still
evaluates.

Settings cap ``max_examples=100`` and ``deadline=2000`` so the file runs
well under five seconds wall-clock.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Mirror the import pattern used by the other Mission tests:
# ``gco_mcp/run_mcp.py`` adds ``gco_mcp/`` to ``sys.path`` at runtime, but pytest
# has to do it itself before the import resolves.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import predicate as predicate_module
from mission.predicate import (
    PredicateRejected,
    evaluate_predicate,
    parse_predicate,
)

# ---------------------------------------------------------------------------
# Forbidden-snippet grammar
# ---------------------------------------------------------------------------
#
# Each entry is a fragment that, on its own or wrapped in any of the
# context templates below, must be rejected by ``parse_predicate``. The
# fragments cover every escape route catalogued in the predicate module's
# rejection table: dunder access, dynamic call construction, lambdas,
# walrus assignments, attribute walks past ``obs``, subscript-then-call,
# attribute access on builtins, dict unpacking, and dunder-string keys.

_FORBIDDEN_FRAGMENTS: tuple[str, ...] = (
    # Dynamic import / arbitrary code execution.
    '__import__("os")',
    '__import__("subprocess")',
    'eval("1")',
    'exec("1")',
    'compile("1", "", "eval")',
    'open("/etc/hosts")',
    'getattr(obs, "x")',
    'setattr(obs, "x", 1)',
    "globals()",
    "locals()",
    "vars()",
    'print("hello")',
    # Class-walk escapes.
    "().__class__",
    "().__class__.__bases__",
    '"".__class__',
    "(0).__class__",
    "[].__class__",
    "{}.__class__",
    "obs.__class__",
    "sorted.__class__",
    "len.__class__",
    # Lambda — bans hidden code regardless of body.
    "lambda x: x",
    "lambda x: x.__class__",
    "lambda: __import__('os')",
    "(lambda: 1)()",
    # Walrus / named-expression — closes off ``(obs := evil)``.
    "(x := 1)",
    "(obs := 1)",
    "(y := __import__('os'))",
    # Attribute walks past the single ``obs.<attr>`` allowance.
    "obs.foo.bar",
    "obs.metric.real",
    "obs.foo.bar.baz",
    # Subscript-then-call.
    "obs[0]()",
    'obs["k"]()',
    "obs[0][1]()",
    # Dunder identifiers.
    "__class__",
    "__builtins__",
    "__name__",
    # Dunder attribute / dunder string subscript.
    'obs["__import__"]',
    'obs["__class__"]',
    'obs["__builtins__"]',
    # Dict-unpacking — would let an attacker splat an arbitrary mapping.
    "{**obs}",
    # Names outside the allowlist.
    "Exception",
    "type(obs)",
    "id(obs)",
    "dir(obs)",
    "isinstance(obs, dict)",
)


# Wrappers that try to disguise the forbidden fragment as a plausible
# predicate. Each wrapper places ``{f}`` somewhere that produces a
# syntactically valid ``eval``-mode expression.
_WRAPPERS: tuple[str, ...] = (
    "{f}",
    "({f})",
    "{f} and True",
    "True and {f}",
    "{f} or False",
    "({f}) == 1",
    "({f}) > 0",
    "1 if ({f}) else 0",
    "[1 for _ in [({f})]]",
    "len([{f}]) > 0",
    "({f}) + 1",
    "not ({f})",
)


@st.composite
def _forbidden_predicate_source(draw: st.DrawFn) -> str:
    """Synthesise a source string that must be rejected by the parser.

    Picks one fragment from the catalogue and one wrapper from a small
    set of context templates. Every result is syntactically valid in
    ``eval`` mode (so we exercise the validator, not the parser's
    syntax-error path) and contains at least one disallowed construct.
    """
    fragment = draw(st.sampled_from(_FORBIDDEN_FRAGMENTS))
    wrapper = draw(st.sampled_from(_WRAPPERS))
    return wrapper.format(f=fragment)


# ---------------------------------------------------------------------------
# Happy-path grammar — small set of safe expressions over a fixed obs
# ---------------------------------------------------------------------------
#
# The observation is fixed so the property is "every drawn safe
# expression parses and evaluates without raising"; the evaluator
# returns numeric or boolean values that the criterion layer can act on.

_SAFE_OBSERVATION: dict[str, object] = {
    "metric": 0.42,
    "count": 7,
    "xs": [1, 2, 3, 4, 5],
    "ys": [-2, -1, 0, 1, 2],
    "flag": True,
    "label": "ready",
}


_SAFE_KEYS = ("metric", "count", "xs", "ys", "flag")
_NUMERIC_KEYS = ("metric", "count")
_LIST_KEYS = ("xs", "ys")
_COMPARISONS = ("<", "<=", ">", ">=", "==", "!=")
_AGGREGATORS = ("len", "min", "max", "sum")


@st.composite
def _safe_predicate_source(draw: st.DrawFn) -> str:
    """Synthesise a predicate that must parse and evaluate cleanly.

    The grammar covers the categories called out in the task:
      * direct subscript comparisons:        ``obs["metric"] > 0``
      * length comparisons:                  ``len(obs["xs"]) <= 10``
      * aggregator comparisons:              ``min(obs["xs"]) > 0``
      * comprehension predicates:            ``any(x > 0 for x in obs["xs"])``
      * conjunction of two safe clauses:     ``A and B``
    """

    def _numeric_clause() -> str:
        key = draw(st.sampled_from(_NUMERIC_KEYS))
        op = draw(st.sampled_from(_COMPARISONS))
        rhs = draw(st.integers(min_value=-100, max_value=100))
        return f'obs["{key}"] {op} {rhs}'

    def _len_clause() -> str:
        key = draw(st.sampled_from(_LIST_KEYS))
        op = draw(st.sampled_from(_COMPARISONS))
        rhs = draw(st.integers(min_value=0, max_value=20))
        return f'len(obs["{key}"]) {op} {rhs}'

    def _aggregator_clause() -> str:
        agg = draw(st.sampled_from(_AGGREGATORS))
        key = draw(st.sampled_from(_LIST_KEYS))
        op = draw(st.sampled_from(_COMPARISONS))
        rhs = draw(st.integers(min_value=-100, max_value=100))
        return f'{agg}(obs["{key}"]) {op} {rhs}'

    def _comprehension_clause() -> str:
        quant = draw(st.sampled_from(("any", "all")))
        key = draw(st.sampled_from(_LIST_KEYS))
        op = draw(st.sampled_from(_COMPARISONS))
        rhs = draw(st.integers(min_value=-100, max_value=100))
        return f'{quant}(x {op} {rhs} for x in obs["{key}"])'

    def _flag_clause() -> str:
        return 'obs["flag"]'

    builders = (
        _numeric_clause,
        _len_clause,
        _aggregator_clause,
        _comprehension_clause,
        _flag_clause,
    )
    shape = draw(st.sampled_from(("single", "and", "or")))
    if shape == "single":
        return draw(st.sampled_from(builders))()
    left = draw(st.sampled_from(builders))()
    right = draw(st.sampled_from(builders))()
    connector = "and" if shape == "and" else "or"
    return f"({left}) {connector} ({right})"


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestPredicateRejectsForbiddenSource:
    """Forbidden source is rejected at parse time; the evaluator never runs."""

    @given(src=_forbidden_predicate_source())
    @settings(max_examples=100, deadline=2000)
    def test_parse_predicate_rejects_forbidden_source(self, src: str) -> None:
        """Every synthesised forbidden source raises ``PredicateRejected``.

        Patching ``evaluate_predicate`` to raise on entry makes the
        rejection contract explicit: if a future regression lets a
        forbidden source through the parser, the evaluator's tripwire
        fires and the test fails with a distinct error rather than
        silently letting the dangerous code run. The patch is applied
        with ``pytest.MonkeyPatch.context()`` (rather than the
        function-scoped ``monkeypatch`` fixture) so each Hypothesis
        example gets a fresh, fully-reset patch — Hypothesis flags
        function-scoped fixtures as a health-check failure because
        their state carries across generated examples.
        """

        def _tripwire(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("evaluate_predicate must not be called for rejected source")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(predicate_module, "evaluate_predicate", _tripwire)
            with pytest.raises(PredicateRejected):
                parse_predicate(src)


class TestPredicateRejectsUnboundedWork:
    """Operators that turn a few characters into unbounded work are rejected.

    Every source here validated before the size bounds existed, and
    evaluating it pinned a CPU or exhausted memory inside the MCP server.
    """

    @pytest.mark.parametrize(
        ("src", "reason"),
        [
            ("10 ** 10 ** 8 > 0", "exponent_not_bounded"),
            ("9 ** 9 ** 9 ** 9 > 0", "exponent_not_bounded"),
            ("(10 ** 64) ** 64 > 0", "exponent_not_bounded"),
            ("2 ** 65 > 0", "exponent_not_bounded"),
            ("2 ** -65 > 0", "exponent_not_bounded"),
            ("2 ** obs['n'] > 0", "exponent_not_bounded"),
            ("2 ** True > 0", "exponent_not_bounded"),
            ("int('9' * 1000) ** 64 > 0", "exponent_not_bounded"),
            ("len('x' * 10 ** 12) > 0", "repetition_not_bounded"),
            ("len('x' * 1001) > 0", "repetition_not_bounded"),
            ("len('x' * -1) == 0", "repetition_not_bounded"),
            ("len('x' * True) == 1", "repetition_not_bounded"),
            ("len(obs['xs'] * obs['n']) > 0", "repetition_not_bounded"),
            ("len('x' * 1000 * 1000) > 0", "repetition_not_bounded"),
            ("len(1000 * 'x' * 1000) > 0", "repetition_not_bounded"),
            ("len(str([['x' * 1000] * 1000] * 1000)) > 0", "repetition_not_bounded"),
            ("len('%0999999999d' % 1) > 0", "string_formatting_not_allowed"),
            ("len(('%0999' + '999999d') % 1) > 0", "string_formatting_not_allowed"),
            ("any(x % 1 for x in ['%0999999999d'])", "string_formatting_not_allowed"),
            ("obs['step'] % 10 == 0", "string_formatting_not_allowed"),
            ('len(f"{1:>999999999}") > 0', "format_spec_not_bounded"),
            ('len(f"{1:.1001f}") > 0', "format_spec_not_bounded"),
            ('len(f"{1:{obs}}") > 0', "format_spec_not_bounded"),
        ],
    )
    def test_rejected_before_evaluation(self, src: str, reason: str) -> None:
        with pytest.raises(PredicateRejected) as exc_info:
            parse_predicate(src)
        assert exc_info.value.reason == reason
        assert exc_info.value.lineno == 1

    def test_source_longer_than_the_cap_is_rejected(self) -> None:
        assert predicate_module._MAX_SOURCE_CHARS == 4096
        at_limit = "'" + "x" * 4094 + "'"
        assert evaluate_predicate(parse_predicate(at_limit), {}) == "x" * 4094
        with pytest.raises(PredicateRejected) as exc_info:
            parse_predicate(at_limit + " ")
        assert exc_info.value.reason == "source_too_long"
        assert exc_info.value.lineno is None

    def test_tree_deeper_than_the_cap_is_rejected_instead_of_recursing(self) -> None:
        # 500 additions fit in the source cap; the recursive validator
        # raised RecursionError on them before the depth bound existed.
        assert predicate_module._MAX_DEPTH == 100
        with pytest.raises(PredicateRejected) as exc_info:
            parse_predicate("1" + " + 1" * 500)
        assert exc_info.value.reason == "expression_too_deep"
        # Expression -> 98 UnaryOp -> Constant is exactly 100 levels.
        assert evaluate_predicate(parse_predicate("-" * 98 + "1"), {}) == 1
        with pytest.raises(PredicateRejected) as exc_info:
            parse_predicate("-" * 99 + "1")
        assert exc_info.value.reason == "expression_too_deep"

    @pytest.mark.parametrize(
        "src",
        [
            "sorted(['metric_history'], key=obs.update)",
            "min(['tool_results'], key=obs.pop)",
            "max(['k'], key=obs.setdefault)",
            "sorted([1], key=obs.clear)",
            "obs.events",
            "obs.value",
        ],
    )
    def test_obs_attributes_other_than_read_only_methods_are_rejected(self, src: str) -> None:
        with pytest.raises(PredicateRejected) as exc_info:
            parse_predicate(src)
        assert exc_info.value.reason == "attribute_not_allowed"

    def test_read_only_method_callback_does_not_mutate_the_observation(self) -> None:
        obs = {"xs": ["b", "a"], "a": 2, "b": 1}
        parsed = parse_predicate("sorted(obs['xs'], key=obs.get) == ['b', 'a']")
        assert evaluate_predicate(parsed, obs) is True
        assert obs == {"xs": ["b", "a"], "a": 2, "b": 1}

    @pytest.mark.parametrize(
        ("src", "expected"),
        [
            ("2 ** 10", 1024),
            ("2 ** -1", 0.5),
            ("(-2) ** 64 > 0", True),
            ("obs['n'] ** 2", 9),
            ("[0] * 3", [0, 0, 0]),
            ("3 * 'ab'", "ababab"),
            ("- - 2 * 'ab'", "abab"),
            ("+2 * 'ab'", "abab"),
            ("-len(obs['xs']) * 1001", -2002),
            ("'-' * 1000 == '-' * 1000", True),
            ("obs['loss'] * 0.5", 0.25),
            ("obs['xs'] * 2", [1, 2, 1, 2]),
            ("[len(obs['xs']) * 2] * 3", [4, 4, 4]),
            ("float(obs['loss']) * 100 * 2", 100.0),
            ("int('9' * 3) * int('9' * 3)", 998001),
            ("sum(obs['xs']) * 1001", 3003),
            ("int(obs['step']) % 7", 6),
            ("len(obs['xs']) % 2", 0),
            ("f'{obs[\"loss\"]:>10.3f}'", "     0.500"),
            ("f'{obs[\"n\"]:%}'", "300.000000%"),
        ],
    )
    def test_bounded_arithmetic_still_evaluates(self, src: str, expected: object) -> None:
        obs = {"n": 3, "loss": 0.5, "xs": [1, 2], "step": "20"}
        assert evaluate_predicate(parse_predicate(src), obs) == expected

    def test_format_spec_that_is_not_a_joined_string_is_checked_as_one_part(self) -> None:
        def validate(spec: ast.expr) -> None:
            value = ast.FormattedValue(value=ast.Constant(1), conversion=-1, format_spec=spec)
            tree = ast.Expression(body=ast.JoinedStr(values=[value]))
            predicate_module._PredicateValidator().visit(ast.fix_missing_locations(tree))

        validate(ast.Constant(">5"))
        with pytest.raises(PredicateRejected) as exc_info:
            validate(ast.Name(id="obs", ctx=ast.Load()))
        assert exc_info.value.reason == "format_spec_not_bounded"


class TestPredicateAcceptsSafeSource:
    """Well-formed predicates over ``obs`` parse and evaluate cleanly."""

    @given(src=_safe_predicate_source())
    @settings(max_examples=100, deadline=2000)
    def test_safe_predicate_evaluates_to_numeric_or_bool(self, src: str) -> None:
        """Every drawn safe predicate parses, evaluates, and returns
        a value of a type the criterion layer can act on (``bool``,
        ``int``, or ``float``).
        """
        parsed = parse_predicate(src)
        result = evaluate_predicate(parsed, _SAFE_OBSERVATION)
        # ``bool`` is a subclass of ``int`` in Python, so this also
        # admits truthy/falsy boolean clauses produced by the grammar.
        assert isinstance(result, (bool, int, float))
