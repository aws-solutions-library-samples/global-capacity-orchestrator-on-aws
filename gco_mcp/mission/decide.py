"""Pure deterministic verdict cascade for the Mission Decide_Phase.

The cascade is the **control-path** output of the loop: given the current
``SessionState`` (before the in-progress iteration is appended), the
in-progress :class:`IterationRecord` (with ``strategy``, ``observation``,
and ``criteria_evaluation`` already populated but ``verdict`` /
``verdict_reason`` not yet set), and the wall-clock value the caller has
already measured, :func:`decide_verdict` returns a
``(VerdictLabel, VerdictReason)`` tuple. The function is pure: no logger
calls, no I/O, no random sources, no clock reads. The wall-clock value is
passed in on the call signature so tests can pin it.

The cascade order is fixed:

1. **Budget terminations** — checked in a fixed sub-order so the
   verdict_reason is deterministic when more than one cap is breached:

   * ``max_iterations`` — the in-progress iteration would be the
     ``budget["max_iterations"]``-th or later. Computed as
     ``len(session["iterations"]) + 1 >= max_iterations``.
   * ``max_wall_clock`` — ``now - session["started_at"] >= max_wall_clock_seconds``.
     Returns False when ``started_at`` is missing (the session has
     not yet transitioned out of ``pending``).
   * ``no_progress`` — ``no_progress_counter >= stagnation_threshold``.
     When the session has ``use_sampling=true``, the heuristic (step
     4) gets priority so the sampler can revise the strategy before
     the loop terminates. Without sampling, ``no_progress``
     terminates immediately. If the heuristic doesn't fire (e.g.,
     the tool sequence changed after a prior revision), the deferred
     stagnation check (step 4b) terminates.

2. **Completion** — every ``required=True`` Criterion has status
   ``met`` in the in-progress iteration's ``criteria_evaluation``, AND
   no Criterion (required or not) has status ``inconclusive``.

3. **Cadence-skip** — when :func:`should_evaluate_now` says "this is
   not a checkpoint", emit a synthetic ``("continue", "cadence_skip")``
   without consulting the Strategy_Revision_Heuristic. The heuristic
   only fires on real checkpoints so off-cadence iterations cannot
   advance the no-progress counter or trigger an ``adjust``.

4. **Strategy_Revision_Heuristic** — :func:`_strategy_unproductive`:
   the same ``tool_calls[*].tool_name`` sequence
   for the last 3 iterations AND ``no_progress_counter`` at or above
   half the stagnation threshold, OR new errors in the latest
   Observation that didn't appear in the prior Observation. Returns
   ``("adjust", "heuristic_unproductive")`` when either clause fires.

4b. **Deferred stagnation** — if step 1c deferred the ``no_progress``
    check (because sampling is enabled) and the heuristic didn't fire,
    terminate now.

5. **Default** — ``("continue", "in_progress")``.

The ``iteration`` argument is *not* yet present in
``session["iterations"]`` — the engine appends it after the verdict is
decided. Anything that needs to look at "the last N iterations
including the current one" composes the current ``iteration`` with
``session["iterations"][-(N-1):]``.

Determinism: same ``(session, iteration, now)`` triples produce the same
``(VerdictLabel, VerdictReason)`` tuples. This is enforced by a property
test in ``tests/test_mission_decide_determinism.py``.

Cost guardrails are intentionally absent from this cascade. Real-time
workload cost tracking is structurally inaccurate (Spot vs on-demand
drift, EBS / EFA / egress not in the Pricing API, Cost Explorer 24h
latency). Operators who need a cost cap should configure AWS Budgets
and Cost Anomaly Detection at the account level — Mission caps only
the controls the loop has direct visibility into.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from .checkpoints import should_evaluate_now
from .types import IterationRecord, SessionState, VerdictLabel, VerdictReason

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``decide_verdict`` -> ``diagrams/code_diagrams/gco_mcp/mission/decide.decide_verdict.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/mission/decide.decide_verdict.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


__all__ = [
    "build_revision_rationale_template",
    "decide_verdict",
]


def decide_verdict(
    session: SessionState,
    iteration: IterationRecord,
    now: datetime,
) -> tuple[VerdictLabel, VerdictReason]:
    """Return the deterministic Verdict for the in-progress iteration.

    The cascade order is fixed (see module docstring). The first matching
    branch wins: a session that has both run out of iterations and has
    every Criterion met returns ``("terminate", "max_iterations")``, not
    ``("complete", "criteria_met")`` — budget caps are evaluated before
    completion so the operator can tell the loop ended because it ran
    out of budget rather than because the goal was reached on the
    closing iteration.
    Note: When the prior Execute_Phase ran a scripted Strategy and the
    sandbox cap fired, ``_execute_script`` writes
    ``iteration["sandbox_terminated_reason"]`` and the cascade returns
    that reason verbatim before anything else is consulted. The
    sandbox limit is a true budget cap — the script ran out of wall
    clock during execution — so it routes to a ``terminate`` verdict
    on the same path as the ``BudgetControls``-driven caps below.
    """
    # 0. Sandbox-cap propagation. ``_execute_script`` stashes the
    # reason on the in-progress iteration when the sandbox runner
    # raised :class:`SandboxTerminated`. Reading the sentinel here
    # means the engine's Execute_Phase can complete cleanly (no phase
    # failure) while still routing the verdict to the budget-cap path.
    sandbox_reason = iteration.get("sandbox_terminated_reason")
    if sandbox_reason is not None:
        return ("terminate", sandbox_reason)

    # 1a. max_iterations — the +1 captures "this in-progress iteration
    # would be the Nth one to land", so a session with budget=N and N-1
    # already-recorded iterations terminates on the Nth's Decide_Phase.
    # ``-1`` is the explicit "uncapped" sentinel; the validator
    # already enforced that any other non-positive value is rejected.
    max_iter = session["budget"]["max_iterations"]
    if max_iter != -1 and len(session["iterations"]) + 1 >= max_iter:
        return ("terminate", "max_iterations")
    # 1b. max_wall_clock — pure time arithmetic; missing started_at
    # means the session has not yet recorded its first iteration's
    # start, so no wall-clock can be measured.
    if _wall_clock_exceeded(session, now):
        return ("terminate", "max_wall_clock")
    # 1c. no_progress — the counter is incremented by the engine only
    # on evaluated iterations, so a session with all-skipped checkpoints
    # cannot terminate for stagnation. When the session has sampling
    # enabled, the heuristic gets priority (step 4 below) so the
    # sampler can revise the strategy before the loop terminates.
    # Without sampling, ``adjust`` is purely informational and
    # ``no_progress`` terminates immediately.
    if session["no_progress_counter"] >= session["stagnation_threshold"]:
        if not session.get("use_sampling"):
            return ("terminate", "no_progress")
        # With sampling enabled, fall through to the heuristic check
        # below. If the heuristic fires, the sampler gets one more
        # chance. If it doesn't fire (e.g., the tool sequence changed
        # after a prior revision), terminate for stagnation.
        _stagnation_pending = True
    else:
        _stagnation_pending = False

    # 2. Completion — every required Criterion met AND nothing inconclusive.
    if _completion_satisfied(session, iteration):
        return ("complete", "criteria_met")

    # 3. Cadence-skip — bail before the heuristic fires so off-cadence
    # iterations don't ever produce ``adjust``. The iteration_index
    # passed to ``should_evaluate_now`` is the 0-indexed position of
    # the in-progress iteration (which equals the count of already-
    # persisted iterations).
    if not should_evaluate_now(session, len(session["iterations"]), now):
        return ("continue", "cadence_skip")

    # 4. Strategy_Revision_Heuristic.
    unproductive, _heuristic_reason = _strategy_unproductive(session, iteration)
    if unproductive:
        return ("adjust", "heuristic_unproductive")

    # 4b. Deferred stagnation — the counter hit the threshold but the
    # heuristic didn't fire (e.g., the tool sequence changed after a
    # prior sampled revision). Terminate now.
    if _stagnation_pending:
        return ("terminate", "no_progress")

    # 5. Default.
    return ("continue", "in_progress")


# ---------------------------------------------------------------------------
# Budget helpers — pure
# ---------------------------------------------------------------------------


def _wall_clock_exceeded(session: SessionState, now: datetime) -> bool:
    """True iff ``now - session["started_at"] >= max_wall_clock_seconds``.

    Returns False when ``started_at`` is absent — a session that has
    never been transitioned out of ``pending`` cannot have exceeded any
    wall-clock budget. The engine writes ``started_at`` on the first
    iteration entry, so this guard only matters for the synthetic
    "decide called before run_iteration" path used in unit tests.

    Returns False when ``max_wall_clock_seconds`` is the explicit
    ``-1`` "uncapped" sentinel — the operator opted out of the wall-
    clock cap and the cascade should fall through to the next branch
    rather than terminate spuriously.
    """
    started_iso = session.get("started_at")
    if not started_iso:
        return False
    max_seconds = session["budget"]["max_wall_clock_seconds"]
    if max_seconds == -1:
        return False
    started = datetime.fromisoformat(started_iso)
    return now - started >= timedelta(seconds=max_seconds)


# ---------------------------------------------------------------------------
# Completion check
# ---------------------------------------------------------------------------


def _completion_satisfied(
    session: SessionState,
    iteration: IterationRecord,
) -> bool:
    """True iff every required Criterion is met and none are inconclusive.

    A session completes when all Criteria with ``required=True`` have
    status ``met`` AND no Criterion (required or not) has status
    ``inconclusive``. The ``required`` flag lives on the Criterion
    declaration in ``session["criteria"]``; the per-iteration status
    lives on ``iteration["criteria_evaluation"]``. The two are joined
    by ``criterion_id``.

    A session with zero declared Criteria can never complete on its own
    — there are no required Criteria for the cascade to satisfy. The
    operator drives such a session to terminal via ``mission_complete``
    or a budget cap. We mirror that semantic here by returning False
    when the criteria list is empty.
    """
    if not session["criteria"]:
        return False
    required_by_id = {c["criterion_id"]: c.get("required", True) for c in session["criteria"]}
    for result in iteration["criteria_evaluation"]:
        status = result["status"]
        if status == "inconclusive":
            return False
        if required_by_id.get(result["criterion_id"], True) and status != "met":
            return False
    return True


# ---------------------------------------------------------------------------
# Strategy_Revision_Heuristic
# ---------------------------------------------------------------------------


def _strategy_unproductive(
    session: SessionState,
    iteration: IterationRecord,
) -> tuple[bool, str]:
    """Pure heuristic for the Strategy_Revision check.

    Two clauses, evaluated in declaration order. The first match wins
    so the returned reason is deterministic when both clauses fire.

    * **Clause (a)** — the same ``tool_calls[*].tool_name`` sequence
      has been used for the last 3 iterations (counting the in-progress
      one) AND ``no_progress_counter >= ceil(stagnation_threshold / 2)``.
      Needs at least 2 prior iterations to evaluate (3 total when the
      current iteration is included). A scripted strategy contributes
      an empty sequence so two scripts with the same body register as
      "same sequence" — that's intentional: the heuristic flags repeats,
      and an empty-sequence repeat across three iterations is a repeat.
    * **Clause (b)** — the in-progress Observation contains at least
      one ``errors`` entry that did not appear in the immediately
      prior Iteration's Observation. Needs at least 1 prior iteration
      to evaluate. Without a prior to compare to, "new" is undefined
      and we return False.

    Returns ``(False, "")`` when neither clause fires. When clause (a)
    fires, the reason is ``"tool_sequence_repeating"``; when clause (b)
    fires, ``"new_observation_errors"``. The reason string is
    informational only — the Verdict's ``verdict_reason`` is always
    ``"heuristic_unproductive"`` regardless of which clause matched.
    """
    # Clause (a): no_progress threshold AND tool-sequence repeat.
    threshold = session["stagnation_threshold"]
    half = math.ceil(threshold / 2)
    if session["no_progress_counter"] >= half:
        # Need at least 2 prior + the current = 3 total iterations.
        prior = session["iterations"]
        if len(prior) >= 2:
            recent_three = [prior[-2], prior[-1], iteration]
            sequences = [_tool_name_sequence(it) for it in recent_three]
            if sequences[0] == sequences[1] == sequences[2]:
                return (True, "tool_sequence_repeating")

    # Clause (b): new errors in the latest Observation vs the prior one.
    if session["iterations"]:
        prior_observation = session["iterations"][-1].get("observation") or {}
        prior_errors = list(prior_observation.get("errors") or [])
        current_errors = list(iteration["observation"].get("errors") or [])
        for err in current_errors:
            if err not in prior_errors:
                return (True, "new_observation_errors")

    return (False, "")


def _tool_name_sequence(iteration: IterationRecord) -> tuple[str, ...]:
    """Extract the ordered tuple of ``tool_name``s from an iteration's strategy.

    Returns an empty tuple when the strategy is a script (no
    ``tool_calls``) or when ``tool_calls`` is missing. Two scripted
    strategies therefore both produce ``()`` and compare equal — clause
    (a) treats that as "same sequence", which matches the operator's
    intent of flagging mechanical repetition regardless of mode.
    """
    strategy = iteration.get("strategy") or {}
    tool_calls = strategy.get("tool_calls") or []
    return tuple(str(call.get("tool_name", "")) for call in tool_calls if isinstance(call, dict))


# ---------------------------------------------------------------------------
# Revision rationale template
# ---------------------------------------------------------------------------


def build_revision_rationale_template(
    session: SessionState,
    iteration: IterationRecord,
) -> str:
    """Build the deterministic ``revision_rationale`` text for an ``adjust`` verdict.

    Used both as the rationale on sessions with ``use_sampling=false``
    and as the fallback rationale when sampling is rejected on a
    ``use_sampling=true`` session.
    Pure: depends only on persisted Session/Iteration fields, never
    calls into the sampler or any other non-deterministic component.

    The rendered text names the iteration index (1-indexed for
    operator-friendliness), the heuristic reason, the unmet Criterion
    ids (so the rationale points at the goal that's still moving), and
    a one-line summary of the in-progress strategy (tool-name sequence
    or ``"scripted strategy"``). The format is intentionally short and
    machine-parseable — operators can grep it; no LLM is involved.
    """
    # Resolve the iteration index — the in-progress iteration has not
    # been appended to session["iterations"] yet, so its 0-indexed
    # position equals len(iterations) and the 1-indexed position is +1.
    iteration_index_one_based = len(session["iterations"]) + 1

    # Match the heuristic again so the rationale text matches whichever
    # clause actually fired. Both calls are pure and cheap.
    _, heuristic_reason = _strategy_unproductive(session, iteration)
    if not heuristic_reason:
        # decide_verdict only emits ``adjust`` when the heuristic fires,
        # but the caller may invoke this template independently (e.g.
        # the sampling-fallback path on a non-heuristic adjust) — fall
        # back to a generic reason so the template stays usable.
        heuristic_reason = "strategy_review_requested"

    unmet_ids = [
        result["criterion_id"]
        for result in iteration["criteria_evaluation"]
        if result["status"] == "unmet"
    ]
    unmet_summary = ", ".join(unmet_ids) if unmet_ids else "none"

    strategy = iteration.get("strategy") or {}
    if "script" in strategy:
        strategy_summary = "scripted strategy"
    else:
        names = _tool_name_sequence(iteration)
        strategy_summary = ", ".join(names) if names else "no tool calls"

    no_progress = session["no_progress_counter"]
    threshold = session["stagnation_threshold"]

    return (
        f"Strategy revised on iteration {iteration_index_one_based}: "
        f"{heuristic_reason}. Unmet criteria: {unmet_summary}. "
        f"Last strategy: {strategy_summary}. "
        f"No-progress counter: {no_progress}/{threshold}. "
        f"Adjusting approach for next iteration."
    )
