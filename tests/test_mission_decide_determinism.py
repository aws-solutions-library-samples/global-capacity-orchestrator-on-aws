"""Determinism property tests for the Mission verdict cascade.

The verdict cascade in ``mcp/mission/decide.py`` is the **control path**
of the loop: given the persisted ``SessionState``, the in-progress
``IterationRecord``, and a wall-clock value, it returns a
``(VerdictLabel, VerdictReason)`` tuple. Two universal properties must
hold and are pinned down here:

* **Property 1 — control-path determinism.** For every well-formed
  ``(session, iteration, now)`` triple, calling ``decide_verdict``
  twice must return equal tuples. The cascade reads no clocks (``now``
  is on the call signature), no globals, no random sources — so
  identical inputs must produce identical outputs.

* **Property 2 — sampling cannot mutate the control path.** The
  cascade may not consult any field whose value is the output of an
  LLM sampler. The persistent record of those outputs lives under
  ``iteration["sampling_output"]`` on prior iterations. Mutating
  those strings to three different values must leave the verdict
  tuple unchanged. A stricter variant of the same property cycles
  the in-progress iteration through five distinct sampler-mode
  profiles — covering the no-sampler, MCP-success-strategy-A,
  Bedrock-success-strategy-B, MCP-rejected, and Bedrock-no-credentials
  modes — and asserts the verdict stays put.

Hypothesis budget: ``max_examples=50, deadline=2000`` keeps the file's
wall-clock under five seconds even on the slowest CI runner. The
shared strategies live in ``tests/strategies/mission.py`` so other
slices (engine tests, audit reconstruction tests, sampling tests) can
reuse them.

Validates: Property 1 (Control-path determinism), Property 2
(Sampling cannot mutate the control path).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Mirror the import pattern used by every other Mission test:
# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime, but
# pytest has to do it itself before the import below resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

from mission.decide import decide_verdict  # noqa: E402

from tests.strategies.mission import (  # noqa: E402
    decide_verdict_inputs,
    iteration_records,
    session_states,
)

# Shared budget: 50 examples per property, 2 second per-example deadline.
# The ``too_slow`` health check is suppressed because Hypothesis's
# generator setup for the composite ``decide_verdict_inputs`` strategy
# can take ~50 ms on the first draw under coverage; the per-example
# deadline still enforces the wall-clock cap.
_PBT_SETTINGS = settings(
    max_examples=50,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Property 1 — control-path determinism
# ---------------------------------------------------------------------------


class TestVerdictDeterminism:
    """``decide_verdict`` returns the same tuple on repeat calls.

    The cascade is documented as pure (no I/O, no random source, no
    clock read), so calling it twice with the same inputs must yield
    equal outputs. A regression that introduced any non-determinism —
    a stray ``datetime.now()`` call, a dict iteration that depended on
    insertion order across runs, a memoization cache that cleared
    between calls — would surface here.
    """

    @given(triple=decide_verdict_inputs())
    @_PBT_SETTINGS
    def test_decide_verdict_deterministic(
        self,
        triple: tuple[dict[str, Any], dict[str, Any], datetime],
    ) -> None:
        """Calling ``decide_verdict`` twice on the same inputs returns equal tuples.

        Validates: Property 1 (Control-path determinism).
        """
        session, iteration, now = triple
        first = decide_verdict(session, iteration, now)
        second = decide_verdict(session, iteration, now)
        assert first == second


# ---------------------------------------------------------------------------
# Property 2 — sampling cannot mutate the control path
# ---------------------------------------------------------------------------


class TestVerdictSamplingIndependence:
    """The verdict tuple does not depend on prior iterations' sampling output.

    The cascade may consult any deterministic field on prior iterations
    (the strategy's ``tool_calls``, the observation's ``errors``, the
    criteria evaluation's ``status``) but it must not consult anything
    whose value is the output of an LLM sampler. The sampling-output
    strings live under ``iteration["sampling_output"]`` on each prior
    iteration; mutating them to three different values must leave the
    verdict unchanged.
    """

    @given(
        # Force at least one prior iteration so there is a record to
        # mutate. Three suffices to demonstrate the property.
        session=session_states(min_prior_iterations=1, max_prior_iterations=3),
        in_progress=iteration_records(),
        now=st.datetimes(
            min_value=datetime(2025, 1, 1),
            max_value=datetime(2030, 1, 1),
            timezones=st.just(UTC),
        ),
        sampling_outputs=st.tuples(
            st.text(max_size=64),
            st.text(max_size=64),
            st.text(max_size=64),
        ),
    )
    @_PBT_SETTINGS
    def test_verdict_independent_of_sampling_history(
        self,
        session: dict[str, Any],
        in_progress: dict[str, Any],
        now: datetime,
        sampling_outputs: tuple[str, str, str],
    ) -> None:
        """Three different ``sampling_output`` values yield the same verdict.

        For the same ``(session, iteration, now)``, mutate
        ``session["iterations"][-1]["sampling_output"]`` to three
        distinct strings in turn and assert the verdict tuple is
        identical across all three runs.

        Validates: Property 2 (Sampling cannot mutate the control path).
        """
        # Re-align the in-progress iteration's criteria_evaluation to
        # the session's criteria so the cascade exercises every branch
        # of the completion check rather than always falling through
        # on a missing-id mismatch. The per-iteration evaluation
        # entries are draw-time-aligned to a separate criteria draw,
        # so we re-project them onto the session's ids here.
        session_ids = [c["criterion_id"] for c in session["criteria"]]
        existing_results = list(in_progress["criteria_evaluation"])
        rebuilt: list[dict[str, Any]] = []
        for index, cid in enumerate(session_ids):
            if index < len(existing_results):
                rebuilt.append(dict(existing_results[index], criterion_id=cid))
            else:
                rebuilt.append(
                    {
                        "criterion_id": cid,
                        "status": "unmet",
                        "evidence": None,
                        "evaluated_at": in_progress["ended_at"],
                    }
                )
        in_progress = dict(in_progress, criteria_evaluation=rebuilt)

        verdicts: list[tuple[str, str]] = []
        for output in sampling_outputs:
            session["iterations"][-1]["sampling_output"] = output
            verdicts.append(decide_verdict(session, in_progress, now))

        # Every entry must equal the first; the verdict label and the
        # verdict reason must both be invariant under sampling-output
        # mutation.
        assert verdicts[0] == verdicts[1] == verdicts[2], (
            f"verdict changed under sampling-output mutation: {verdicts!r}"
        )


# ---------------------------------------------------------------------------
# Property 2 (stricter) — full sampler-mode profile cycling
# ---------------------------------------------------------------------------


class TestVerdictUnaffectedBySamplerMode:
    """The verdict tuple is invariant under sampler-mode swaps.

    Every Mission iteration carries four optional, sampler-driven
    metadata fields on its persisted record: ``sampling_status``,
    ``sampling_output``, ``sampling_rejection_reason``, and
    ``revision_rationale``. Together they capture which sampling
    backend (none, MCP, Bedrock) ran for that iteration, what JSON
    payload it returned, why the validator rejected the payload (when
    it did), and what rationale text the engine attached to the
    resulting Strategy.

    The cascade in ``decide_verdict`` is documented as reading none
    of these fields — they record what the LLM produced, not what the
    control path will do next. This test pins that contract by
    cycling the most-recent prior iteration through five distinct
    sampler-mode profiles and asserting the verdict tuple is
    identical across all five runs. The profiles together cover the
    full surface implied by the brief:

    * ``"no sampler"`` — the deterministic-fallback path. All four
      fields absent (status defaulted to ``"disabled"``).
    * ``"MCP success (Strategy A)"`` — MCP sampler returned a
      well-formed payload and the engine accepted it.
    * ``"Bedrock success (Strategy B)"`` — Bedrock Converse returned
      a different well-formed payload and the engine accepted it.
    * ``"MCP rejected"`` — sampler returned non-JSON; status flips
      to ``"rejected"`` with a ``json_parse`` rejection reason.
    * ``"Bedrock no credentials"`` — Bedrock backend raised at
      construction; status ``"rejected"`` with
      ``bedrock_no_credentials``.

    The strategy itself (the ``tool_calls`` list) is *not* mutated
    here — that field IS read by the heuristic via
    ``_strategy_unproductive``, and a sampler swap that altered the
    Strategy could legitimately change the verdict. This test
    isolates the metadata fields specifically: same Strategy, same
    Observation, same criteria evaluation — different sampler
    metadata. The verdict tuple must not move.
    """

    #: The five sampler-mode profiles cycled by the property test.
    #: Each entry is a ``(status, output, rejection_reason, rationale)``
    #: tuple; ``None`` means "remove the key from the iteration record"
    #: so the iteration shape matches the persisted-on-disk shape for
    #: a session whose sampler never ran.
    SAMPLER_MODES: tuple[tuple[str | None, str | None, str | None, str | None], ...] = (
        # No sampler — deterministic fallback only.
        ("disabled", None, None, None),
        # MCP returned a well-formed Strategy A payload.
        (
            "used",
            '{"revision_rationale": "MCP-A", "next_strategy": '
            '{"tool_calls": [{"tool_name": "tool_A", "args": {"p": "A"}}]}}',
            None,
            "MCP rationale for Strategy A",
        ),
        # Bedrock returned a well-formed Strategy B payload.
        (
            "used",
            '{"revision_rationale": "Bedrock-B", "next_strategy": '
            '{"tool_calls": [{"tool_name": "tool_B", "args": {"p": "B"}}]}}',
            None,
            "Bedrock rationale for Strategy B",
        ),
        # MCP returned garbage that failed JSON parse.
        ("rejected", "<<not-json>>", "json_parse", None),
        # Bedrock failed at construction (no credentials).
        ("rejected", "", "bedrock_no_credentials", None),
    )

    @given(
        # Force at least one prior iteration so there is a record to
        # mutate the metadata on. Three priors keeps the heuristic's
        # last-3 window populated without ballooning the draw size.
        session=session_states(min_prior_iterations=1, max_prior_iterations=3),
        in_progress=iteration_records(),
        now=st.datetimes(
            min_value=datetime(2025, 1, 1),
            max_value=datetime(2030, 1, 1),
            timezones=st.just(UTC),
        ),
    )
    @_PBT_SETTINGS
    def test_verdict_invariant_under_sampler_mode(
        self,
        session: dict[str, Any],
        in_progress: dict[str, Any],
        now: datetime,
    ) -> None:
        """Cycling the prior iteration's sampler metadata through five distinct profiles preserves the verdict.

        For each profile, mutate ``session["iterations"][-1]`` so its
        ``sampling_status`` / ``sampling_output`` /
        ``sampling_rejection_reason`` / ``revision_rationale`` fields
        match the profile (or are removed when the profile sets them
        to ``None``), then call ``decide_verdict`` and collect the
        result. After the loop, every collected tuple must equal the
        first.

        Validates: Property 2 (Sampling cannot mutate the control path).
        """
        # Re-align the in-progress iteration's criteria_evaluation to
        # the session's criteria so the cascade exercises every branch
        # of the completion check rather than always falling through
        # on a missing-id mismatch. Mirrors the helper used by the
        # sampling-output-only variant above so both tests draw from
        # the same shape distribution.
        session_ids = [c["criterion_id"] for c in session["criteria"]]
        existing_results = list(in_progress["criteria_evaluation"])
        rebuilt: list[dict[str, Any]] = []
        for index, cid in enumerate(session_ids):
            if index < len(existing_results):
                rebuilt.append(dict(existing_results[index], criterion_id=cid))
            else:
                rebuilt.append(
                    {
                        "criterion_id": cid,
                        "status": "unmet",
                        "evidence": None,
                        "evaluated_at": in_progress["ended_at"],
                    }
                )
        in_progress = dict(in_progress, criteria_evaluation=rebuilt)

        # Snapshot the prior iteration so each profile mutates from a
        # known clean baseline rather than compounding mutations.
        baseline_prior = dict(session["iterations"][-1])

        verdicts: list[tuple[str, str]] = []
        for status, output, rejection_reason, rationale in self.SAMPLER_MODES:
            mutated_prior = dict(baseline_prior)
            # Remove every sampler-metadata key first so absent-vs-set
            # transitions are observable across profile boundaries.
            for key in (
                "sampling_status",
                "sampling_output",
                "sampling_rejection_reason",
                "revision_rationale",
            ):
                mutated_prior.pop(key, None)
            if status is not None:
                mutated_prior["sampling_status"] = status
            if output is not None:
                mutated_prior["sampling_output"] = output
            if rejection_reason is not None:
                mutated_prior["sampling_rejection_reason"] = rejection_reason
            if rationale is not None:
                mutated_prior["revision_rationale"] = rationale

            session_variant = dict(session)
            session_variant["iterations"] = [
                *session["iterations"][:-1],
                mutated_prior,
            ]
            verdicts.append(decide_verdict(session_variant, in_progress, now))

        # Every entry must equal the first; the verdict label and the
        # verdict reason are both invariant under sampler-mode swaps.
        first = verdicts[0]
        assert all(v == first for v in verdicts), (
            f"verdict changed under sampler-mode mutation: {verdicts!r}"
        )


# ---------------------------------------------------------------------------
# Smoke test — guards against an empty Hypothesis run silently passing.
# ---------------------------------------------------------------------------


def test_decide_verdict_smoke_returns_tuple() -> None:
    """A hand-crafted session/iteration produces a real two-tuple.

    Hypothesis can occasionally configure a property strategy that
    discards every drawn example, in which case the property test
    technically passes (no failures observed) without exercising the
    code under test. This static smoke test backstops that case.
    """
    session: dict[str, Any] = {
        "version": 1,
        "session_id": "smoke",
        "directive_text": "smoke",
        "criteria": [],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 3600},
        "tool_allowlist": ["any_tool"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2025-06-01T00:00:00+00:00",
        "started_at": "2025-06-01T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
    }
    iteration: dict[str, Any] = {
        "iteration_index": 0,
        "started_at": "2025-06-01T00:00:01+00:00",
        "ended_at": "2025-06-01T00:00:02+00:00",
        "phases": [],
        "strategy": {"tool_calls": [{"tool_name": "any_tool", "args": {}}]},
        "observation": {
            "tool_results": [],
            "metrics": {},
            "events": [],
            "errors": [],
            "phase_started_at": "2025-06-01T00:00:01+00:00",
            "phase_ended_at": "2025-06-01T00:00:02+00:00",
        },
        "criteria_evaluation": [],
        "verdict": "continue",
        "verdict_reason": "in_progress",
        "checkpoint_evaluated": True,
    }
    now = datetime(2025, 6, 1, 0, 0, 5, tzinfo=UTC)
    verdict = decide_verdict(session, iteration, now)
    assert isinstance(verdict, tuple)
    assert len(verdict) == 2
    label, reason = verdict
    assert isinstance(label, str)
    assert isinstance(reason, str)


# Module-level marker so a stray ``pytest -k`` filter that excludes the
# property tests still gets a defensive backstop from the smoke test.
pytestmark: list[pytest.MarkDecorator] = []
