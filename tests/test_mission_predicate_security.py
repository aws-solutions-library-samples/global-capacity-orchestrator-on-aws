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

Settings cap ``max_examples=100`` and ``deadline=2000`` so the file runs
well under five seconds wall-clock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Mirror the import pattern used by the other Mission tests:
# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime, but pytest
# has to do it itself before the import resolves.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import predicate as predicate_module  # noqa: E402
from mission.predicate import (  # noqa: E402
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
