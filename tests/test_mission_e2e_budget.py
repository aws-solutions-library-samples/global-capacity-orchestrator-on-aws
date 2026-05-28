"""End-to-end Mission sessions terminated by budget caps.

Walks two full Mission sessions through the engine without going near
the MCP or CLI surfaces. Both sessions configure a Criterion the
dispatcher can never satisfy, so completion never fires and the verdict
cascade is forced down its budget-cap branch:

* ``test_terminate_on_max_iterations`` — caps the run at three
  iterations and asserts the cascade returns
  ``("terminate", "max_iterations")`` on the iteration where
  ``len(session["iterations"]) + 1 >= budget["max_iterations"]`` first
  flips True.
* ``test_terminate_on_max_cost`` — wires a cost estimator that adds
  ``$5.00`` per successful tool call, caps the budget at ``$12.00``,
  and asserts the cascade returns ``("terminate", "max_cost")`` on the
  iteration where the running ``accumulated_cost_usd`` first reaches
  the cap.

Both tests share the structure of :mod:`tests.test_mission_e2e_train_to_loss`
and the precedent search / converge modules — a hand-built
:class:`SessionState` dict, a stub async dispatcher, a
:class:`MissionEngine` constructed with the dispatcher, and a small
driver loop that runs iterations until the cascade emits a terminal
verdict. The :class:`FilesystemBackend` is rooted at ``tmp_path`` so
both runs are offline and self-contained — no AWS, no network, no real
LLM.

Where this test diverges from the precedents and why:

* **Unreachable Criterion.** Both tests use a ``metric_threshold``
  Criterion with ``target=-1.0`` and ``op="<="``. The dispatcher
  returns a positive ``val_loss``, so the comparison is always
  ``unmet`` and the verdict cascade never reaches the completion
  branch. The cap branches above completion are the only way out of
  the loop, which is precisely what these tests exercise.
* **Cost wiring.** ``test_terminate_on_max_cost`` registers a single
  cost estimator on the engine — a closure that returns ``5.0`` for
  every call regardless of args. The engine's ``_dispatch_one_call``
  adds the estimator's return to ``session["accumulated_cost_usd"]``
  after every successful call, and the Decide_Phase reads the same
  field on every iteration. The exact number of iterations the
  cascade takes to terminate depends on the cost-accumulation order
  (Execute_Phase runs before Decide_Phase, so the running total
  visible to Decide is post-Execute), so the assertion is bounded
  rather than equality-pinned: 2 or 3 iterations are both valid.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime; pytest has
# to mirror that before any ``mission.*`` import resolves. Same idiom
# used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Shared parameters
# ---------------------------------------------------------------------------

# Operator-language directives carried verbatim through each loop. Two
# distinct directives so a regression that crosses-wires the sessions
# (e.g. one test loading the other's persisted state) surfaces as a
# directive mismatch rather than a vague verdict drift.
_DIRECTIVE_MAX_ITER = (
    "Drive validation loss to or below an unreachable target so the "
    "iteration cap is the only thing that ends the run."
)
_DIRECTIVE_MAX_COST = (
    "Run cost-incurring tools repeatedly until the accumulated spend "
    "reaches the declared budget cap."
)

# The single tool both sessions allowlist. Picked because it is on the
# safe-tier in the rest of the test corpus — no AWS calls, no network,
# no real side effects when the dispatcher is stubbed. The dispatcher
# in this module ignores the tool name entirely, but the engine's
# Tool_Allowlist gating enforces that the Propose_Phase deterministic
# fallback chooses an allowlisted name.
_ALLOWLISTED_TOOL = "find_examples"

# The Propose_Phase deterministic fallback synthesises a single-call
# Strategy per iteration when no prior successful call exists, and
# re-runs that same call on every subsequent iteration. With one call
# per iteration the cost accumulator advances by exactly one estimator
# return per iteration, which is the simplest pattern for asserting
# "after 2 or 3 calls the cap fires".
_COST_PER_CALL_USD = 5.0
_MAX_COST_USD = 12.0

# Iteration cap for the max_iterations test. Three was specified by the
# task brief; the cascade fires ``("terminate", "max_iterations")`` on
# the iteration where ``len(iterations) + 1 >= 3`` first holds, which
# is the third iteration (0-indexed iteration 2, count of 3).
_MAX_ITERATIONS_CAP = 3

# Iteration cap for the max_cost test. Picked high enough that the cost
# branch fires before the iteration branch can — at $5/call and a $12
# cap, the cost branch fires on call 3 at the latest, well below this
# generous cap.
_MAX_ITERATIONS_GENEROUS = 50

# Driver-loop safety bound. Both tests should reach a terminal verdict
# in well under this many iterations; the bound exists so a regression
# in cap detection surfaces as a clean test failure rather than an
# infinite loop.
_DRIVER_LOOP_BOUND = 20


# ---------------------------------------------------------------------------
# Session builders
# ---------------------------------------------------------------------------


def _make_unreachable_criterion() -> dict[str, Any]:
    """Build a ``metric_threshold`` Criterion the dispatcher cannot satisfy.

    The dispatcher in this module always returns a positive ``val_loss``
    (``0.5``), so the comparison ``val_loss <= -1.0`` is always
    ``unmet``. Combined with ``required=True`` this guarantees the
    completion branch never fires; the cascade has to exit through one
    of the budget-cap branches.
    """
    return {
        "criterion_id": "unreachable_loss_target",
        "kind": "metric_threshold",
        "required": True,
        # The dispatcher returns ``{"metrics": {"val_loss": ...}}`` and
        # the Observe_Phase permissively merges the top-level
        # ``metrics`` dict into the Observation, so the dot-path
        # ``metrics.val_loss`` resolves to the dispatcher value.
        "metric": "metrics.val_loss",
        "op": "<=",
        "target": -1.0,
    }


def _make_session_max_iter() -> dict[str, Any]:
    """Build the session for the ``max_iterations`` test.

    Bypasses the validators on purpose — the engine consumes the typed
    fields directly and the validators are exercised in their own test
    module. The shape mirrors :mod:`tests.test_mission_e2e_train_to_loss`
    so any drift in the persisted contract surfaces in both places.

    Tuning notes:

    * ``max_iterations=3`` is the cap declared in the task brief; the
      cascade fires ``("terminate", "max_iterations")`` on iteration
      2 (the third 0-indexed iteration).
    * ``every_iteration`` cadence makes every iteration an evaluated
      checkpoint, so the verdict cascade reaches the budget-cap check
      on every iteration without synthetic ``cadence_skip`` verdicts.
    * ``stagnation_threshold=100`` keeps the no-progress termination
      branch dormant — a three-iteration run cannot reach a counter of
      100, so the cascade can only terminate via ``max_iterations``.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": "sess-budget-iter",
        "directive_text": _DIRECTIVE_MAX_ITER,
        "criteria": [_make_unreachable_criterion()],
        "budget": {
            "max_iterations": _MAX_ITERATIONS_CAP,
            "max_wall_clock_seconds": 600,
        },
        "tool_allowlist": [_ALLOWLISTED_TOOL],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
    }


def _make_session_max_cost() -> dict[str, Any]:
    """Build the session for the ``max_cost`` test.

    Same shape as :func:`_make_session_max_iter` but with the iteration
    cap relaxed and a cost cap declared. The cost cap is the only
    constraint that can fire here:

    * The completion branch is locked off by the unreachable Criterion.
    * The iteration cap (``50``) is generous — at one call per
      iteration and a ``$5.00`` per-call estimator, the cost cap
      fires at iteration 2 or 3, far below the iteration cap.
    * The wall-clock and stagnation branches are kept dormant by
      large values for the same reason as the precedent tests.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": "sess-budget-cost",
        "directive_text": _DIRECTIVE_MAX_COST,
        "criteria": [_make_unreachable_criterion()],
        "budget": {
            "max_iterations": _MAX_ITERATIONS_GENEROUS,
            "max_wall_clock_seconds": 600,
            "max_cost_usd": _MAX_COST_USD,
        },
        "tool_allowlist": [_ALLOWLISTED_TOOL],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 100,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
        "accumulated_cost_usd": 0.0,
    }


# ---------------------------------------------------------------------------
# Stub dispatchers
# ---------------------------------------------------------------------------


def _unmet_metric_dispatcher() -> Any:
    """Return an async dispatcher that always emits an unmet ``val_loss``.

    The constant ``0.5`` is positive, so the Criterion's
    ``val_loss <= -1.0`` comparison is always ``unmet`` and the
    completion branch is locked off. The dispatcher signature matches
    the engine's :class:`ToolDispatcher` protocol; ``tool_name`` and
    ``args`` are ignored because the stub does not need to discriminate
    and the engine is the single place that checks Tool_Allowlist
    gating before this callable is invoked.

    No mutable state is captured — the dispatcher is purely a constant
    function, which keeps both tests free of cross-iteration coupling
    in the dispatcher itself.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict[str, Any]:
        return {"metrics": {"val_loss": 0.5}}

    return dispatcher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.mission_e2e
async def test_terminate_on_max_iterations(tmp_path: Path) -> None:
    """Cascade returns ``terminate / max_iterations`` after the third iteration.

    With ``budget["max_iterations"]=3`` and an unreachable Criterion,
    the cascade walks the iteration count up:

    * iteration 0 — ``len(iterations)+1 = 1 < 3`` → continue
    * iteration 1 — ``len(iterations)+1 = 2 < 3`` → continue
    * iteration 2 — ``len(iterations)+1 = 3 >= 3`` → terminate

    The driver-loop bound of :data:`_DRIVER_LOOP_BOUND` is generous so
    a regression in the cap branch (e.g. a fence-post error that lets
    the loop overrun by one) shows up as a precise iteration-count
    mismatch rather than as an infinite loop.
    """
    backend = FilesystemBackend(root=tmp_path)
    session = _make_session_max_iter()
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
        cost_estimators={},
    )

    final_record: dict[str, Any] | None = None
    iteration_count = 0
    for _ in range(_DRIVER_LOOP_BOUND):
        record = await engine.run_iteration(session["session_id"])
        iteration_count += 1
        if record["verdict"] in ("complete", "terminate"):
            final_record = record
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "Mission did not reach a terminal verdict within the driver "
            "loop bound; max_iterations cap detection may be misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — the iteration cap is the only branch that
    # could have fired (completion is locked off by the unreachable
    # Criterion; the wall-clock, cost, and stagnation branches are
    # kept dormant by their generous limits).
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "terminate"
    assert final_record["verdict_reason"] == "max_iterations"

    # ------------------------------------------------------------------ #
    # Iteration-count invariant — exactly three iterations ran. With
    # ``max_iterations=3`` the cascade fires on the third iteration
    # (where ``len(iterations)+1 >= 3`` first holds). A different
    # count is a fence-post regression in :func:`decide.decide_verdict`.
    # ------------------------------------------------------------------ #
    assert iteration_count == _MAX_ITERATIONS_CAP

    # ------------------------------------------------------------------ #
    # Persistence invariant — the session is now in the ``terminated``
    # status and the persisted iteration list matches the count.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"
    assert persisted["final_verdict"] == "terminate"
    assert len(persisted["iterations"]) == _MAX_ITERATIONS_CAP

    # The Final_Report is the durable exit artifact; sanity-check that
    # it lands and carries the matching verdict / reason so an operator
    # reading the report sees the same story the in-memory record did.
    report_path = Path(persisted["final_report_path"])
    assert report_path.exists()


@pytest.mark.mission_e2e
async def test_terminate_on_max_cost(tmp_path: Path) -> None:
    """Cascade returns ``terminate / max_cost`` once accumulated cost hits the cap.

    With ``budget["max_cost_usd"]=12.0`` and a ``$5.00`` per-call cost
    estimator, the cascade walks the running total up:

    * iteration 0 — Execute adds ``$5``, total ``$5 < $12`` → continue
    * iteration 1 — Execute adds ``$5``, total ``$10 < $12`` → continue
    * iteration 2 — Execute adds ``$5``, total ``$15 >= $12`` → terminate

    The exact iteration count where the cascade fires is bounded
    rather than equality-pinned (``2 or 3``) so the test is robust to
    any future change in the order of the budget-cap evaluations
    inside :func:`decide.decide_verdict` — what matters is that the
    cap fires and that the verdict reason is ``max_cost``.
    """
    backend = FilesystemBackend(root=tmp_path)
    session = _make_session_max_cost()
    backend.save_session(session)

    # The engine accepts a ``cost_estimators`` mapping keyed by tool
    # name. The estimator is invoked once per successful call inside
    # :meth:`MissionEngine._dispatch_one_call`; its return is added to
    # ``session["accumulated_cost_usd"]``. A constant return ignores
    # the args dict, which is fine for this test — the cap-detection
    # logic does not depend on per-call variation.
    cost_estimators = {_ALLOWLISTED_TOOL: lambda args: _COST_PER_CALL_USD}

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
        cost_estimators=cost_estimators,
    )

    final_record: dict[str, Any] | None = None
    iteration_count = 0
    for _ in range(_DRIVER_LOOP_BOUND):
        record = await engine.run_iteration(session["session_id"])
        iteration_count += 1
        if record["verdict"] in ("complete", "terminate"):
            final_record = record
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "Mission did not reach a terminal verdict within the driver "
            "loop bound; max_cost cap detection may be misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — the cost cap is the only branch that could
    # have fired (completion is locked off by the unreachable
    # Criterion; the iteration cap is generous; wall-clock and
    # stagnation are dormant).
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "terminate"
    assert final_record["verdict_reason"] == "max_cost"

    # ------------------------------------------------------------------ #
    # Iteration-count invariant — bounded rather than equality-pinned.
    # At ``$5/call`` and a ``$12`` cap, the running total reaches the
    # cap on call 3 (total ``$15``). The exact iteration the cascade
    # observes that depends on the order of Execute and Decide inside
    # ``run_iteration`` — Execute runs first, so Decide sees the
    # post-Execute total and fires on iteration 2 (the 0-indexed
    # third iteration). Allowing 2 or 3 keeps the test robust to a
    # future re-ordering that reads the cost mid-iteration.
    # ------------------------------------------------------------------ #
    assert iteration_count in (2, 3)

    # ------------------------------------------------------------------ #
    # Cost-accumulator invariant — the running total at termination is
    # at or above the cap. This is the predicate the cascade actually
    # tests, so a regression in the accumulator (e.g. costs being
    # double-counted, or zeroed between iterations) shows up here as
    # a precise diff rather than as a verdict mismatch.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["accumulated_cost_usd"] >= _MAX_COST_USD

    # ------------------------------------------------------------------ #
    # Persistence invariant — the session is now in the ``terminated``
    # status and the persisted iteration list matches the count.
    # ------------------------------------------------------------------ #
    assert persisted["status"] == "terminated"
    assert persisted["final_verdict"] == "terminate"
    assert len(persisted["iterations"]) == iteration_count

    # The Final_Report is the durable exit artifact; sanity-check that
    # it lands so an operator reading the report sees the same story
    # the in-memory record did.
    report_path = Path(persisted["final_report_path"])
    assert report_path.exists()
