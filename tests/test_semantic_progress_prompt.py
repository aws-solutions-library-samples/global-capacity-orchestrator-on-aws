"""Tests for the semantic-progress judge's deterministic prompt builder.

Building the request the judge sends to the model is the pure half of the
flow: given a directive, some optional recent progress context, and a rubric
version, the builder must always produce the same bytes. Only the model's
answer is allowed to vary. These tests pin that guarantee, plus the two ways
the context is shaped before it goes in:

* the incorporated context is bounded to a fixed character budget by keeping
  the most recent tail and dropping the oldest head, so a long history can
  never grow the prompt without limit; and
* when no context is supplied the prompt is built from the directive alone.

The property tests drive the builder across ordinary, empty/absent, and
oversized context, asserting two independent builds render byte-for-byte
identically, the folded-in context never exceeds the budget, oversized
context keeps its newest tail behind a truncation marker, and an
absent/empty context leaves the context section empty while the directive
still appears in the rendered prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``mcp/run_mcp.py`` puts ``mcp/`` on ``sys.path`` at runtime; mirror that
# here so the pure ``mission_judge`` package imports the same way it does in
# production, matching the convention used by the sibling Mission tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission_judge.prompt import (  # noqa: E402
    MAX_CONTEXT_CHARS,
    TRUNCATION_MARKER,
    JudgePrompt,
    build_prompt,
    truncate_context,
)
from mission_judge.rubric import RUBRIC_VERSION  # noqa: E402

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A non-empty directive. Kept non-empty so the "directive appears in the
# rendered prompt" assertion is meaningful rather than vacuously true.
_directives = st.text(min_size=1, max_size=512)

# Ordinary context comfortably within the character budget: returned verbatim
# by the truncation step, so the builder folds it in unchanged.
_in_budget_context = st.text(max_size=512)

# The absent and empty cases, which both collapse to an empty context section.
_absent_context = st.sampled_from([None, ""])

# Context deliberately past the budget, exercising the keep-newest truncation:
# anything beyond the ceiling must be dropped oldest-first.
_oversized_context = st.text(min_size=MAX_CONTEXT_CHARS + 1, max_size=MAX_CONTEXT_CHARS + 500)

# The union spanning every shape of context the builder must handle.
_any_context = st.one_of(_absent_context, _in_budget_context, _oversized_context)


# ---------------------------------------------------------------------------
# Property: identical inputs render a byte-identical, budget-bounded prompt
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(directive=_directives, recent_context=_any_context)
def test_prompt_is_a_deterministic_bounded_function_of_its_inputs(
    directive: str,
    recent_context: str | None,
) -> None:
    """Two independent builds of the same inputs render identical, bounded bytes.

    Across ordinary, absent/empty, and oversized context, for the same
    directive, context, and fixed rubric version:

    * two independent ``build_prompt(...).assemble()`` calls produce a
      byte-identical string, so the rendered prompt carries no clock, random,
      or ambient content;
    * the context folded into the prompt never exceeds the character budget,
      whatever the size of the supplied input; and
    * the directive always appears verbatim in the rendered prompt.
    """
    first = build_prompt(directive, recent_context, RUBRIC_VERSION)
    second = build_prompt(directive, recent_context, RUBRIC_VERSION)

    # The builder returns the frozen prompt bundle, and two builds of the same
    # inputs are equal both as objects and as rendered bytes.
    assert isinstance(first, JudgePrompt)
    assert first == second
    assert first.assemble() == second.assemble()

    # The incorporated context is bounded, regardless of input size.
    assert len(first.context) <= MAX_CONTEXT_CHARS

    # The directive is carried into the rendered prompt verbatim.
    assert directive in first.assemble()


# ---------------------------------------------------------------------------
# Property: an absent or empty context yields a directive-only prompt
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(directive=_directives, recent_context=_absent_context)
def test_absent_or_empty_context_builds_a_directive_only_prompt(
    directive: str,
    recent_context: str | None,
) -> None:
    """When no context is supplied the context section is empty but the directive remains.

    Both an absent (``None``) and an empty (``""``) context collapse to an
    empty context section, leaving the judgement to proceed from the directive
    alone, which still appears in the rendered prompt.
    """
    prompt = build_prompt(directive, recent_context, RUBRIC_VERSION)

    assert prompt.context == ""
    assert directive in prompt.assemble()


# ---------------------------------------------------------------------------
# Property: oversized context is truncated keep-newest
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.large_base_example,
    ],
)
@given(directive=_directives, recent_context=_oversized_context)
def test_oversized_context_keeps_the_newest_tail_and_drops_the_oldest_head(
    directive: str,
    recent_context: str,
) -> None:
    """Context past the budget keeps its most recent tail behind a truncation marker.

    For context longer than the character budget:

    * the folded-in context is bounded by the budget;
    * it opens with the truncation marker, signalling older content was
      dropped;
    * the retained portion after the marker is a proper suffix of the original
      input — the newest characters survive (keep-newest) while the oldest head
      is discarded; and
    * the builder's context matches the standalone truncation helper, which is
      itself deterministic for identical oversized inputs.
    """
    prompt = build_prompt(directive, recent_context, RUBRIC_VERSION)
    context = prompt.context

    # Still within budget after truncation.
    assert len(context) <= MAX_CONTEXT_CHARS

    # Truncation occurred, so the marker leads the retained content.
    assert context.startswith(TRUNCATION_MARKER)

    # The content kept after the marker is the newest tail of the original:
    # a proper suffix, with the older head discarded.
    retained_tail = context[len(TRUNCATION_MARKER) :]
    assert recent_context.endswith(retained_tail)
    assert len(retained_tail) < len(recent_context)

    # The builder defers to the standalone truncation helper, which is
    # deterministic for identical oversized inputs.
    assert context == truncate_context(recent_context)
    assert truncate_context(recent_context) == truncate_context(recent_context)
