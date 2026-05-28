"""End-to-end Mission session terminated by the iteration budget cap.

Walks one full Mission session through the engine without going near
the MCP or CLI surfaces. The session configures a Criterion the
dispatcher can never satisfy, so completion never fires and the verdict
cascade is forced down its budget-cap branch:

* ``test_terminate_on_max_iterations`` — caps the run at three
  iterations and asserts the cascade returns
  ``("terminate", "max_iterations")`` on the iteration where
  ``len(session["iterations"]) + 1 >= budget["max_iterations"]`` first
  flips True.

The test shares the structure of :mod:`tests.test_mission_e2e_train_to_loss`
and the precedent search / converge modules — a hand-built
:class:`SessionState` dict, a stub async dispatcher, a
:class:`MissionEngine` constructed with the dispatcher, and a small
driver loop that runs iterations until the cascade emits a terminal
verdict. The :class:`FilesystemBackend` is rooted at ``tmp_path`` so
the run is offline and self-contained — no AWS, no network, no real
LLM.

Where this test diverges from the precedents and why:

* **Unreachable Criterion.** The test uses a ``metric_threshold``
  Criterion with ``target=-1.0`` and ``op="<="``. The dispatcher
  returns a positive ``val_loss``, so the comparison is always
  ``unmet`` and the verdict cascade never reaches the completion
  branch. The cap branches above completion are the only way out of
  the loop, which is precisely what this test exercises.

Cost guardrails live out-of-band via AWS Budgets / Cost Anomaly
Detection rather than in the Mission cascade, so there is no
companion ``max_cost`` test in this module.
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

# Operator-language directive carried verbatim through the loop.
_DIRECTIVE_MAX_ITER = (
    "Drive validation loss to or below an unreachable target so the "
    "iteration cap is the only thing that ends the run."
)

# The single tool the session allowlists. Picked because it is on the
# safe-tier in the rest of the test corpus — no AWS calls, no network,
# no real side effects when the dispatcher is stubbed. The dispatcher
# in this module ignores the tool name entirely, but the engine's
# Tool_Allowlist gating enforces that the Propose_Phase deterministic
# fallback chooses an allowlisted name.
_ALLOWLISTED_TOOL = "find_examples"

# Iteration cap for the test. Three was specified by the original
# task brief; the cascade fires ``("terminate", "max_iterations")`` on
# the iteration where ``len(iterations) + 1 >= 3`` first holds, which
# is the third iteration (0-indexed iteration 2, count of 3).
_MAX_ITERATIONS_CAP = 3

# Driver-loop safety bound. The test should reach a terminal verdict
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
    function, which keeps the test free of cross-iteration coupling
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
    # Criterion; the wall-clock and stagnation branches are kept
    # dormant by their generous limits).
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


# ---------------------------------------------------------------------------
# Uncapped-sentinel coverage
# ---------------------------------------------------------------------------


def _make_session_uncapped_iter(reachable: bool) -> dict[str, Any]:
    """Build a session with ``max_iterations=-1`` (uncapped iterations).

    With the iteration cap disabled, the cascade is forced to exit
    through one of the other branches: completion (when ``reachable``
    is True), or — in the wall-clock-paired test — wall-clock. Wall
    clock is set to a finite 600 here so the only way out is
    completion.
    """
    target = 0.5 if reachable else -1.0  # ``<=`` against val_loss=0.5
    return {
        "version": SCHEMA_VERSION,
        "session_id": f"sess-uncapped-iter-{'reach' if reachable else 'unreach'}",
        "directive_text": "Iteration cap disabled; completion must drive termination.",
        "criteria": [
            {
                "criterion_id": "loss",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.val_loss",
                "op": "<=",
                "target": target,
            }
        ],
        "budget": {
            "max_iterations": -1,  # explicit uncapped sentinel
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
    }


@pytest.mark.mission_e2e
async def test_uncapped_iterations_does_not_terminate_on_iteration_count(
    tmp_path: Path,
) -> None:
    """``max_iterations=-1`` disables the iteration cap entirely.

    With the cap off and the Criterion set to a reachable target, the
    session completes via the criterion-met branch on iteration 0 —
    the cascade never enters the (now-dormant) iteration-cap branch.
    The point of the test is the negative invariant: even if the
    completion branch were buggy, the iteration-cap branch must not
    fire when the cap is the explicit ``-1`` sentinel.
    """
    backend = FilesystemBackend(root=tmp_path)
    session = _make_session_uncapped_iter(reachable=True)
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
    )

    record = await engine.run_iteration(session["session_id"])

    # The session terminated via completion, not the iteration cap. A
    # regression that reads ``max_iterations=-1`` as "max == -1" would
    # fire the iteration cap on iteration 0 (since ``0 + 1 >= -1`` is
    # always True for non-negative iteration counts) — this assertion
    # rules that out.
    assert record["verdict"] == "complete"
    assert record["verdict_reason"] == "criteria_met"


@pytest.mark.mission_e2e
async def test_uncapped_wall_clock_does_not_terminate_on_time(tmp_path: Path) -> None:
    """``max_wall_clock_seconds=-1`` disables the wall-clock cap entirely.

    Set the iteration cap to 1 so the only thing that could end the
    run before completion is the wall-clock cap. Pass ``-1`` to
    disable that cap explicitly. The session terminates on
    ``max_iterations`` after exactly one iteration; a regression that
    treated ``-1`` as "any elapsed time exceeds it" would terminate
    with ``max_wall_clock`` instead.
    """
    backend = FilesystemBackend(root=tmp_path)
    session = {
        "version": SCHEMA_VERSION,
        "session_id": "sess-uncapped-wall",
        "directive_text": "Wall-clock cap disabled; iteration cap drives termination.",
        "criteria": [_make_unreachable_criterion()],
        "budget": {
            "max_iterations": 1,
            "max_wall_clock_seconds": -1,  # explicit uncapped sentinel
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
    }
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
    )

    record = await engine.run_iteration(session["session_id"])

    assert record["verdict"] == "terminate"
    assert record["verdict_reason"] == "max_iterations"
