"""End-to-end Mission sessions terminated by the no-progress counter.

Walks two full Mission sessions through the engine without going near
the MCP or CLI surfaces. Both sessions configure a Criterion the
dispatcher can never satisfy, so completion never fires and no
criterion ever improves; the no-progress counter advances on every
evaluated iteration until either the Strategy_Revision_Heuristic
flags the run as unproductive (``adjust``) or the counter hits
``stagnation_threshold`` and the cascade emits
``("terminate", "no_progress")``.

* ``test_adjust_fires_before_terminate`` — caps the run at
  ``stagnation_threshold=4`` and asserts that the heuristic fires
  ``("adjust", "heuristic_unproductive")`` on an iteration *before*
  the cascade terminates. The brief calls out that the ``adjust``
  verdict must surface at the half-threshold (``ceil(4/2)=2``) once
  three consecutive iterations share a tool-name sequence — clause
  (a) of :func:`mcp.mission.decide._strategy_unproductive`.
* ``test_terminate_on_no_progress`` — same setup but lets the loop
  run all the way to threshold; asserts the final verdict pair is
  ``("terminate", "no_progress")`` and the persisted session reflects
  the terminal state.

Both tests share the structure of the precedent
:mod:`tests.test_mission_e2e_train_to_loss` and
:mod:`tests.test_mission_e2e_budget` modules — a hand-built
:class:`SessionState` dict, a stub async dispatcher, a
:class:`MissionEngine` constructed with the dispatcher, and a small
driver loop that runs iterations until the verdict cascade emits a
verdict the test cares about. The :class:`FilesystemBackend` is rooted
at ``tmp_path`` so both runs are offline and self-contained — no AWS,
no network, no real LLM.

Where this test diverges from the precedents and why:

* **Unreachable Criterion.** Both tests use a ``metric_threshold``
  Criterion with ``op="<"`` and ``target=-1.0``. The dispatcher
  returns a positive ``loss``, so the comparison is always ``unmet``
  and the verdict cascade never reaches the completion branch. The
  no-progress branch is the only constraint that can fire, which is
  precisely what these tests exercise.
* **Counter-walking cadence.** ``every_iteration`` keeps every
  iteration on a real checkpoint — synthetic ``cadence_skip``
  iterations would leave the counter alone (Requirement 6.8) and
  keep stagnation latent forever. Pinning the cadence makes the
  counter sequence ``0 → 1 → 2 → 3 → 4`` predictable across the run.
* **Generous iteration cap.** ``max_iterations=20`` is well above the
  stagnation threshold so the iteration cap cannot fire first. The
  same logic applies to ``max_wall_clock_seconds=600`` — the tests
  reach terminal long before the wall-clock cap could trip.
* **Deterministic Strategy fallback.** With one tool in the
  allowlist and no sampling, the engine's Propose_Phase deterministic
  fallback re-uses the same single-call Strategy on every iteration.
  Three consecutive iterations therefore have the same
  ``tool_calls[*].tool_name`` sequence, which is exactly the input
  clause (a) of the Strategy_Revision_Heuristic measures against.

Counter-and-verdict walk for ``stagnation_threshold=4``:

==========  =======  ==================  ============================
Iteration   Counter  Heuristic clause(a)  Verdict
==========  =======  ==================  ============================
0           0        no — counter < 2     ``continue / in_progress``
1           1        no — counter < 2     ``continue / in_progress``
2           2        yes — len(prior)=2,  ``adjust / heuristic_unproductive``
                     same sequence
3           3        yes                  ``adjust / heuristic_unproductive``
4           4 ≥ 4    n/a — terminate      ``terminate / no_progress``
==========  =======  ==================  ============================

The counter values shown above are the values *consulted by the
Decide_Phase on that iteration* — the post-iteration update that
advances the counter happens after Decide_Phase, so iteration 0 reads
counter ``0`` and writes ``1`` into the persisted session.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# ``gco_mcp/run_mcp.py`` adds ``gco_mcp/`` to ``sys.path`` at runtime; pytest has
# to mirror that before any ``mission.*`` import resolves. Same idiom
# used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Shared parameters
# ---------------------------------------------------------------------------

# Operator-language directive carried verbatim through the loop. No
# references to internal planning artifacts so the no-spec-references
# guardrail stays happy.
_DIRECTIVE = (
    "Drive validation loss to or below an unreachable target so the "
    "no-progress branch is the only path that can end the run."
)

# The single tool both sessions allowlist. Picked because it is on the
# safe-tier in the rest of the test corpus — no AWS calls, no network,
# no real side effects when the dispatcher is stubbed. The dispatcher
# in this module ignores the tool name entirely, but the engine's
# Tool_Allowlist gating enforces that the Propose_Phase deterministic
# fallback chooses an allowlisted name.
_ALLOWLISTED_TOOL = "find_examples"

# Stagnation threshold for both tests. ``4`` is the value declared in
# the task brief; combined with ``every_iteration`` cadence and an
# unreachable Criterion the cascade walks the counter from 0 to 4 in
# four post-iteration updates and terminates on the fifth iteration.
_STAGNATION_THRESHOLD = 4

# Driver-loop safety bound. Both tests should reach a terminal verdict
# in well under this many iterations; the bound exists so a regression
# in cap detection surfaces as a clean test failure rather than an
# infinite loop. Set higher than ``stagnation_threshold + 1`` so a
# fence-post regression that delays termination by one iteration shows
# up as an explicit assertion miss rather than a timeout.
_DRIVER_LOOP_BOUND = 20


# ---------------------------------------------------------------------------
# Session builder and dispatcher
# ---------------------------------------------------------------------------


def _make_unreachable_criterion() -> dict[str, Any]:
    """Build a ``metric_threshold`` Criterion the dispatcher cannot satisfy.

    The dispatcher in this module always returns a positive ``loss``
    (``0.5``), so the comparison ``loss < -1.0`` is always ``unmet``.
    Combined with ``required=True`` this guarantees the completion
    branch never fires; the cascade has to exit through the
    no-progress branch.

    Same idiom as :mod:`tests.test_mission_e2e_budget`'s unreachable
    criterion, but the metric path is the bare ``"loss"`` key so the
    Observe_Phase merge does not rely on a top-level ``metrics``
    dict — both forms work, and using the bare key here keeps the
    dispatcher's return shape the simplest possible.
    """
    return {
        "criterion_id": "unreachable_loss_target",
        "kind": "metric_threshold",
        "required": True,
        # The dispatcher returns ``{"metrics": {"loss": 0.5}}`` and
        # the Observe_Phase permissively merges the top-level
        # ``metrics`` dict into the Observation, so the dot-path
        # ``metrics.loss`` resolves to the dispatcher value.
        "metric": "metrics.loss",
        "op": "<",
        "target": -1.0,
    }


def _make_session(*, session_id: str) -> dict[str, Any]:
    """Build a minimal ``SessionState`` dict by hand.

    Bypasses the validators on purpose — the engine consumes the typed
    fields directly and the validators are exercised in their own test
    module. The shape mirrors the precedent e2e modules so any drift
    in the persisted contract surfaces in all of them.

    Tuning notes:

    * ``max_iterations=20`` is generous: the cascade terminates on
      iteration 4 (the 0-indexed fifth iteration), well inside that
      bound. A regression in the no-progress branch would surface
      first as a wrong verdict on iteration 4 rather than as a hit on
      this iteration cap.
    * ``every_iteration`` cadence keeps the no-progress counter
      advancing on every iteration. Synthetic ``cadence_skip``
      iterations leave the counter alone (Requirement 6.8) and the
      stagnation branch would never fire.
    * ``stagnation_threshold=4`` matches the brief. The
      Strategy_Revision_Heuristic's clause (a) fires once the counter
      reaches ``ceil(4/2)=2`` and three iterations share a tool-name
      sequence — that's iteration 2 in this run.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": _DIRECTIVE,
        "criteria": [_make_unreachable_criterion()],
        "budget": {
            "max_iterations": 20,
            "max_wall_clock_seconds": 600,
        },
        "tool_allowlist": [_ALLOWLISTED_TOOL],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": _STAGNATION_THRESHOLD,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _unmet_metric_dispatcher() -> Any:
    """Return an async dispatcher that always emits an unmet ``loss``.

    The constant ``0.5`` is positive, so the Criterion's
    ``loss < -1.0`` comparison is always ``unmet`` and the completion
    branch is locked off. The dispatcher signature matches the
    engine's :class:`ToolDispatcher` protocol; ``tool_name`` and
    ``args`` are ignored because the stub does not need to discriminate
    and the engine is the single place that checks Tool_Allowlist
    gating before this callable is invoked.

    No mutable state is captured — the dispatcher is purely a constant
    function, which keeps both tests free of cross-iteration coupling
    in the dispatcher itself. The deterministic fallback in
    Propose_Phase therefore sees the same inputs on every iteration
    and synthesises the same single-call Strategy, which is precisely
    the "same tool-name sequence" the heuristic's clause (a) detects.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict[str, Any]:
        return {"metrics": {"loss": 0.5}}

    return dispatcher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.mission_e2e
async def test_adjust_fires_before_terminate(tmp_path: Path) -> None:
    """The ``adjust`` verdict surfaces at the half-threshold before terminate.

    With ``stagnation_threshold=4``, ``every_iteration`` cadence, and
    a constant unmet Criterion, the cascade walks the counter up:

    * iteration 0 — counter ``0``, heuristic dormant → ``continue``
    * iteration 1 — counter ``1`` (< 2), heuristic dormant → ``continue``
    * iteration 2 — counter ``2`` (≥ ceil(4/2)=2), 3 same-sequence
      iterations (prior 2 + current) → ``adjust / heuristic_unproductive``
    * iteration 3 — counter ``3`` (< 4), heuristic still firing → ``adjust``
    * iteration 4 — counter ``4`` (≥ 4) → ``terminate / no_progress``

    The test pins:

    * an ``adjust`` verdict appears strictly before any ``terminate``
      verdict (Requirement 13.6's "the ``adjust`` Verdict fires …
      before ``terminate`` does"),
    * the first ``adjust`` lands on iteration 2 (the half-threshold
      iteration, per Requirement 8.4's clause (a)),
    * every ``adjust`` iteration carries a non-empty
      ``revision_rationale`` (Requirement 8.6's templated text).
    """
    backend = FilesystemBackend(root=tmp_path)
    session = _make_session(session_id="sess-stagnation-adjust")
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
    )

    # Walk the loop until the cascade terminates, recording each
    # iteration's verdict pair and counter for the assertions below.
    # The driver-loop bound is generous so a regression in either the
    # heuristic or the no-progress branch surfaces as a precise diff
    # rather than as a timeout.
    verdicts: list[tuple[str, str]] = []
    counters: list[int] = []
    rationales: list[str | None] = []
    for _ in range(_DRIVER_LOOP_BOUND):
        record = await engine.run_iteration(session["session_id"])
        verdicts.append((record["verdict"], record["verdict_reason"]))
        rationales.append(record.get("revision_rationale"))
        persisted = backend.load_session(session["session_id"])
        assert persisted is not None
        counters.append(persisted["no_progress_counter"])
        if record["verdict"] in ("complete", "terminate"):
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "Mission did not reach a terminal verdict within the driver "
            "loop bound; no-progress detection may be misconfigured."
        )

    # ------------------------------------------------------------------ #
    # Verdict-sequence invariant — exactly five iterations: two
    # ``continue``, two ``adjust``, one ``terminate``. The exact
    # cascade is locked because every input is deterministic
    # (constant dispatcher, deterministic Propose_Phase fallback,
    # injected ``every_iteration`` cadence, no sampling, no random
    # source). A regression anywhere in the chain shows up here as a
    # cleanly-named diff.
    # ------------------------------------------------------------------ #
    assert verdicts == [
        ("continue", "in_progress"),
        ("continue", "in_progress"),
        ("adjust", "heuristic_unproductive"),
        ("adjust", "heuristic_unproductive"),
        ("terminate", "no_progress"),
    ]

    # ------------------------------------------------------------------ #
    # Ordering invariant — restated from the brief: an ``adjust``
    # verdict must appear strictly before any ``terminate`` verdict.
    # The verdict-sequence assertion above already implies this, but
    # restating the invariant explicitly here documents the brief's
    # acceptance criterion for readers scanning the file.
    # ------------------------------------------------------------------ #
    first_adjust = next(i for i, (verdict, _) in enumerate(verdicts) if verdict == "adjust")
    first_terminate = next(i for i, (verdict, _) in enumerate(verdicts) if verdict == "terminate")
    assert first_adjust < first_terminate
    # Half-threshold invariant — the brief calls out that the heuristic
    # fires at ``ceil(stagnation_threshold / 2) = 2`` iterations once
    # the tool-name sequence has repeated three times in a row. With
    # the deterministic fallback re-running the same single-call
    # Strategy every iteration, the third repeat lands on iteration 2.
    assert first_adjust == 2

    # ------------------------------------------------------------------ #
    # Counter invariant — the post-iteration counter values reflect the
    # walk above. The post-iteration update runs regardless of verdict
    # (the engine advances the counter before checking whether the
    # verdict is terminal), so the terminal iteration writes ``5`` into
    # the session even though no further Decide_Phase will ever read
    # it. The earlier values ``1, 2, 3, 4`` are the counters seen by
    # iterations 1, 2, 3, 4 respectively (the 0-indexed sequence is
    # one step behind because Decide_Phase reads the counter *before*
    # the update fires).
    # ------------------------------------------------------------------ #
    assert counters == [1, 2, 3, 4, 5]

    # ------------------------------------------------------------------ #
    # Rationale invariant — every ``adjust`` iteration must carry a
    # non-empty ``revision_rationale`` string (Requirement 8.6). Non-
    # adjust iterations leave the field unset; we don't pin its
    # absence because the engine never writes it on those paths.
    # ------------------------------------------------------------------ #
    for index, (verdict, _) in enumerate(verdicts):
        if verdict == "adjust":
            rationale = rationales[index]
            assert isinstance(rationale, str) and rationale, (
                f"iteration {index} produced ``adjust`` but the persisted "
                "revision_rationale is missing or empty"
            )

    # ------------------------------------------------------------------ #
    # Persistence invariant — the session is now in the ``terminated``
    # status and the persisted iteration list matches the count.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"
    assert persisted["final_verdict"] == "terminate"
    assert len(persisted["iterations"]) == len(verdicts)


@pytest.mark.mission_e2e
async def test_terminate_on_no_progress(tmp_path: Path) -> None:
    """The cascade terminates on ``no_progress`` once the counter hits the cap.

    Same setup as :func:`test_adjust_fires_before_terminate` — an
    unreachable Criterion, ``every_iteration`` cadence,
    ``stagnation_threshold=4`` — but this test focuses purely on the
    terminal verdict pair rather than the heuristic-sequence walk:

    * the run reaches a terminal verdict within the driver-loop bound,
    * the terminal verdict pair is ``("terminate", "no_progress")``,
    * the persisted session is in the ``terminated`` status with
      ``final_verdict="terminate"``,
    * a Final_Report exists at the path the session records.

    The Final_Report assertion mirrors the precedent budget-cap test
    (:mod:`tests.test_mission_e2e_budget`) — the report is the
    durable exit artifact every Mission session produces, and a
    no-progress termination must produce one too.
    """
    backend = FilesystemBackend(root=tmp_path)
    session = _make_session(session_id="sess-stagnation-terminate")
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_unmet_metric_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
    )

    final_record: dict[str, Any] | None = None
    for _ in range(_DRIVER_LOOP_BOUND):
        record = await engine.run_iteration(session["session_id"])
        if record["verdict"] in ("complete", "terminate"):
            final_record = record
            break
    else:  # pragma: no cover — safety bound
        pytest.fail(
            "Mission did not reach a terminal verdict within the driver "
            "loop bound; no-progress detection may be misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — the no-progress branch is the only branch
    # that could have fired (completion is locked off by the
    # unreachable Criterion; the iteration cap is generous; the
    # wall-clock and cost branches have no caps in scope).
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "terminate"
    assert final_record["verdict_reason"] == "no_progress"

    # ------------------------------------------------------------------ #
    # Counter invariant — the cascade fires when the counter reaches
    # ``stagnation_threshold``. The post-iteration update on the
    # iteration immediately *before* the terminal one wrote the
    # threshold value into the session; the terminal iteration's
    # Decide_Phase reads it and fires the no-progress branch.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["no_progress_counter"] >= _STAGNATION_THRESHOLD

    # ------------------------------------------------------------------ #
    # Persistence invariant — the session is now in the ``terminated``
    # status with the matching final verdict label.
    # ------------------------------------------------------------------ #
    assert persisted["status"] == "terminated"
    assert persisted["final_verdict"] == "terminate"

    # ------------------------------------------------------------------ #
    # Final_Report invariant — the durable exit artifact is on disk.
    # An operator querying the session through ``mission_status`` or
    # ``mission://sessions/<id>/report`` must find it. Same assertion
    # the precedent budget-cap test makes.
    # ------------------------------------------------------------------ #
    report_path = Path(persisted["final_report_path"])
    assert report_path.exists()
