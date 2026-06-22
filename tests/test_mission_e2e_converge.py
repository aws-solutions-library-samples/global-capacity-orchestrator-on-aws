"""End-to-end Mission session driven to completion by a convergence rule.

Walks one full Mission session through the engine without going near the
MCP or CLI surfaces. The directive declares a metric to drive
monotonically downward; completion requires *two* Criteria to hold
simultaneously:

* a ``metric_threshold`` Criterion — ``metrics.current_loss <= 0.5``,
* a ``predicate`` Criterion — the absolute delta of that metric across
  the last three iterations is at or below a small tolerance.

Asserts the four invariants the Reference_Use_Case brief calls out:

* The verdict cascade lands on ``("complete", "criteria_met")`` only on
  the iteration where *both* Criteria are simultaneously ``met`` — not
  earlier (when only the threshold is met but the values still vary)
  and not later (the cascade stops on the first satisfying iteration).
* The persisted Final_Report carries the directive verbatim.
* The audit stream emits exactly one ``mission_phase_event`` per phase
  per iteration (five per iteration).
* The persisted ``iterations`` length matches the Final_Report's
  ``iterations_run`` field.

Where this test diverges from the metric-threshold (9.3) and predicate
(9.4) precedents and why:

* **Two-Criterion AND semantics.** :func:`mcp.mission.decide._completion_satisfied`
  requires every ``required=True`` Criterion to be ``met`` *and* none
  to be ``inconclusive`` for the cascade to emit ``complete``. The test
  proves this branch by walking the loop through iterations where the
  threshold is already ``met`` but the predicate is still ``unmet`` —
  the session must keep iterating, not complete on a partial match.
* **Rolling-window observation.** The predicate sandbox forbids access
  to ``session["iterations"]``: the only data the predicate sees is
  the in-progress iteration's :class:`Observation`. The dispatcher
  therefore emits a rolling list of the last three metric values
  under ``metrics.recent_values`` so the predicate can compute the
  ``max - min`` delta against a self-contained payload. Maintaining
  the window in the dispatcher rather than the engine keeps the test
  honest — the engine and predicate sandbox stay unchanged.
* **Predicate sandbox surface.** The brief's text says "absolute delta
  across last 3 iterations" which the predicate evaluator can compute
  via ``max(...) - min(...)`` over the rolling window — both builtins
  are on the predicate sandbox allowlist (``len``, ``min``, ``max``,
  ``sum``, ``abs``, ``any``, ``all``, ``sorted``). ``abs(...)`` is not
  needed because ``max - min`` of a list of floats is always
  non-negative.

The test runs offline against a :class:`FilesystemBackend` rooted at
``tmp_path`` — no AWS calls, no network, no real LLM. The dispatcher
is a closure over a list iterator so the metric sequence is
reproducible across runs.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ``gco_mcp/run_mcp.py`` adds ``gco_mcp/`` to ``sys.path`` at runtime; pytest has
# to mirror that before any ``mission.*`` import resolves. Same idiom
# used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission import engine as mission_engine  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.predicate import parse_predicate  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------

# Five phases per iteration; the engine's ``_run_phase`` wrapper emits
# exactly one ``mission_phase_event`` per phase regardless of outcome.
_PHASES_PER_ITERATION = 5

# Loss threshold for the metric_threshold Criterion. Picked so the
# sequence below crosses it on iteration 3 — early enough that the
# threshold is satisfied while the predicate is still unmet, proving
# the AND semantics on the way through.
_LOSS_TARGET = 0.5

# Tolerance for the delta-convergence predicate. With the sequence
# below, the rolling ``max - min`` only drops below this on the
# iteration where the metric finally plateaus at ``0.40``.
_DELTA_TOLERANCE = 0.05

# Window size for the convergence check — "across the last 3
# iterations" per the Reference_Use_Case brief.
_CONVERGENCE_WINDOW = 3

# Loss sequence handed to the dispatcher. Designed to walk the verdict
# cascade through three distinct AND-semantics states:
#
#   iter | loss | recent_values     | threshold | predicate | verdict
#   -----+------+-------------------+-----------+-----------+----------
#     0  | 1.00 | [1.00]            | unmet     | unmet     | continue
#     1  | 0.90 | [1.00, 0.90]      | unmet     | unmet     | continue
#     2  | 0.80 | [1.00, 0.90, 0.80]| unmet     | unmet     | continue
#     3  | 0.50 | [0.90, 0.80, 0.50]| MET       | unmet     | continue   <-- threshold met, predicate not
#     4  | 0.40 | [0.80, 0.50, 0.40]| MET       | unmet     | continue
#     5  | 0.40 | [0.50, 0.40, 0.40]| MET       | unmet     | continue   <-- delta=0.10 still > 0.05
#     6  | 0.40 | [0.40, 0.40, 0.40]| MET       | MET       | COMPLETE   <-- both met simultaneously
#
# The mid-run states where one Criterion is met but the other is not
# are the whole point of this test: they prove the cascade does not
# fire ``complete`` on a partial match.
_LOSS_SEQUENCE = (1.00, 0.90, 0.80, 0.50, 0.40, 0.40, 0.40)

# Iteration index where the cascade is expected to emit ``complete``.
# Listed as a constant so a regression in either Criterion shows up as
# a cleanly-named assertion failure rather than a vague index mismatch.
_EXPECTED_COMPLETION_INDEX = 6

# Per-iteration Criterion statuses anticipated for the cascade above.
# Pinned so any change to the Observation merge, the metric path
# resolution, or the predicate evaluator surfaces here as a precise
# diff instead of a verdict-only mismatch.
_EXPECTED_THRESHOLD_STATUSES = [
    "unmet",  # iter 0 — loss 1.00
    "unmet",  # iter 1 — loss 0.90
    "unmet",  # iter 2 — loss 0.80
    "met",  # iter 3 — loss 0.50 (boundary; <= passes)
    "met",  # iter 4 — loss 0.40
    "met",  # iter 5 — loss 0.40
    "met",  # iter 6 — loss 0.40
]
_EXPECTED_PREDICATE_STATUSES = [
    "unmet",  # iter 0 — window [1.00], len < 3
    "unmet",  # iter 1 — window [1.00, 0.90], len < 3
    "unmet",  # iter 2 — window [1.00, 0.90, 0.80], delta 0.20 > 0.05
    "unmet",  # iter 3 — window [0.90, 0.80, 0.50], delta 0.40 > 0.05
    "unmet",  # iter 4 — window [0.80, 0.50, 0.40], delta 0.40 > 0.05
    "unmet",  # iter 5 — window [0.50, 0.40, 0.40], delta 0.10 > 0.05
    "met",  # iter 6 — window [0.40, 0.40, 0.40], delta 0.00 <= 0.05
]

# Predicate expression — rewritten in the subscript form the Mission
# predicate sandbox accepts. ``len`` / ``min`` / ``max`` are on the
# allowlist; attribute access on ``obs`` is one-level only and we
# don't need it here. The ``len(...) >= window`` clause guards the
# first two iterations where the rolling window has fewer than three
# entries — without it the predicate would evaluate the delta of a
# one- or two-element list and silently return ``True`` on iteration 1.
_PREDICATE_EXPR = (
    f'len(obs["metrics"]["recent_values"]) >= {_CONVERGENCE_WINDOW} '
    f'and (max(obs["metrics"]["recent_values"]) '
    f'- min(obs["metrics"]["recent_values"])) <= {_DELTA_TOLERANCE}'
)

# Operator-language directive carried verbatim through the loop and
# into the Final_Report. No references to internal planning artifacts
# so the no-spec-references guardrail stays happy.
_DIRECTIVE = (
    "Drive the validation loss to or below 0.5 and confirm convergence "
    "by holding the loss steady across the most recent three iterations."
)


# ---------------------------------------------------------------------------
# Backend wrapper
# ---------------------------------------------------------------------------


class _PredicateAwareBackend(FilesystemBackend):
    """Filesystem backend that survives a non-JSON ``_parsed_ast`` cache.

    The engine reloads the session from the backend on every
    ``run_iteration`` call. The cached :class:`ast.Expression` attached
    by :func:`parse_predicate` is not JSON-serialisable, so a plain
    :class:`FilesystemBackend.save_session` would raise ``TypeError``
    when handed a session with predicate criteria. Production avoids
    this by pre-stripping private keys at the MCP-tool layer; here we
    fold the same pattern into a thin backend wrapper so the test
    drives the engine through its real persistence cycle.

    On save, we deep-copy the session and drop every leading-underscore
    key from each criterion. On load, we re-parse the criterion
    expressions and re-attach the AST under ``_parsed_ast`` so the
    engine's Evaluate_Phase finds it on every iteration.

    Same wrapper as :mod:`tests.test_mission_e2e_search` — duplicated
    rather than shared because the e2e tests are intentionally
    self-contained walk-throughs and a shared helper would couple
    them in a way that obscures the per-test fixtures.
    """

    def save_session(self, session: dict[str, Any]) -> None:  # type: ignore[override]
        cleaned = dict(session)
        criteria = cleaned.get("criteria")
        if isinstance(criteria, list):
            cleaned["criteria"] = [
                {k: v for k, v in c.items() if not str(k).startswith("_")}
                if isinstance(c, dict)
                else c
                for c in criteria
            ]
        super().save_session(cleaned)  # type: ignore[arg-type]

    def load_session(self, session_id: str) -> dict[str, Any] | None:  # type: ignore[override]
        loaded = super().load_session(session_id)
        if loaded is None:
            return None
        criteria = loaded.get("criteria")
        if isinstance(criteria, list):
            for criterion in criteria:
                if isinstance(criterion, dict) and criterion.get("kind") == "predicate":
                    expression = criterion.get("expression")
                    if isinstance(expression, str):
                        criterion["_parsed_ast"] = parse_predicate(expression)
        return loaded


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_session(*, session_id: str = "sess-converge") -> dict[str, Any]:
    """Build a minimal ``SessionState`` dict by hand.

    Bypasses the validators on purpose — the engine consumes the typed
    fields directly and the validators are exercised in their own test
    module. The shape mirrors the precedent e2e modules so any drift in
    the persisted contract surfaces in all three places.

    Tuning notes:

    * ``max_iterations=20`` is the cap declared in the e2e brief; the
      sequence completes on iteration 6, well inside that bound.
    * ``every_iteration`` cadence makes every iteration an evaluated
      checkpoint, so the verdict cascade reaches the completion check
      on every iteration without synthetic ``cadence_skip`` verdicts.
    * ``stagnation_threshold=100`` keeps the no-progress termination
      branch dormant. The dispatcher returns the same ``0.40`` for
      iterations 4, 5, and 6, which the engine's improvement check
      treats as "no improvement" — so the no-progress counter
      *does* advance on those final iterations. A high threshold
      stops the stagnation branch from firing before the convergence
      branch does.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": _DIRECTIVE,
        "criteria": [
            {
                "criterion_id": "loss_below_target",
                "kind": "metric_threshold",
                "required": True,
                # The dispatcher returns ``{"metrics": {"current_loss":
                # ..., "recent_values": [...]}}`` and the Observe_Phase
                # permissively merges the top-level ``metrics`` dict
                # into the Observation, so the dot-path
                # ``metrics.current_loss`` resolves to the dispatcher
                # value.
                "metric": "metrics.current_loss",
                "op": "<=",
                "target": _LOSS_TARGET,
            },
            {
                "criterion_id": "loss_converged",
                "kind": "predicate",
                "required": True,
                "expression": _PREDICATE_EXPR,
                # Pre-validated AST — the engine's _evaluate_predicate
                # returns "inconclusive" when this slot is missing.
                "_parsed_ast": parse_predicate(_PREDICATE_EXPR),
            },
        ],
        "budget": {
            "max_iterations": 20,
            "max_wall_clock_seconds": 600,
        },
        "tool_allowlist": ["submit_job_sqs"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _converging_loss_dispatcher() -> Any:
    """Return an async dispatcher that emits a converging loss series.

    Each call returns the next value from :data:`_LOSS_SEQUENCE` along
    with a rolling list of the most recent
    :data:`_CONVERGENCE_WINDOW` values. The window list is what the
    delta-tolerance predicate actually inspects — the predicate has no
    way to walk ``session["iterations"]``, so the dispatcher folds the
    history into the current iteration's Observation under
    ``metrics.recent_values``.

    Closure over a list keeps the window stateful across the
    dispatcher's lifetime but isolated to the test invocation —
    pytest's ``function`` fixture scope plus a fresh closure per test
    gives each run an independent sequence. After the configured
    sequence is exhausted the dispatcher falls back to the trailing
    value (``0.40``) indefinitely so a regression in completion
    detection does not crash the test for an unrelated reason.
    """
    sequence = itertools.chain(iter(_LOSS_SEQUENCE), itertools.repeat(_LOSS_SEQUENCE[-1]))
    window: list[float] = []

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict[str, Any]:
        # The dispatcher returns the same shape an MCP tool result
        # would carry — a dict whose top-level ``metrics`` key the
        # Observe_Phase lifts into the Observation. The ``tool_name``
        # and ``args`` arguments are ignored: the stub does not need
        # to discriminate, and the engine is the single place that
        # checks Tool_Allowlist gating before this callable is even
        # invoked.
        current = next(sequence)
        window.append(current)
        # Trim the window to the convergence size; the predicate's
        # ``len(...) >= window`` clause guards iterations where the
        # window has not yet filled.
        if len(window) > _CONVERGENCE_WINDOW:
            del window[0]
        return {
            "metrics": {
                "current_loss": current,
                # New list per call so the engine's persistence layer
                # never sees a mutable reference shared across
                # iterations — a shared list would be observable as
                # the same window value on every persisted iteration
                # after it stabilises, which would also pass the
                # assertions but would mask a real regression.
                "recent_values": list(window),
            }
        }

    return dispatcher


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


@pytest.mark.mission_e2e
async def test_convergence_completes_only_when_both_criteria_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a session that completes only when threshold AND predicate fire.

    With the loss sequence ``(1.00, 0.90, 0.80, 0.50, 0.40, 0.40,
    0.40)`` and the rolling window of size 3:

    * iterations 0–2 — both Criteria unmet → ``("continue", "in_progress")``
    * iteration 3 — threshold met (``0.50 <= 0.5``), predicate unmet
      (delta 0.40) → ``("continue", "in_progress")``
    * iterations 4–5 — threshold met, predicate still unmet
      (delta 0.40 then 0.10) → ``("continue", "in_progress")``
    * iteration 6 — threshold met AND predicate met (window is
      ``[0.40, 0.40, 0.40]``, delta 0.00) → ``("complete", "criteria_met")``

    The test then walks the persisted artifacts to assert the four
    invariants the brief calls out: terminal verdict, directive
    fidelity in the report, exactly five phase events per iteration in
    the audit stream, and ``len(iterations) == iterations_run``.
    """
    backend = _PredicateAwareBackend(root=tmp_path)
    session = _make_session()
    backend.save_session(session)

    # Capture every ``emit_phase_event`` call. Patching the bound
    # attribute on ``mission.engine.audit`` is sufficient because the
    # engine module references the audit module by attribute lookup —
    # ``mission_engine.audit.emit_phase_event(...)``. Same idiom as
    # the train-to-loss precedent (9.3).
    phase_events: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> None:
        phase_events.append(kwargs)

    monkeypatch.setattr(mission_engine.audit, "emit_phase_event", capture)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_converging_loss_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
    )

    # Drive iterations until the verdict cascade ends the run. The 20
    # safety bound matches ``budget.max_iterations`` so a regression
    # in completion detection shows up as a test failure here rather
    # than as an infinite loop.
    final_record: dict[str, Any] | None = None
    for _ in range(20):
        record = await engine.run_iteration(session["session_id"])
        if record["verdict"] in ("complete", "terminate"):
            final_record = record
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "Mission did not reach a terminal verdict within "
            "max_iterations; convergence detection may be misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — completion fired on the iteration where both
    # Criteria simultaneously evaluated ``met``.
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "complete"
    assert final_record["verdict_reason"] == "criteria_met"
    assert final_record["iteration_index"] == _EXPECTED_COMPLETION_INDEX

    # ------------------------------------------------------------------ #
    # Persistence invariant — session is now in ``completed`` state and
    # the iteration count matches the expected run length.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "completed"
    assert persisted["final_verdict"] == "complete"
    assert len(persisted["iterations"]) == _EXPECTED_COMPLETION_INDEX + 1

    # ------------------------------------------------------------------ #
    # AND-semantics invariant — the per-iteration criterion statuses
    # walked through the expected pattern. This is the core of the
    # Convergence_Optimization Reference_Use_Case: the cascade must
    # NOT fire ``complete`` when only the threshold is met (iters 3–5)
    # and must fire on the first iteration where both Criteria are
    # ``met`` simultaneously (iter 6).
    # ------------------------------------------------------------------ #
    threshold_statuses = [
        next(
            result["status"]
            for result in iteration["criteria_evaluation"]
            if result["criterion_id"] == "loss_below_target"
        )
        for iteration in persisted["iterations"]
    ]
    predicate_statuses = [
        next(
            result["status"]
            for result in iteration["criteria_evaluation"]
            if result["criterion_id"] == "loss_converged"
        )
        for iteration in persisted["iterations"]
    ]
    assert threshold_statuses == _EXPECTED_THRESHOLD_STATUSES
    assert predicate_statuses == _EXPECTED_PREDICATE_STATUSES

    # Spot-check the iterations where the threshold was met but the
    # predicate was not — these prove the AND semantics on the way
    # through. Anywhere both conditions held simultaneously before
    # iteration 6 would be a completion-cascade bug.
    for index in (3, 4, 5):
        iteration = persisted["iterations"][index]
        statuses = {
            result["criterion_id"]: result["status"] for result in iteration["criteria_evaluation"]
        }
        assert statuses["loss_below_target"] == "met"
        assert statuses["loss_converged"] == "unmet", (
            f"iteration {index} should still have predicate unmet "
            f"so the AND completion does not fire prematurely"
        )

    # ------------------------------------------------------------------ #
    # Final_Report invariant — directive carried verbatim, iteration
    # count consistent with the persisted session.
    # ------------------------------------------------------------------ #
    report_path = Path(persisted["final_report_path"])
    assert report_path.exists()
    report = json.loads(report_path.read_text())

    assert report["directive_text"] == _DIRECTIVE
    assert report["iterations_run"] == len(persisted["iterations"])
    assert report["final_verdict"] == "complete"
    assert report["final_verdict_reason"] == "criteria_met"
    # Both Criteria appear in the report's final evaluation snapshot
    # and both are ``met`` — operators reading the report should see a
    # full picture of what closed the loop.
    final_eval = {
        result["criterion_id"]: result["status"]
        for result in report["final_criteria_evaluation"] or []
    }
    assert final_eval == {"loss_below_target": "met", "loss_converged": "met"}

    # ------------------------------------------------------------------ #
    # Audit invariant — five phase events per iteration, no skips.
    # ------------------------------------------------------------------ #
    assert len(phase_events) == _PHASES_PER_ITERATION * len(persisted["iterations"])

    # Every event tagged with this session, every event ``succeeded``
    # (no phase exceptions on the happy path), and the per-iteration
    # phase ordering is the canonical propose → execute → observe →
    # evaluate → decide cycle.
    expected_phase_order = ["propose", "execute", "observe", "evaluate", "decide"]
    for iteration_index in range(len(persisted["iterations"])):
        slice_start = iteration_index * _PHASES_PER_ITERATION
        slice_end = slice_start + _PHASES_PER_ITERATION
        iteration_events = phase_events[slice_start:slice_end]
        assert [event["phase"] for event in iteration_events] == expected_phase_order
        for event in iteration_events:
            assert event["session_id"] == session["session_id"]
            assert event["iteration_index"] == iteration_index
            assert event["status"] == "succeeded"
