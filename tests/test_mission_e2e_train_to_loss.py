"""End-to-end Mission session driven to completion by a decreasing loss.

Walks one full Mission session through the engine without going near the
MCP or CLI surfaces. A stub tool dispatcher returns a ``val_loss`` metric
that decreases each iteration; a single ``metric_threshold`` criterion
declares the session complete when the metric drops to ``0.5`` or below.

Asserts four invariants:

* The verdict cascade lands on ``("complete", "criteria_met")`` within
  the configured ``max_iterations=20`` cap.
* The persisted Final_Report carries the directive verbatim — no
  truncation, no rewriting, no summarisation.
* The audit stream emits exactly one ``mission_phase_event`` per phase
  per iteration (five per iteration), confirming the engine's
  try/finally per-phase emit contract.
* The persisted ``iterations`` length matches the report's
  ``iterations_run`` field, so the durable artifact stays consistent
  with the durable session state.

The test runs offline against a :class:`FilesystemBackend` rooted at
``tmp_path`` — no AWS calls, no network, no real LLM. The dispatcher
is a closure over an :class:`itertools.chain` so the val_loss sequence
``0.9, 0.7, 0.5, 0.3, 0.1, 0.1, ...`` is reproducible across runs.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime; pytest has
# to mirror that before any ``mission.*`` import resolves. Same idiom
# used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission import engine as mission_engine  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

# Five phases per iteration; the engine's ``_run_phase`` wrapper emits
# exactly one ``mission_phase_event`` per phase regardless of outcome.
_PHASES_PER_ITERATION = 5

# Directive carried verbatim through the loop and into the Final_Report.
# Plain operator language — no references to internal planning artifacts
# so the no-spec-references guardrail stays happy.
_DIRECTIVE = (
    "Drive validation loss to or below 0.5 by iterating training jobs "
    "with adjusted hyperparameters."
)


def _make_session(*, session_id: str = "sess-train-to-loss") -> dict[str, Any]:
    """Build a minimal ``SessionState`` dict by hand.

    Bypasses the validators on purpose — the engine consumes the typed
    fields directly and the validators are exercised in their own test
    module. The shape mirrors :func:`tests.test_mission_engine._make_session`
    so any drift in the persisted contract surfaces in both places.

    Tuning notes:

    * ``max_iterations=20`` is the cap declared in the e2e brief; with
      a four-step val_loss sequence the completion branch fires on
      iteration 2, well inside that bound.
    * ``every_iteration`` cadence makes every iteration an evaluated
      checkpoint, so the verdict cascade reaches the completion check
      on every iteration without synthetic ``cadence_skip`` verdicts.
    * ``stagnation_threshold=100`` keeps the no-progress termination
      branch dormant — a four-step run cannot reach a counter of 100.
    * The single ``metric_threshold`` criterion uses ``op="<="`` and
      ``target=0.5`` so the third value in the val_loss sequence
      (``0.5``) is the moment the criterion flips to ``met``.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": _DIRECTIVE,
        "criteria": [
            {
                "criterion_id": "val_loss_target",
                "kind": "metric_threshold",
                "required": True,
                # The dispatcher returns ``{"metrics": {"val_loss": ...}}``
                # and the Observe_Phase permissively merges the top-level
                # ``metrics`` dict into the Observation, so the dot-path
                # ``metrics.val_loss`` resolves to the dispatcher value.
                "metric": "metrics.val_loss",
                "op": "<=",
                "target": 0.5,
            }
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
        "accumulated_cost_usd": 0.0,
    }


def _val_loss_dispatcher() -> Any:
    """Return an async dispatcher that emits a decreasing ``val_loss`` series.

    The sequence ``0.9, 0.7, 0.5, 0.3, then 0.1 forever`` exercises
    the criterion's ``<=`` boundary on its third value: iterations 0
    and 1 stay ``unmet`` (``0.9`` and ``0.7`` are both above ``0.5``),
    iteration 2 flips to ``met`` (``0.5 <= 0.5``), and the verdict
    cascade returns ``("complete", "criteria_met")``. The trailing
    ``0.1`` repeat is a safety bound — if the cascade misses
    completion on iteration 2 the loop keeps producing meaningful
    values rather than tripping ``StopIteration`` and crashing the
    test for an unrelated reason.

    Closure over the iterator means the dispatcher is stateful across
    calls but isolated to the test invocation — pytest's
    ``function`` fixture scope plus a fresh closure per test gives
    each run an independent sequence.
    """
    sequence = itertools.chain(iter([0.9, 0.7, 0.5, 0.3]), itertools.repeat(0.1))

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict[str, Any]:
        # The dispatcher returns the same shape an MCP tool result
        # would carry — a dict whose top-level ``metrics`` key the
        # Observe_Phase lifts into the Observation. The ``tool_name``
        # and ``args`` arguments are ignored: the stub does not need
        # to discriminate, and the engine is the single place that
        # checks Tool_Allowlist gating before this callable is even
        # invoked.
        return {"metrics": {"val_loss": next(sequence)}}

    return dispatcher


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


@pytest.mark.mission_e2e
async def test_train_to_target_loss_completes_within_max_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive a session whose ``val_loss`` sequence makes completion inevitable.

    With the dispatcher returning ``0.9, 0.7, 0.5, 0.3, 0.1, ...`` and
    the criterion ``val_loss <= 0.5`` (required), the completion
    branch in the verdict cascade fires on iteration 2:

    * iteration 0 — ``val_loss=0.9`` → unmet → ``("continue", "in_progress")``
    * iteration 1 — ``val_loss=0.7`` → unmet → ``("continue", "in_progress")``
    * iteration 2 — ``val_loss=0.5`` → met   → ``("complete", "criteria_met")``

    The test then walks the persisted artifacts to assert the four
    invariants the brief calls out: terminal verdict, directive
    fidelity in the report, exactly five phase events per iteration
    in the audit stream, and ``len(iterations) == iterations_run``.
    """
    backend = FilesystemBackend(root=tmp_path)
    session = _make_session()
    backend.save_session(session)

    # Capture every ``emit_phase_event`` call. Patching the bound
    # attribute on ``mission.engine.audit`` is sufficient because the
    # engine module references the audit module by attribute lookup —
    # ``mission_engine.audit.emit_phase_event(...)``. The test module
    # for the audit reconstruction takes the caplog route; here we
    # take the patch route for a tighter, faster assertion that does
    # not depend on logger propagation configuration.
    phase_events: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> None:
        phase_events.append(kwargs)

    monkeypatch.setattr(mission_engine.audit, "emit_phase_event", capture)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=_val_loss_dispatcher(),
        sampling_callable=None,
        sandbox_runner=None,
        cost_estimators={},
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
            "Mission did not reach a terminal verdict within max_iterations; "
            "completion detection may be misconfigured."
        )

    assert final_record is not None
    # ------------------------------------------------------------------ #
    # Verdict invariant — completion fired on the iteration where the
    # val_loss sequence first reached 0.5.
    # ------------------------------------------------------------------ #
    assert final_record["verdict"] == "complete"
    assert final_record["verdict_reason"] == "criteria_met"

    # ------------------------------------------------------------------ #
    # Persistence invariant — session is now in ``completed`` state,
    # the iteration count matches the expected three-iteration run.
    # ------------------------------------------------------------------ #
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "completed"
    assert persisted["final_verdict"] == "complete"
    assert len(persisted["iterations"]) == 3

    # Sanity-check the val_loss values the engine actually saw on each
    # iteration so a regression in the Observe_Phase metric merge
    # surfaces here rather than as a vague verdict mismatch.
    val_losses = [
        iteration["observation"]["metrics"]["val_loss"] for iteration in persisted["iterations"]
    ]
    assert val_losses == [0.9, 0.7, 0.5]

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
