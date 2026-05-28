"""
Round-trip property test for the Mission domain types JSON serializability.

The Mission filesystem-backed state store writes session payloads with
``json.dumps`` and reloads them with ``json.loads``. This test pins down
that invariant: for every well-formed ``SessionState`` shape Hypothesis
can generate, ``json.loads(json.dumps(s)) == s`` must hold.

The strategy uses ``st.fixed_dictionaries`` for every required key set
defined in ``mcp/mission/types.py`` and draws Literal-typed labels via
``st.sampled_from`` against the literal arguments. ``NotRequired`` fields
are intentionally omitted: the round-trip invariant is the central thing
under test, not the validator surface — well-formed-by-validators data
is exercised separately by the validation tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import get_args

from hypothesis import given, settings
from hypothesis import strategies as st

# Mirror the import pattern used by every other test module that touches
# the MCP package: mcp/run_mcp.py adds mcp/ to sys.path at runtime, but
# tests have to do it themselves before the import.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import types  # noqa: E402

# ---------------------------------------------------------------------------
# Leaf strategies — JSON-stable scalars and recursive "any" values
# ---------------------------------------------------------------------------

# Text without unpaired surrogates (Cs category) so json.dumps never has
# to invoke its non-strict surrogate fallback path.
_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=24,
)

# JSON's number type does not support NaN or Infinity. json.dumps would
# emit them as the non-standard "NaN"/"Infinity" tokens that round-trip
# inequally (NaN != NaN). Constrain to finite floats.
_finite_floats = st.floats(allow_nan=False, allow_infinity=False)

_small_ints = st.integers(min_value=-(2**31), max_value=2**31)
_positive_ints = st.integers(min_value=1, max_value=10**6)
_non_negative_ints = st.integers(min_value=0, max_value=10**6)

# Generic JSON value: any leaf or a bounded list/dict of leaves. Used
# for the typed-as-Any fields (Observation.tool_results items,
# Observation.metrics values, Observation.events items, CriterionResult
# evidence). Dict keys are constrained to strings because json.dumps
# coerces non-string keys to strings on the way out, which would break
# the round-trip equality check.
_json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        _small_ints,
        _finite_floats,
        _text,
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(_text, children, max_size=3),
    ),
    max_leaves=4,
)


# ---------------------------------------------------------------------------
# Literal-label strategies — drawn from the type aliases via typing.get_args
# ---------------------------------------------------------------------------

_verdict_labels = st.sampled_from(list(get_args(types.VerdictLabel)))
_verdict_reasons = st.sampled_from(list(get_args(types.VerdictReason)))
_status_labels = st.sampled_from(list(get_args(types.StatusLabel)))
_criterion_kinds = st.sampled_from(list(get_args(types.CriterionKind)))
_cadence_kinds = st.sampled_from(list(get_args(types.CadenceKind)))

# PhaseRecord.phase, PhaseRecord.status, and CriterionResult.status are
# inline ``Literal[...]`` annotations on the TypedDicts rather than named
# aliases, so we hand-mirror the literal values here.
_phase_names = st.sampled_from(["propose", "execute", "observe", "evaluate", "decide"])
_phase_statuses = st.sampled_from(["succeeded", "failed"])
_criterion_result_statuses = st.sampled_from(["met", "unmet", "inconclusive"])


# ---------------------------------------------------------------------------
# Composite strategies — required-only dict shape for each TypedDict
# ---------------------------------------------------------------------------

_criterion = st.fixed_dictionaries(
    {
        "criterion_id": _text,
        "kind": _criterion_kinds,
        "required": st.booleans(),
    }
)

_budget = st.fixed_dictionaries(
    {
        "max_iterations": _positive_ints,
        "max_wall_clock_seconds": _positive_ints,
    }
)

_cadence = st.fixed_dictionaries({"kind": _cadence_kinds})

# Strategy is declared with ``total=False``; every key is optional and
# the minimal shape that satisfies the type is the empty dict, which
# round-trips trivially through JSON.
_strategy = st.just({})

_observation = st.fixed_dictionaries(
    {
        "tool_results": st.lists(_json_values, max_size=3),
        "metrics": st.dictionaries(_text, _json_values, max_size=3),
        "events": st.lists(
            st.dictionaries(_text, _json_values, max_size=3),
            max_size=3,
        ),
        "phase_started_at": _text,
        "phase_ended_at": _text,
    }
)

_phase_record = st.fixed_dictionaries(
    {
        "phase": _phase_names,
        "status": _phase_statuses,
        "started_at": _text,
        "ended_at": _text,
    }
)

_criterion_result = st.fixed_dictionaries(
    {
        "criterion_id": _text,
        "status": _criterion_result_statuses,
        "evidence": _json_values,
        "evaluated_at": _text,
    }
)

_iteration_record = st.fixed_dictionaries(
    {
        "iteration_index": _non_negative_ints,
        "started_at": _text,
        "ended_at": _text,
        "phases": st.lists(_phase_record, max_size=5),
        "strategy": _strategy,
        "observation": _observation,
        "criteria_evaluation": st.lists(_criterion_result, max_size=3),
        "verdict": _verdict_labels,
        "verdict_reason": _verdict_reasons,
        "checkpoint_evaluated": st.booleans(),
    }
)

_session_state = st.fixed_dictionaries(
    {
        "version": st.just(types.SCHEMA_VERSION),
        "session_id": _text,
        "directive_text": _text,
        "criteria": st.lists(_criterion, max_size=3),
        "budget": _budget,
        "tool_allowlist": st.lists(_text, max_size=4),
        "checkpoint_cadence": _cadence,
        "stagnation_threshold": _non_negative_ints,
        "use_sampling": st.booleans(),
        "allow_scripted_strategies": st.booleans(),
        "status": _status_labels,
        "created_at": _text,
        "iterations": st.lists(_iteration_record, max_size=3),
        "no_progress_counter": _non_negative_ints,
        "accumulated_cost_usd": _finite_floats,
    }
)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


class TestSessionStateRoundTrip:
    """SessionState dicts survive a ``json.dumps`` -> ``json.loads`` round trip."""

    @given(session=_session_state)
    @settings(max_examples=50, deadline=2000)
    def test_json_round_trip_preserves_value(self, session: dict) -> None:
        """For every well-formed minimal SessionState, JSON round-trip is identity.

        This is the persistence-layer invariant: the state backend writes
        sessions to disk via ``json.dumps`` and reloads them via
        ``json.loads``. If any required field's value does not survive
        that pair, on-disk durability silently corrupts session state
        across restarts.
        """
        encoded = json.dumps(session)
        decoded = json.loads(encoded)
        assert decoded == session
