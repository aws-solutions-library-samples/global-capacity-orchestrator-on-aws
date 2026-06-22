"""Property-based soundness tests for the Mission validators.

The Mission entry points (the MCP tools, the CLI subcommands, the engine's
session loader) all funnel operator-supplied JSON through
``gco_mcp/mission/validation.py`` before any state is created. The validators
are pure: they either return a normalized value or raise
``MissionValidationError`` with a stable short ``code`` and a structured
``details`` dict whose ``field``/``reason`` keys identify the rejection.

These tests pin down two complementary invariants per validator:

* **Negative invariant** — synthesised malformed inputs always raise
  ``MissionValidationError`` with the expected ``code`` and the expected
  ``details["field"]`` / ``details["reason"]`` markers. Hypothesis
  searches the malformed input space; the mapping from input shape to
  rejection reason is the property under test.
* **Positive invariant** — well-formed inputs (either Hypothesis-drawn
  with constrained strategies or hand-crafted) return the normalised
  shape the validator promises. For ``predicate`` criteria the
  normalised entry carries the cached AST under ``_parsed_ast``.

Hypothesis settings cap ``max_examples=50`` and ``deadline=2000`` so the
file completes well under five seconds wall-clock.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Mirror the import pattern the other Mission tests use: ``gco_mcp/run_mcp.py``
# adds ``gco_mcp/`` to ``sys.path`` at runtime, but pytest has to do it itself
# before the import below resolves.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import validation  # noqa: E402
from mission.validation import MissionValidationError  # noqa: E402

# A single shared settings profile keeps every property test bounded.
_PBT_SETTINGS = settings(
    max_examples=50,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _expect_validation_error(
    callable_,
    *,
    field: str,
    reason: str | None = None,
    code: str = "validation_error",
) -> MissionValidationError:
    """Run ``callable_`` and assert it raises with the expected markers.

    Returns the exception so individual tests can assert on additional
    keys when useful.
    """
    with pytest.raises(MissionValidationError) as exc_info:
        callable_()
    err = exc_info.value
    assert err.code == code, f"expected code {code!r}, got {err.code!r}"
    assert err.details is not None, "validator must attach a details dict"
    assert err.details.get("field") == field, (
        f"expected details.field={field!r}, got {err.details.get('field')!r}"
    )
    if reason is not None:
        assert err.details.get("reason") == reason, (
            f"expected details.reason={reason!r}, got {err.details.get('reason')!r}"
        )
    return err


# ---------------------------------------------------------------------------
# Directive
# ---------------------------------------------------------------------------


class TestValidateDirective:
    """The directive must be a non-empty trimmed string up to 8192 chars."""

    @given(
        # Whitespace-only strings — every char is in the strip-set so the
        # trimmed result is the empty string.
        ws=st.text(alphabet=" \t\n\r\f\v", min_size=0, max_size=16),
    )
    @_PBT_SETTINGS
    def test_blank_or_whitespace_only_directive_rejected(self, ws: str) -> None:
        _expect_validation_error(
            lambda: validation.validate_directive(ws),
            field="directive",
            reason="empty",
        )

    @given(
        # Pad past the 8192-char hard cap. Hypothesis caps its own text()
        # generator below this length, so we synthesise the overflow by
        # picking the number of extra chars and a single-char filler and
        # building the string by repetition.
        overflow=st.integers(min_value=1, max_value=200),
        filler=st.sampled_from("abcXYZ0193"),
    )
    @_PBT_SETTINGS
    def test_directive_above_max_length_rejected(self, overflow: int, filler: str) -> None:
        body = filler * (8192 + overflow)
        err = _expect_validation_error(
            lambda: validation.validate_directive(body),
            field="directive",
            reason="too_long",
        )
        # The validator surfaces the max-length cap and the actual length
        # so the operator can size the input correctly without re-reading
        # the docs.
        assert err.details is not None
        assert err.details["max_length"] == 8192
        assert err.details["actual_length"] == len(body)

    @given(
        # Synthesise an input that is *not* a string. ``one_of`` makes
        # Hypothesis cover every leaf type the validator must reject.
        non_str=st.one_of(
            st.none(),
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.lists(st.text(), max_size=2),
            st.dictionaries(st.text(), st.integers(), max_size=2),
        ),
    )
    @_PBT_SETTINGS
    def test_non_string_directive_rejected(self, non_str: object) -> None:
        _expect_validation_error(
            lambda: validation.validate_directive(non_str),  # type: ignore[arg-type]
            field="directive",
            reason="not_a_string",
        )

    @given(
        # Constrained well-formed strategy: surrounding whitespace plus a
        # non-whitespace core. The validator must trim the whitespace and
        # return the core unchanged.
        leading=st.text(alphabet=" \t", max_size=4),
        core=st.text(
            alphabet=st.characters(
                min_codepoint=33, max_codepoint=126, blacklist_categories=("Cs",)
            ),
            min_size=1,
            max_size=64,
        ),
        trailing=st.text(alphabet=" \t", max_size=4),
    )
    @_PBT_SETTINGS
    def test_well_formed_directive_returns_trimmed_text(
        self, leading: str, core: str, trailing: str
    ) -> None:
        result = validation.validate_directive(leading + core + trailing)
        assert result == core

    def test_directive_at_exact_max_length_accepted(self) -> None:
        """The 8192-char cap is inclusive; the boundary value is accepted."""
        text = "x" * 8192
        assert validation.validate_directive(text) == text


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


# A small alphabet of stable identifiers used for criterion ids.
_ID_ALPHABET = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=8,
)


def _metric_threshold(criterion_id: str) -> dict[str, Any]:
    """A canonical well-formed metric_threshold criterion."""
    return {
        "criterion_id": criterion_id,
        "kind": "metric_threshold",
        "required": True,
        "metric": "loss",
        "op": "<",
        "target": 0.1,
    }


def _event_criterion(criterion_id: str) -> dict[str, Any]:
    """A canonical well-formed event criterion."""
    return {
        "criterion_id": criterion_id,
        "kind": "event",
        "required": False,
        "event_name": "training_complete",
    }


def _predicate_criterion(criterion_id: str) -> dict[str, Any]:
    """A canonical well-formed predicate criterion."""
    return {
        "criterion_id": criterion_id,
        "kind": "predicate",
        "required": True,
        "expression": 'obs["count"] > 0',
    }


class TestValidateCriteria:
    """Criteria must have unique ids and the kind-specific keys."""

    @given(criterion_id=_ID_ALPHABET)
    @_PBT_SETTINGS
    def test_duplicate_criterion_id_rejected(self, criterion_id: str) -> None:
        criteria = [
            _metric_threshold(criterion_id),
            _event_criterion(criterion_id),
        ]
        err = _expect_validation_error(
            lambda: validation.validate_criteria(criteria),
            field="criteria",
            reason="duplicate_criterion_id",
        )
        assert err.details is not None
        assert err.details["criterion_id"] == criterion_id

    @given(
        # A metric_threshold entry with one required kind-specific key
        # missing. The strategy picks which one to drop and produces the
        # matching expected rejection reason.
        drop=st.sampled_from(
            [
                ("metric", "metric_missing_or_invalid"),
                ("op", "op_invalid"),
                ("target", "target_not_a_number"),
            ]
        ),
    )
    @_PBT_SETTINGS
    def test_metric_threshold_missing_required_key_rejected(self, drop: tuple[str, str]) -> None:
        key_to_drop, expected_reason = drop
        entry = _metric_threshold("c1")
        del entry[key_to_drop]
        err = _expect_validation_error(
            lambda: validation.validate_criteria([entry]),
            field="criteria",
            reason=expected_reason,
        )
        assert err.details is not None
        assert err.details["criterion_id"] == "c1"

    @given(
        # The op key must be one of the six comparison tokens; anything
        # else is rejected as op_invalid.
        bad_op=st.one_of(
            st.text(min_size=1, max_size=4).filter(
                lambda s: s not in {"<", "<=", ">", ">=", "==", "!="}
            ),
            st.integers(),
            st.none(),
        ),
    )
    @_PBT_SETTINGS
    def test_metric_threshold_invalid_op_rejected(self, bad_op: object) -> None:
        entry = _metric_threshold("c1")
        entry["op"] = bad_op
        _expect_validation_error(
            lambda: validation.validate_criteria([entry]),
            field="criteria",
            reason="op_invalid",
        )

    @given(
        # Non-numeric targets — bool excluded because the validator's
        # _is_number explicitly rejects bool, but a user-supplied bool is
        # still a violation we want to confirm.
        bad_target=st.one_of(
            st.text(),
            st.none(),
            st.lists(st.integers(), max_size=2),
            st.booleans(),
        ),
    )
    @_PBT_SETTINGS
    def test_metric_threshold_non_numeric_target_rejected(self, bad_target: object) -> None:
        entry = _metric_threshold("c1")
        entry["target"] = bad_target
        _expect_validation_error(
            lambda: validation.validate_criteria([entry]),
            field="criteria",
            reason="target_not_a_number",
        )

    @given(
        bad_event=st.one_of(
            st.just(""),
            st.none(),
            st.integers(),
            st.lists(st.text(), max_size=2),
        ),
    )
    @_PBT_SETTINGS
    def test_event_missing_or_invalid_event_name_rejected(self, bad_event: object) -> None:
        entry = _event_criterion("c1")
        if bad_event is None:
            del entry["event_name"]
        else:
            entry["event_name"] = bad_event
        _expect_validation_error(
            lambda: validation.validate_criteria([entry]),
            field="criteria",
            reason="event_name_missing_or_invalid",
        )

    @given(
        # A grab-bag of clearly malformed predicate expressions: outright
        # syntax errors, dunder access, lambdas, attribute walks past
        # ``obs``, calls to disallowed names, and non-string types. The
        # exact rejection reason varies (syntax_error vs. one of the
        # _PredicateValidator rejections), so the test asserts only the
        # field marker.
        bad_expression=st.sampled_from(
            [
                "((",  # syntax error
                "1 +",  # syntax error
                "lambda x: x",  # lambda
                "__import__('os')",  # dunder
                "obs.foo.bar.baz",  # attribute walk
                "Exception",  # name not in allowlist
                'eval("1")',  # disallowed call
                "obs[0]()",  # subscript-then-call
                "(x := 1)",  # walrus
                "{**obs}",  # dict unpacking
            ]
        ),
    )
    @_PBT_SETTINGS
    def test_predicate_with_malformed_expression_rejected(self, bad_expression: str) -> None:
        entry = _predicate_criterion("c1")
        entry["expression"] = bad_expression
        with pytest.raises(MissionValidationError) as exc_info:
            validation.validate_criteria([entry])
        err = exc_info.value
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["field"] == "criteria"
        assert err.details["criterion_id"] == "c1"
        # The reason comes from _PredicateValidator (or "syntax_error"),
        # so it cannot be one of the structural reasons that signal the
        # predicate-specific keys are missing or wrong-typed.
        assert err.details.get("reason") not in {
            None,
            "expression_missing_or_invalid",
        }

    @given(
        # Non-string or empty expression — handled by the structural
        # check before the parser ever runs, so the reason is the
        # expression-missing token specifically.
        bad_expression=st.one_of(
            st.just(""),
            st.none(),
            st.integers(),
            st.lists(st.text(), max_size=2),
        ),
    )
    @_PBT_SETTINGS
    def test_predicate_with_missing_or_non_string_expression_rejected(
        self, bad_expression: object
    ) -> None:
        entry = _predicate_criterion("c1")
        if bad_expression is None:
            del entry["expression"]
        else:
            entry["expression"] = bad_expression
        _expect_validation_error(
            lambda: validation.validate_criteria([entry]),
            field="criteria",
            reason="expression_missing_or_invalid",
        )

    def test_empty_criteria_list_rejected(self) -> None:
        _expect_validation_error(
            lambda: validation.validate_criteria([]),
            field="criteria",
            reason="empty",
        )

    def test_well_formed_criteria_returns_normalized_list(self) -> None:
        criteria = [
            _metric_threshold("loss-cap"),
            _event_criterion("training-finished"),
            _predicate_criterion("non-empty-batch"),
        ]
        normalized = validation.validate_criteria(criteria)
        assert len(normalized) == 3
        # Inputs are not mutated; the normalised list carries shallow copies.
        assert "_parsed_ast" not in criteria[2]
        # The predicate entry carries the cached parsed AST under the
        # private key.
        assert "_parsed_ast" in normalized[2]
        # The metric_threshold and event entries pass straight through.
        assert normalized[0]["criterion_id"] == "loss-cap"
        assert normalized[0]["metric"] == "loss"
        assert normalized[1]["event_name"] == "training_complete"

    def test_tool_call_succeeded_minimal_shape_accepted(self) -> None:
        """The new kind validates with just ``tool_name`` (default min_count=1)."""
        criteria = [
            {
                "criterion_id": "find_docs_called",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
            }
        ]
        normalized = validation.validate_criteria(criteria)
        assert len(normalized) == 1
        assert normalized[0]["kind"] == "tool_call_succeeded"
        assert normalized[0]["tool_name"] == "find_docs"
        # ``min_count`` was not provided; the validator does not inject
        # a default so the engine reads ``criterion.get("min_count", 1)``.
        assert "min_count" not in normalized[0]

    def test_tool_call_succeeded_with_min_count_accepted(self) -> None:
        criteria = [
            {
                "criterion_id": "find_docs_three_times",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
                "min_count": 3,
            }
        ]
        normalized = validation.validate_criteria(criteria)
        assert normalized[0]["min_count"] == 3

    def test_tool_call_succeeded_missing_tool_name_rejected(self) -> None:
        criteria = [
            {
                "criterion_id": "x",
                "kind": "tool_call_succeeded",
                "required": True,
            }
        ]
        _expect_validation_error(
            lambda: validation.validate_criteria(criteria),
            field="criteria",
            reason="tool_name_missing_or_invalid",
        )

    def test_tool_call_succeeded_empty_tool_name_rejected(self) -> None:
        criteria = [
            {
                "criterion_id": "x",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "",
            }
        ]
        _expect_validation_error(
            lambda: validation.validate_criteria(criteria),
            field="criteria",
            reason="tool_name_missing_or_invalid",
        )

    @pytest.mark.parametrize("bad_count", [0, -1, 1.5, "1", True, False])
    def test_tool_call_succeeded_invalid_min_count_rejected(self, bad_count: object) -> None:
        criteria = [
            {
                "criterion_id": "x",
                "kind": "tool_call_succeeded",
                "required": True,
                "tool_name": "find_docs",
                "min_count": bad_count,
            }
        ]
        _expect_validation_error(
            lambda: validation.validate_criteria(criteria),
            field="criteria",
            reason="min_count_must_be_positive_int",
        )


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


# A registry where one tool is tagged cost-incurring and another is not.
# Both validate_budget and validate_tool_allowlist use this shape.
_REGISTERED_TAGS: dict[str, set[str]] = {
    "tool.cheap": {"read"},
    "tool.expensive": {"cost-incurring"},
    "tool.image": {"image"},
    "tool.upload": {"data-upload"},
    "tool.infra": {"infrastructure"},
}


class TestValidateBudget:
    """Budget caps must be positive ints. Cost guardrails live out-of-band."""

    @given(
        # Non-positive ints, bools (which the validator excludes from the
        # int test on purpose), strings, and floats — everything the
        # validator must reject for max_iterations. Excludes ``-1``
        # explicitly because the validator now accepts it as the
        # "uncapped" sentinel; every other negative or zero is still
        # invalid.
        bad_iterations=st.one_of(
            st.integers(max_value=-2),
            st.just(0),
            st.booleans(),
            st.text(min_size=0, max_size=4),
            st.floats(allow_nan=False, allow_infinity=False),
            st.none(),
        ),
    )
    @_PBT_SETTINGS
    def test_max_iterations_must_be_positive_int(self, bad_iterations: object) -> None:
        budget: dict[str, Any] = {
            "max_iterations": bad_iterations,
            "max_wall_clock_seconds": 60,
        }
        err = _expect_validation_error(
            lambda: validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS),
            field="budget",
            reason="missing_or_not_positive_int_or_minus_one",
        )
        assert err.details is not None
        assert err.details["subfield"] == "max_iterations"

    @given(
        bad_wall=st.one_of(
            st.integers(max_value=-2),
            st.just(0),
            st.booleans(),
            st.text(min_size=0, max_size=4),
            st.none(),
        ),
    )
    @_PBT_SETTINGS
    def test_max_wall_clock_must_be_positive_int(self, bad_wall: object) -> None:
        budget: dict[str, Any] = {
            "max_iterations": 10,
            "max_wall_clock_seconds": bad_wall,
        }
        err = _expect_validation_error(
            lambda: validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS),
            field="budget",
            reason="missing_or_not_positive_int_or_minus_one",
        )
        assert err.details is not None
        assert err.details["subfield"] == "max_wall_clock_seconds"

    def test_well_formed_budget_returns_normalized(self) -> None:
        budget = {"max_iterations": 5, "max_wall_clock_seconds": 60}
        result = validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS)
        assert result == {"max_iterations": 5, "max_wall_clock_seconds": 60}

    def test_uncapped_iterations_accepted(self) -> None:
        """``max_iterations=-1`` is accepted as the explicit uncapped sentinel."""
        budget = {"max_iterations": -1, "max_wall_clock_seconds": 60}
        result = validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS)
        assert result == {"max_iterations": -1, "max_wall_clock_seconds": 60}

    def test_uncapped_wall_clock_accepted(self) -> None:
        """``max_wall_clock_seconds=-1`` is accepted as the explicit uncapped sentinel."""
        budget = {"max_iterations": 5, "max_wall_clock_seconds": -1}
        result = validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS)
        assert result == {"max_iterations": 5, "max_wall_clock_seconds": -1}

    def test_both_uncapped_accepted(self) -> None:
        """Both caps being ``-1`` is allowed — the operator's informed-consent opt-out.

        Mission treats double-uncapped as an explicit configuration:
        the loop runs until a Criterion satisfies completion, the
        operator aborts, or — for scripted strategies — the sandbox
        cap fires. Validation is deliberately permissive here so the
        operator can drive a session purely by Criterion semantics.
        """
        budget = {"max_iterations": -1, "max_wall_clock_seconds": -1}
        result = validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS)
        assert result == {"max_iterations": -1, "max_wall_clock_seconds": -1}

    def test_zero_iterations_rejected(self) -> None:
        """``max_iterations=0`` is rejected — only ``-1`` is the special sentinel."""
        budget = {"max_iterations": 0, "max_wall_clock_seconds": 60}
        err = _expect_validation_error(
            lambda: validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS),
            field="budget",
            reason="missing_or_not_positive_int_or_minus_one",
        )
        assert err.details is not None
        assert err.details["subfield"] == "max_iterations"

    def test_negative_other_than_minus_one_rejected(self) -> None:
        """Any negative other than ``-1`` is rejected — typo guard."""
        budget = {"max_iterations": -2, "max_wall_clock_seconds": 60}
        err = _expect_validation_error(
            lambda: validation.validate_budget(budget, ["tool.cheap"], _REGISTERED_TAGS),
            field="budget",
            reason="missing_or_not_positive_int_or_minus_one",
        )
        assert err.details is not None
        assert err.details["subfield"] == "max_iterations"

    def test_extra_keys_are_ignored(self) -> None:
        """Operator-supplied extra keys (e.g. legacy max_cost_usd) are dropped silently."""
        budget = {
            "max_iterations": 5,
            "max_wall_clock_seconds": 60,
            "max_cost_usd": 1.50,
        }
        result = validation.validate_budget(budget, ["tool.expensive"], _REGISTERED_TAGS)
        # Only the two real caps land in the normalized output; the
        # legacy cost field (kept on operator-supplied dicts during the
        # post-rip-out transition) is ignored.
        assert result == {"max_iterations": 5, "max_wall_clock_seconds": 60}


# ---------------------------------------------------------------------------
# Tool allowlist
# ---------------------------------------------------------------------------


class TestValidateToolAllowlist:
    """Every name in the allowlist must currently be registered."""

    @given(
        # Names that are not in the registered_tools mapping — drawn from
        # an alphabet that won't collide with the canonical fixture keys.
        unknown_name=st.text(
            alphabet=st.characters(min_codepoint=65, max_codepoint=90),
            min_size=1,
            max_size=8,
        ).filter(lambda s: s not in _REGISTERED_TAGS),
    )
    @_PBT_SETTINGS
    def test_unknown_tool_rejected(self, unknown_name: str) -> None:
        err = _expect_validation_error(
            lambda: validation.validate_tool_allowlist(
                [unknown_name], dict.fromkeys(_REGISTERED_TAGS, object())
            ),
            field="tool_allowlist",
            reason="tool_not_registered",
        )
        assert err.details is not None
        assert err.details["tool_name"] == unknown_name
        # No flag was passed, so the gating-flag hint is omitted.
        assert "flag" not in err.details

    def test_unknown_tool_with_flag_lookup_carries_flag_hint(self) -> None:
        registered = dict.fromkeys(_REGISTERED_TAGS, object())
        flag_lookup = {"tool.future": "GCO_ENABLE_FUTURE"}
        err = _expect_validation_error(
            lambda: validation.validate_tool_allowlist(
                ["tool.future"], registered, flag_lookup=flag_lookup
            ),
            field="tool_allowlist",
            reason="tool_not_registered",
        )
        # When the caller supplies a flag_lookup mapping, the gating
        # flag name is surfaced so the operator sees *why* the tool is
        # currently absent from the catalog.
        assert err.details is not None
        assert err.details["flag"] == "GCO_ENABLE_FUTURE"

    @given(name=_ID_ALPHABET)
    @_PBT_SETTINGS
    def test_duplicate_tool_name_rejected(self, name: str) -> None:
        registered = {name: object()}
        _expect_validation_error(
            lambda: validation.validate_tool_allowlist([name, name], registered),
            field="tool_allowlist",
            reason="duplicate_tool_name",
        )

    def test_empty_allowlist_rejected(self) -> None:
        _expect_validation_error(
            lambda: validation.validate_tool_allowlist(
                [], dict.fromkeys(_REGISTERED_TAGS, object())
            ),
            field="tool_allowlist",
            reason="empty",
        )

    def test_well_formed_allowlist_returns_list_unchanged(self) -> None:
        registered = dict.fromkeys(_REGISTERED_TAGS, object())
        result = validation.validate_tool_allowlist(["tool.cheap", "tool.expensive"], registered)
        assert result == ["tool.cheap", "tool.expensive"]


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


class TestValidateCadence:
    """Each cadence kind has its own required companion keys."""

    @given(
        # ``every_n_iterations`` requires a positive int ``n``.
        bad_n=st.one_of(
            st.integers(max_value=0),
            st.booleans(),
            st.text(min_size=0, max_size=4),
            st.none(),
        ),
    )
    @_PBT_SETTINGS
    def test_every_n_iterations_missing_or_invalid_n_rejected(self, bad_n: object) -> None:
        cadence: dict[str, Any] = {"kind": "every_n_iterations"}
        if bad_n is not None:
            cadence["n"] = bad_n
        err = _expect_validation_error(
            lambda: validation.validate_cadence(cadence),
            field="checkpoint_cadence",
            reason="missing_or_not_positive_int",
        )
        assert err.details is not None
        assert err.details["subfield"] == "n"

    @given(
        bad_t=st.one_of(
            st.integers(max_value=0),
            st.booleans(),
            st.text(min_size=0, max_size=4),
            st.none(),
        ),
    )
    @_PBT_SETTINGS
    def test_every_t_seconds_missing_or_invalid_t_rejected(self, bad_t: object) -> None:
        cadence: dict[str, Any] = {"kind": "every_t_seconds"}
        if bad_t is not None:
            cadence["t"] = bad_t
        err = _expect_validation_error(
            lambda: validation.validate_cadence(cadence),
            field="checkpoint_cadence",
            reason="missing_or_not_positive_int",
        )
        assert err.details is not None
        assert err.details["subfield"] == "t"

    @given(
        bad_event=st.one_of(
            st.just(""),
            st.none(),
            st.integers(),
            st.lists(st.text(), max_size=2),
        ),
    )
    @_PBT_SETTINGS
    def test_on_event_missing_or_empty_event_name_rejected(self, bad_event: object) -> None:
        cadence: dict[str, Any] = {"kind": "on_event"}
        if bad_event is not None:
            cadence["event_name"] = bad_event
        _expect_validation_error(
            lambda: validation.validate_cadence(cadence),
            field="checkpoint_cadence",
            reason="missing_or_empty",
        )

    @given(
        bad_kind=st.one_of(
            st.text(min_size=0, max_size=8).filter(
                lambda s: (
                    s
                    not in {
                        "every_iteration",
                        "every_n_iterations",
                        "every_t_seconds",
                        "on_event",
                    }
                )
            ),
            st.none(),
            st.integers(),
        ),
    )
    @_PBT_SETTINGS
    def test_unknown_cadence_kind_rejected(self, bad_kind: object) -> None:
        cadence: dict[str, Any] = {"kind": bad_kind}
        _expect_validation_error(
            lambda: validation.validate_cadence(cadence),
            field="checkpoint_cadence",
            reason="kind_invalid",
        )

    def test_every_iteration_cadence_returns_normalized(self) -> None:
        result = validation.validate_cadence({"kind": "every_iteration"})
        assert result == {"kind": "every_iteration"}

    def test_every_n_iterations_well_formed_returns_normalized(self) -> None:
        result = validation.validate_cadence({"kind": "every_n_iterations", "n": 3})
        assert result == {"kind": "every_n_iterations", "n": 3}

    def test_on_event_well_formed_returns_normalized(self) -> None:
        result = validation.validate_cadence({"kind": "on_event", "event_name": "checkpoint_ready"})
        assert result == {"kind": "on_event", "event_name": "checkpoint_ready"}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class TestValidateStrategy:
    """Exactly one of ``tool_calls`` or ``script`` is present."""

    @given(
        # A strategy with both tool_calls and script — must be rejected
        # regardless of their content.
        tool_call=st.fixed_dictionaries({"tool_name": st.just("tool.cheap")}),
        script=st.text(min_size=1, max_size=16),
    )
    @_PBT_SETTINGS
    def test_strategy_with_both_tool_calls_and_script_rejected(
        self, tool_call: dict[str, Any], script: str
    ) -> None:
        strategy = {"tool_calls": [tool_call], "script": script}
        _expect_validation_error(
            lambda: validation.validate_strategy(strategy, ["tool.cheap"], allow_scripts=True),
            field="strategy",
            reason="must_have_exactly_one_of_tool_calls_or_script",
        )

    def test_strategy_with_neither_rejected(self) -> None:
        _expect_validation_error(
            lambda: validation.validate_strategy({}, ["tool.cheap"], allow_scripts=False),
            field="strategy",
            reason="must_have_exactly_one_of_tool_calls_or_script",
        )

    @given(
        empty_calls=st.one_of(
            st.just([]),
            st.text(),
            st.integers(),
        ),
    )
    @_PBT_SETTINGS
    def test_tool_calls_must_be_non_empty_list(self, empty_calls: object) -> None:
        strategy = {"tool_calls": empty_calls}
        err = _expect_validation_error(
            lambda: validation.validate_strategy(strategy, ["tool.cheap"], allow_scripts=False),
            field="strategy",
            reason="must_be_non_empty_list",
        )
        assert err.details is not None
        assert err.details["subfield"] == "tool_calls"

    def test_script_strategy_with_allow_scripts_false_rejected(self) -> None:
        err = _expect_validation_error(
            lambda: validation.validate_strategy(
                {"script": "print('hi')"},
                ["tool.cheap"],
                allow_scripts=False,
            ),
            field="strategy",
            reason="scripts_not_allowed_by_session",
        )
        assert err.details is not None
        assert err.details["subfield"] == "script"

    def test_script_strategy_with_allow_scripts_true_routes_to_sandbox(self) -> None:
        """When ``allow_scripts=True`` and a script is supplied, the validator
        hands the source to the sandbox AST validator. Any rejection from
        the sandbox layer (e.g. a script that uses an off-allowlist name
        like ``print``) is translated back into a structured
        ``MissionValidationError`` with the sandbox's stable ``reason``
        token in ``details``.
        """
        with pytest.raises(MissionValidationError) as exc_info:
            validation.validate_strategy(
                # ``print`` is not in the script sandbox's safe-builtin set
                # and is not in the supplied tool allowlist, so the AST
                # validator rejects this on the bare-name lookup.
                {"script": "print('hi')"},
                ["tool.cheap"],
                allow_scripts=True,
            )
        err = exc_info.value
        assert err.code == "validation_error"
        assert err.details is not None
        assert err.details["field"] == "strategy"
        assert err.details["subfield"] == "script"
        # The exact reason comes from the sandbox layer; ``name_not_allowed``
        # is the token the script validator uses for off-allowlist names.
        assert err.details["reason"] == "name_not_allowed"

    @given(
        bad_keys=st.one_of(
            st.text(),
            st.integers(),
            st.lists(st.integers(), min_size=1, max_size=3),
        ),
    )
    @_PBT_SETTINGS
    def test_expected_observation_keys_must_be_list_of_strings(self, bad_keys: object) -> None:
        strategy = {
            "tool_calls": [{"tool_name": "tool.cheap"}],
            "expected_observation_keys": bad_keys,
        }
        err = _expect_validation_error(
            lambda: validation.validate_strategy(strategy, ["tool.cheap"], allow_scripts=False),
            field="strategy",
            reason="must_be_list_of_strings",
        )
        assert err.details is not None
        assert err.details["subfield"] == "expected_observation_keys"

    @given(
        # A simple constrained strategy: a non-empty list of dict-shaped
        # tool calls. The validator returns a copy of the calls list,
        # carrying through the optional rationale field unchanged.
        tool_calls=st.lists(
            st.fixed_dictionaries({"tool_name": st.just("tool.cheap")}),
            min_size=1,
            max_size=3,
        ),
        rationale=st.text(min_size=0, max_size=32),
    )
    @_PBT_SETTINGS
    def test_well_formed_tool_calls_strategy_returns_normalized(
        self, tool_calls: list[dict[str, Any]], rationale: str
    ) -> None:
        strategy = {"tool_calls": tool_calls, "rationale": rationale}
        result = validation.validate_strategy(strategy, ["tool.cheap"], allow_scripts=False)
        assert result["tool_calls"] == tool_calls
        # The validator shallow-copies each call so the caller's dicts
        # stay isolated from the normalised output.
        assert result["tool_calls"] is not strategy["tool_calls"]
        assert result["rationale"] == rationale
        # ``script`` must not leak into the normalised shape.
        assert "script" not in result

    def test_non_dict_strategy_rejected(self) -> None:
        _expect_validation_error(
            lambda: validation.validate_strategy(
                "not a dict",  # type: ignore[arg-type]
                ["tool.cheap"],
                allow_scripts=False,
            ),
            field="strategy",
            reason="not_a_dict",
        )


class TestStripPrivateFields:
    """The canonical ``_parsed_ast``-stripping helpers in validation.py.

    Three places in the tree previously had their own near-duplicate
    implementations (``cli/commands/mission_cmd.py::_strip_private_criteria``,
    ``gco_mcp/tools/mission.py::_strip_private_fields`` plus its iterations
    variant, ``gco_mcp/resources/mission.py::_strip_private_fields``). They
    now delegate to the canonical helpers under
    :mod:`mcp.mission.validation` so a single source of truth governs
    the JSON-safety contract. These tests pin that contract.
    """

    def test_strip_returns_shallow_copy_not_mutating_input(self) -> None:
        """The original session and criteria are never mutated."""
        from mission.validation import strip_private_fields

        original = {
            "session_id": "s",
            "criteria": [
                {"criterion_id": "c1", "kind": "predicate", "_parsed_ast": object()},
            ],
        }
        cleaned = strip_private_fields(original)
        # The cleaned version dropped the private key.
        assert "_parsed_ast" not in cleaned["criteria"][0]
        # The original still has it — the helper never mutates input.
        assert "_parsed_ast" in original["criteria"][0]
        # Different list instance (shallow copy).
        assert cleaned["criteria"] is not original["criteria"]

    def test_strip_drops_every_leading_underscore_key(self) -> None:
        """Every leading-underscore key is dropped, not just ``_parsed_ast``.

        The strip rule is intentionally broad so a future cache (a
        normalised JSON-Pointer for the metric path, a pre-resolved
        tool-tag set) can ride on the same convention without
        breaking persistence.
        """
        from mission.validation import strip_private_fields

        session = {
            "criteria": [
                {
                    "criterion_id": "c1",
                    "kind": "predicate",
                    "_parsed_ast": object(),
                    "_other_cache": "something",
                    "_meta": {"nested": "still-dropped"},
                }
            ],
        }
        cleaned = strip_private_fields(session)
        crit = cleaned["criteria"][0]
        assert crit == {"criterion_id": "c1", "kind": "predicate"}

    def test_strip_handles_empty_criteria_and_iterations(self) -> None:
        """Empty / missing criteria / iterations don't trip the strip."""
        from mission.validation import strip_private_fields

        # Empty list — no crash.
        cleaned = strip_private_fields({"criteria": [], "iterations": []})
        assert cleaned == {"criteria": [], "iterations": []}
        # Missing keys — no crash.
        cleaned = strip_private_fields({"session_id": "s"})
        assert cleaned == {"session_id": "s"}

    def test_strip_handles_non_dict_criterion_entries(self) -> None:
        """Defensive: corrupt non-dict entries pass through verbatim."""
        from mission.validation import strip_private_fields

        cleaned = strip_private_fields({"criteria": ["not_a_dict", 42, None]})
        assert cleaned["criteria"] == ["not_a_dict", 42, None]

    def test_strip_walks_iterations_criteria_evaluation(self) -> None:
        """``iterations[*].criteria_evaluation[*]._parsed_ast`` is dropped."""
        from mission.validation import strip_private_fields

        session = {
            "iterations": [
                {
                    "iteration_index": 0,
                    "criteria_evaluation": [
                        {"criterion_id": "p1", "status": "met", "_parsed_ast": object()},
                    ],
                },
            ],
        }
        cleaned = strip_private_fields(session)
        eval_entries = cleaned["iterations"][0]["criteria_evaluation"]
        assert eval_entries == [{"criterion_id": "p1", "status": "met"}]

    def test_strip_iterations_variant_handles_non_dict_entries(self) -> None:
        """The iterations variant tolerates non-dict entries (corruption surfaces)."""
        from mission.validation import strip_private_fields_iterations

        out = strip_private_fields_iterations(["corrupt-string-entry", {"iteration_index": 0}])
        assert out[0] == "corrupt-string-entry"
        assert out[1]["iteration_index"] == 0

    def test_strip_idempotent(self) -> None:
        """Running ``strip_private_fields`` twice gives the same result.

        The strip is the persistence layer's last defence; saving an
        already-clean session should not be a no-cost-but-different
        operation. Pin idempotency so a future change that re-attaches
        a private key on the second pass is caught here.
        """
        from mission.validation import strip_private_fields

        session = {
            "criteria": [
                {"criterion_id": "c1", "_parsed_ast": object(), "kind": "predicate"},
            ],
        }
        once = strip_private_fields(session)
        twice = strip_private_fields(once)
        assert once == twice
