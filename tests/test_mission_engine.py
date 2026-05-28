"""Engine-level happy-path and failure-path tests for ``MissionEngine``.

Five end-to-end cases that exercise ``MissionEngine.run_iteration``
through a real :class:`FilesystemBackend` against ``tmp_path``, with
every external dependency stubbed:

* ``test_run_iteration_happy_path_continue`` — one normal iteration that
  evaluates ``in_progress``. Confirms all five phases land as
  ``succeeded`` on the persisted iteration record.
* ``test_run_iteration_completes_on_criteria_met`` — the sole criterion
  evaluates ``met`` on iteration 0, so the verdict is ``complete`` and a
  Final_Report file is written next to the session JSON.
* ``test_run_iteration_terminates_on_max_iterations`` — with
  ``max_iterations=1`` the first iteration's Decide_Phase fires
  ``max_iterations`` (because ``len(iterations) + 1 >= 1``), so the
  second call refuses with ``session_terminal``.
* ``test_run_iteration_records_failure_on_phase_exception`` — an empty
  ``tool_allowlist`` makes the deterministic Propose_Phase raise
  ``MissionEngineError("propose_no_tool_available")``. The engine
  records the failed phase, marks the session ``failed``, and refuses
  subsequent calls with ``session_failed``.
* ``test_run_iteration_emits_one_audit_event_per_phase`` —
  ``mission.engine.audit.emit_phase_event`` is monkeypatched; a single
  successful iteration produces exactly five calls (one per phase)
  carrying the right ``session_id`` and ``iteration_index`` values.

Construction note: every session is built directly via the
``_make_session`` helper rather than the validators. The validators are
exercised in ``tests/test_mission_validation.py`` and are out of scope
for engine-level behaviour — the engine treats the session payload as
opaque and only consumes the typed fields.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Mirror the import pattern used by every other ``test_mission_*`` module:
# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime, but pytest
# has to do it itself before the imports below resolve.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission import engine as mission_engine  # noqa: E402
from mission.engine import MissionEngine, MissionEngineError  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path) -> FilesystemBackend:
    """A fresh :class:`FilesystemBackend` rooted at ``tmp_path``.

    Each test gets an isolated directory so the report file written by
    the criteria-met test cannot leak into the happy-path test's
    listing.
    """
    return FilesystemBackend(root=tmp_path)


def _make_session(
    *,
    session_id: str = "sess-engine-001",
    max_iterations: int = 10,
    tool_allowlist: list[str] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    stagnation_threshold: int = 10,
) -> dict[str, Any]:
    """Build a minimally-populated ``SessionState`` dict by hand.

    Bypasses the validators on purpose: the engine consumes the typed
    fields directly, and the validators are covered by their own test
    module. Defaults are tuned so a stock session goes through the
    five-phase loop without tripping budget caps or the stagnation
    threshold — individual tests override the keys they care about.
    """
    if tool_allowlist is None:
        tool_allowlist = ["fake_tool"]
    if criteria is None:
        # An unreachable target so the metric_threshold stays "unmet"
        # for the happy-path case. Tests that want completion supply
        # their own criterion list.
        criteria = [
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "loss",
                "op": "<",
                "target": -1.0,
            }
        ]
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": f"Drive {session_id} to a stable state.",
        "criteria": criteria,
        "budget": {
            "max_iterations": max_iterations,
            "max_wall_clock_seconds": 600,
        },
        "tool_allowlist": tool_allowlist,
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": stagnation_threshold,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


def _make_engine(
    backend_obj: FilesystemBackend,
    dispatcher: Any,
) -> MissionEngine:
    """Construct a ``MissionEngine`` with stubbed sampling and sandbox.

    The engine takes a positional dataclass argument set; using keyword
    args here keeps the test readable when the dataclass grows new
    optional fields.
    """
    return MissionEngine(
        backend=backend_obj,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path, verdict=continue
# ---------------------------------------------------------------------------


async def test_run_iteration_happy_path_continue(
    backend: FilesystemBackend,
) -> None:
    """One iteration with no completion → ``continue`` / ``in_progress``.

    Walks the full five-phase cycle and checks the persisted session
    JSON: one ``IterationRecord`` with five phase entries all
    ``succeeded`` and the verdict pair the cascade produces when no
    budget cap, completion check, or strategy-revision heuristic
    fires.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # The default ``loss`` criterion has an unreachable target
        # (``< -1.0``) so the metric is reported but stays ``unmet``.
        return {"some": "result"}

    session = _make_session()
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    record = await engine.run_iteration(session["session_id"])

    assert record["verdict"] == "continue"
    assert record["verdict_reason"] == "in_progress"

    # The persisted session is the canonical shape; assert against
    # what's on disk rather than the in-memory return value to also
    # cover the save_session path.
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert len(persisted["iterations"]) == 1

    iteration = persisted["iterations"][0]
    assert iteration["iteration_index"] == 0

    phase_names = [phase["phase"] for phase in iteration["phases"]]
    assert phase_names == ["propose", "execute", "observe", "evaluate", "decide"]

    statuses = [phase["status"] for phase in iteration["phases"]]
    assert statuses == ["succeeded"] * 5

    # Status transitioned ``pending`` → ``running`` on the first
    # iteration; nothing in the cascade promotes it further when the
    # verdict is ``continue``.
    assert persisted["status"] == "running"


# ---------------------------------------------------------------------------
# Test 2 — completion when criteria are met
# ---------------------------------------------------------------------------


async def test_run_iteration_completes_on_criteria_met(
    backend: FilesystemBackend, tmp_path: Path
) -> None:
    """Dispatcher returns metrics that satisfy the criterion → ``complete``.

    With ``max_iterations=10`` the budget cap cannot fire on the first
    iteration, so the completion branch wins: every required Criterion
    has status ``met`` and none are ``inconclusive``. The Final_Report
    helper writes ``<root>/<session_id>.report.json`` alongside the
    session JSON.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # The ``metrics`` key on a tool result is permissively merged
        # into the Observation by the Observe_Phase, which in turn is
        # consumed by the metric_threshold evaluator.
        return {"metrics": {"loss": 0.05}}

    session = _make_session(
        session_id="sess-complete",
        criteria=[
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                # Dot-path walks the Observation: ``metrics.loss`` reaches
                # ``observation["metrics"]["loss"]`` where the Observe_Phase
                # has merged the top-level ``metrics`` dict from the tool
                # result.
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            }
        ],
    )
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    record = await engine.run_iteration(session["session_id"])

    assert record["verdict"] == "complete"
    assert record["verdict_reason"] == "criteria_met"

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "completed"
    assert persisted["final_verdict"] == "complete"

    # Final_Report sibling file is the durable exit artifact.
    expected_report_path = tmp_path / f"{session['session_id']}.report.json"
    assert expected_report_path.exists()
    assert persisted["final_report_path"] == str(expected_report_path)


# ---------------------------------------------------------------------------
# Test 3 — terminate on max_iterations, second call refuses
# ---------------------------------------------------------------------------


async def test_run_iteration_terminates_on_max_iterations(
    backend: FilesystemBackend,
) -> None:
    """``max_iterations=1`` ends the run after one iteration.

    The Decide_Phase cascade compares ``len(session["iterations"]) + 1``
    against ``max_iterations`` so the very first iteration's verdict
    is ``("terminate", "max_iterations")`` and the session transitions
    to ``terminated``. The second ``run_iteration`` call refuses with
    ``session_terminal`` because the session is now in a terminal
    state.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"some": "result"}

    session = _make_session(session_id="sess-maxiter", max_iterations=1)
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    record = await engine.run_iteration(session["session_id"])

    assert record["verdict"] == "terminate"
    assert record["verdict_reason"] == "max_iterations"

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"

    with pytest.raises(MissionEngineError) as excinfo:
        await engine.run_iteration(session["session_id"])
    assert excinfo.value.code == "session_terminal"


# ---------------------------------------------------------------------------
# -1 sentinel: max_iterations disabled
# ---------------------------------------------------------------------------


async def test_run_iteration_max_iterations_minus_one_does_not_terminate(
    backend: FilesystemBackend,
) -> None:
    """``max_iterations=-1`` disables the iteration cap branch entirely.

    The cascade reads the cap value before comparing — when the
    sentinel is set, the iteration-count branch never fires. With an
    unreachable Criterion and the cap disabled, the loop has to
    survive past 100 iterations driven only by the deterministic
    ``continue`` / ``in_progress`` verdict; we drive the engine in a
    bounded loop and assert no iteration came back as ``terminate``
    via the iteration cap.

    A regression that compared ``len(iterations)+1 >= -1`` raw
    (always True) would terminate on iteration 0, so the test fails
    loudly on that path.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"some": "result"}

    session = _make_session(
        session_id="sess-uncapped-iter",
        max_iterations=-1,
        stagnation_threshold=10_000,
    )
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    # Drive 105 iterations. With ``max_iterations=-1`` the cascade
    # cannot fire ``("terminate", "max_iterations")`` at all, so every
    # verdict must be a non-terminal one.
    for _ in range(105):
        record = await engine.run_iteration(session["session_id"])
        assert record["verdict"] not in ("terminate", "complete"), (
            f"Loop terminated unexpectedly on iteration "
            f"{record['iteration_index']}: "
            f"{record['verdict']} / {record['verdict_reason']}"
        )

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert len(persisted["iterations"]) == 105
    assert persisted["status"] == "running"


# ---------------------------------------------------------------------------
# Test 4 — phase exception → session marked failed, subsequent calls refuse
# ---------------------------------------------------------------------------


async def test_run_iteration_records_failure_on_phase_exception(
    backend: FilesystemBackend,
) -> None:
    """An empty ``tool_allowlist`` triggers a Propose_Phase exception.

    The deterministic-fallback path in ``_deterministic_strategy``
    raises ``MissionEngineError("propose_no_tool_available")`` when
    the allowlist is empty and there's no prior successful call. The
    engine's ``run_iteration`` catches the exception, appends the
    partial iteration with the failed phase recorded, marks the
    session ``failed``, persists, and re-raises. Subsequent calls
    refuse with ``session_failed``.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # Should never be called — Propose_Phase raises before the
        # Execute_Phase ever runs.
        return {"some": "result"}

    session = _make_session(session_id="sess-fail", tool_allowlist=[])
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    with pytest.raises(MissionEngineError) as excinfo:
        await engine.run_iteration(session["session_id"])
    assert excinfo.value.code == "propose_no_tool_available"

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert len(persisted["iterations"]) == 1

    iteration = persisted["iterations"][0]
    # Only the propose phase ran — the failure stops the cascade
    # before execute / observe / evaluate / decide are entered.
    failed_phases = [p for p in iteration["phases"] if p["status"] == "failed"]
    assert len(failed_phases) == 1
    failed = failed_phases[0]
    assert failed["phase"] == "propose"
    assert failed["status"] == "failed"
    assert "error_message" in failed
    assert "propose_no_tool_available" in failed["error_message"]

    # Subsequent calls on a failed session must refuse with the
    # ``session_failed`` code so callers can tell the failure mode
    # apart from the other terminal states.
    with pytest.raises(MissionEngineError) as excinfo2:
        await engine.run_iteration(session["session_id"])
    assert excinfo2.value.code == "session_failed"


# ---------------------------------------------------------------------------
# Test 5 — exactly five emit_phase_event calls per successful iteration
# ---------------------------------------------------------------------------


async def test_run_iteration_emits_one_audit_event_per_phase(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch ``emit_phase_event`` and confirm one call per phase.

    The engine imports ``from . import audit`` so monkeypatching the
    attribute on ``mission.engine.audit`` is enough — every emit call
    inside the engine resolves through that module reference. We
    record the kwargs each call carries and assert the
    ``session_id`` / ``iteration_index`` pair matches the expected
    values for every phase.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"some": "result"}

    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(mission_engine.audit, "emit_phase_event", capture)

    session = _make_session(session_id="sess-audit")
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    await engine.run_iteration(session["session_id"])

    # Exactly five — one per phase — for a successful iteration.
    assert len(calls) == 5

    phases = [call["phase"] for call in calls]
    assert phases == ["propose", "execute", "observe", "evaluate", "decide"]

    for call in calls:
        # The engine passes ``session_id`` and ``iteration_index`` as
        # keyword arguments to ``emit_phase_event``; the audit module
        # then renames ``session_id`` to ``mission_session_id`` in the
        # log entry. We assert against the kwargs the engine passes.
        assert call["session_id"] == session["session_id"]
        assert call["iteration_index"] == 0
        assert call["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Test 6 — counter resets when any criterion improves
# ---------------------------------------------------------------------------


async def test_no_progress_counter_resets_on_criterion_improvement(
    backend: FilesystemBackend,
) -> None:
    """An improvement on any criterion resets ``no_progress_counter`` to 0.

    Two criteria. ``c1`` is required with an unreachable target
    (``< -1.0``) so completion can never fire — the loop is forced to
    keep running across both iterations regardless of what ``c2`` does.
    ``c2`` is non-required and flips from ``unmet`` to ``met`` between
    iteration 0 and iteration 1: the dispatcher returns ``loss=0.5`` on
    its first call (above ``c2``'s ``< 0.1`` target) and ``loss=0.05``
    on every subsequent call.

    Counter math:

    * Iteration 0 — no prior evaluated iteration to compare against, so
      the post-iteration update bumps the counter from 0 to 1.
    * Iteration 1 — ``_criteria_improved`` sees ``c2`` went
      ``unmet → met``; the counter resets to 0.
    """

    call_count = {"n": 0}

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # Stateful: first call leaves c2 unmet, every later call meets
        # it. The engine doesn't surface the call sequence anywhere
        # else, so a closure-counter is the simplest way to drive the
        # transition.
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"metrics": {"loss": 0.5}}
        return {"metrics": {"loss": 0.05}}

    session = _make_session(
        session_id="sess-counter-reset",
        max_iterations=20,
        criteria=[
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                # Required + unreachable: pins completion off so the
                # loop keeps running and the post-iteration counter
                # update is observable on iteration 1.
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                "target": -1.0,
            },
            {
                "criterion_id": "c2",
                "kind": "metric_threshold",
                # Non-required so its ``met`` status doesn't help drive
                # completion; it exists only to provide an
                # ``unmet → met`` transition for the improvement check.
                "required": False,
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            },
        ],
    )
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    # Iteration 0 — c2 unmet; no prior eval to compare against, so the
    # post-iteration update increments the counter.
    await engine.run_iteration(session["session_id"])
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["no_progress_counter"] == 1

    # Iteration 1 — c2 met; ``_criteria_improved`` resets the counter.
    await engine.run_iteration(session["session_id"])
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["no_progress_counter"] == 0

    # Anchor the assertion on the actual c2 status pair so a future
    # change to the metric_threshold evaluator that breaks the
    # transition would surface here rather than masquerading as a
    # counter regression.
    iter_0_eval = persisted["iterations"][0]["criteria_evaluation"]
    iter_1_eval = persisted["iterations"][1]["criteria_evaluation"]
    c2_iter_0 = next(r for r in iter_0_eval if r["criterion_id"] == "c2")
    c2_iter_1 = next(r for r in iter_1_eval if r["criterion_id"] == "c2")
    assert c2_iter_0["status"] == "unmet"
    assert c2_iter_1["status"] == "met"


# ---------------------------------------------------------------------------
# Test 7 — counter only advances on evaluated (non-skip) iterations
# ---------------------------------------------------------------------------


async def test_no_progress_counter_only_advances_on_evaluated_iterations(
    backend: FilesystemBackend,
) -> None:
    """Cadence-skipped iterations leave ``no_progress_counter`` alone.

    With ``every_n_iterations`` cadence at ``n=2``, the cascade emits
    ``("continue", "cadence_skip")`` on iterations whose
    ``(iteration_index + 1) % 2 != 0`` and a real verdict on the rest.
    For a five-iteration run the schedule is skip / eval / skip / eval
    / skip — so the counter advances exactly twice (once on iteration 1,
    once on iteration 3) and stays put on the three skips.

    Criteria stay permanently unmet (unreachable target) so no
    improvement-driven reset can confound the counter sequence.
    ``stagnation_threshold=10`` keeps both the no-progress branch and
    the heuristic's clause (a) — which needs the counter to reach
    ``ceil(10/2)=5`` — out of the picture across the whole run.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # Constant unmet metric: every evaluated iteration produces the
        # same ``unmet`` row so the only thing that can move the
        # counter is the engine's checkpoint-evaluated short-circuit.
        return {"metrics": {"loss": 0.5}}

    session = _make_session(
        session_id="sess-cadence-skip",
        max_iterations=20,
        stagnation_threshold=10,
        criteria=[
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                # Unreachable so completion never fires.
                "target": -1.0,
            }
        ],
    )
    # Override the helper's default ``every_iteration`` cadence with
    # the schedule the test needs. Bypassing the validator is
    # consistent with the rest of this module — the engine consumes
    # the typed fields directly.
    session["checkpoint_cadence"] = {"kind": "every_n_iterations", "n": 2}
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    counters: list[int] = []
    evaluated_flags: list[bool] = []
    for _ in range(5):
        await engine.run_iteration(session["session_id"])
        persisted = backend.load_session(session["session_id"])
        assert persisted is not None
        counters.append(persisted["no_progress_counter"])
        evaluated_flags.append(persisted["iterations"][-1]["checkpoint_evaluated"])

    # Cadence schedule for n=2: skip, evaluate, skip, evaluate, skip.
    # The counter increments only on the two ``True`` rows.
    assert evaluated_flags == [False, True, False, True, False]
    assert counters == [0, 1, 1, 2, 2]


# ---------------------------------------------------------------------------
# Test 8 — terminate when no_progress_counter reaches stagnation_threshold
# ---------------------------------------------------------------------------


async def test_terminate_on_stagnation_threshold(
    backend: FilesystemBackend,
) -> None:
    """``no_progress_counter ≥ stagnation_threshold`` ends the session.

    Dispatcher always returns the same unmet metric so no criterion
    ever improves; the counter advances on every evaluated iteration.
    With ``every_iteration`` cadence and ``stagnation_threshold=3``,
    the four-iteration sequence at the top of Decide_Phase is
    ``counter=0, 1, 2, 3``. The fourth iteration's cascade compares
    ``3 ≥ 3`` and fires ``("terminate", "no_progress")`` before any
    other branch (completion, cadence, the heuristic) runs.

    Iterations 0–2 stay non-terminal. Iteration 2 in particular returns
    ``("adjust", "heuristic_unproductive")`` once the counter reaches
    ``ceil(3/2)=2`` and the loop has three repeated tool-name
    sequences in a row — that's still not a terminal verdict, so the
    loop continues to iteration 3 where the cap fires.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # ``loss=0.5`` against a ``< 0.1`` target stays ``unmet`` on
        # every iteration, so no criterion can ever trigger an
        # improvement-driven reset.
        return {"metrics": {"loss": 0.5}}

    session = _make_session(
        session_id="sess-stagnation",
        max_iterations=20,
        stagnation_threshold=3,
        criteria=[
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            }
        ],
    )
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)

    # First three iterations advance the counter from 0 to 3 without
    # crossing the cap. The exact verdicts are deliberately not pinned
    # here — the heuristic may fire ``adjust`` once the counter reaches
    # the half-threshold — but none of them are terminal.
    for _ in range(3):
        record = await engine.run_iteration(session["session_id"])
        assert record["verdict"] not in ("terminate", "complete")

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["no_progress_counter"] == 3
    assert persisted["status"] == "running"

    # Fourth iteration — ``counter=3 ≥ threshold=3`` at the top of the
    # cascade emits the no-progress termination.
    record = await engine.run_iteration(session["session_id"])
    assert record["verdict"] == "terminate"
    assert record["verdict_reason"] == "no_progress"

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"
    assert persisted["final_verdict"] == "terminate"


# ---------------------------------------------------------------------------
# Test 9 — sandbox wiring: scripted Strategy executes through the runner
# ---------------------------------------------------------------------------


async def test_run_iteration_dispatches_script_through_sandbox_runner(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scripted Strategy routes through the wired ``sandbox_runner``.

    The engine's ``_propose_phase`` builds a tool-calls Strategy by
    default; this test patches the deterministic-strategy helper to
    return a script-bearing Strategy instead. The wired
    ``sandbox_runner`` records the call, returns a hand-built
    ``(observation, script_call_log)`` pair, and the engine threads
    both onto the persisted iteration record.
    """

    sandbox_calls: list[dict[str, Any]] = []

    async def fake_sandbox_runner(
        script: str,
        ctx: Any,
        tool_dispatcher: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        sandbox_calls.append({"script": script, "ctx": ctx})
        observation: dict[str, Any] = {
            "tool_results": [{"some": "result"}],
            "metrics": {"loss": 0.5},
            "events": [],
            "phase_started_at": "2025-01-01T00:00:00+00:00",
            "phase_ended_at": "2025-01-01T00:00:01+00:00",
        }
        script_call_log: list[dict[str, Any]] = [
            {
                "tool_name": "fake_tool",
                "args": {},
                "status": "ok",
                "result_summary": {"some": "result"},
                "duration_ms": 1,
            }
        ]
        return observation, script_call_log

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # The script runner is faked, so the real dispatcher never
        # fires — but the engine still requires a callable to
        # construct the engine.
        return {"some": "result"}

    # Replace the deterministic-strategy helper so Propose_Phase
    # produces a script-bearing Strategy. Patching the bound method
    # on the engine instance keeps the patch tightly scoped to this
    # test's engine object.
    session = _make_session(session_id="sess-script")
    backend.save_session(session)
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=fake_sandbox_runner,
    )

    def fake_deterministic_strategy(self_engine: Any, sess: dict) -> dict:
        return {
            "script": "mission.observe('done', True)\n",
            "rationale": "test fixture: scripted Strategy",
        }

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    record = await engine.run_iteration(session["session_id"])

    # Sandbox runner was invoked exactly once with the script body.
    assert len(sandbox_calls) == 1
    assert "mission.observe" in sandbox_calls[0]["script"]

    # The engine threaded the runner's outputs onto the iteration.
    assert record["script_call_log"] == [
        {
            "tool_name": "fake_tool",
            "args": {},
            "status": "ok",
            "result_summary": {"some": "result"},
            "duration_ms": 1,
        }
    ]
    assert record["observation"]["metrics"] == {"loss": 0.5}

    # All five phases ran successfully — the script path produces a
    # valid Observation, so Observe / Evaluate / Decide all succeed.
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    iteration = persisted["iterations"][0]
    statuses = [phase["status"] for phase in iteration["phases"]]
    assert statuses == ["succeeded"] * 5


# ---------------------------------------------------------------------------
# Test 10 — sandbox wiring: scripted Strategy without a wired runner rejects
# ---------------------------------------------------------------------------


async def test_run_iteration_rejects_script_when_sandbox_runner_unwired(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``sandbox_runner is None``, a script Strategy fails as ``script_rejected``.

    The session-start validator should have caught this already, but
    a sampled-then-injected script could still arrive at the engine.
    The engine treats any script-bearing Strategy as a validation
    failure when no sandbox runner is wired and surfaces the
    ``script_rejected`` code so the MCP / CLI layers render a
    structured error.
    """

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"some": "result"}

    session = _make_session(session_id="sess-script-unwired")
    backend.save_session(session)
    engine = _make_engine(backend, dispatcher)  # sandbox_runner=None

    def fake_deterministic_strategy(self_engine: Any, sess: dict) -> dict:
        return {
            "script": "mission.observe('done', True)\n",
            "rationale": "test fixture: scripted Strategy without sandbox",
        }

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    with pytest.raises(MissionEngineError) as excinfo:
        await engine.run_iteration(session["session_id"])
    assert excinfo.value.code == "script_rejected"

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "failed"


# ---------------------------------------------------------------------------
# Test 11 — sandbox wiring: ScriptRejected from runner translates cleanly
# ---------------------------------------------------------------------------


async def test_run_iteration_translates_script_rejected_from_runner(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ScriptRejected`` raised inside the sandbox runner becomes ``script_rejected``."""

    # Lazy import — same pattern the engine itself uses.
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    from mission.sandbox import ScriptRejected  # noqa: E402

    async def fake_sandbox_runner(
        script: str, ctx: Any, tool_dispatcher: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raise ScriptRejected("forbidden_node")

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"some": "result"}

    session = _make_session(session_id="sess-script-rejected")
    backend.save_session(session)
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=fake_sandbox_runner,
    )

    def fake_deterministic_strategy(self_engine: Any, sess: dict) -> dict:
        return {"script": "import os\n", "rationale": "fixture"}

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    with pytest.raises(MissionEngineError) as excinfo:
        await engine.run_iteration(session["session_id"])
    assert excinfo.value.code == "script_rejected"


# ---------------------------------------------------------------------------
# Test 12 — sandbox wiring: SandboxTerminated routes to a terminate verdict
# ---------------------------------------------------------------------------


async def test_run_iteration_translates_sandbox_terminated_from_runner(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SandboxTerminated`` from the sandbox runner produces a ``terminate`` verdict.

    The sandbox cap is a true budget cap (the script ran out of wall
    clock or memory), so the engine routes it through the budget-cap
    path of the Decide_Phase cascade rather than failing the iteration
    as a phase exception. The :class:`SandboxTerminated` exception
    carries any partial observations / events / script-call records
    the script collected before being killed; the engine stashes
    those on the iteration record and writes a
    ``sandbox_terminated_reason`` sentinel that
    :func:`mcp.mission.decide.decide_verdict` reads at the very top of
    its cascade to short-circuit to ``("terminate",
    "max_wall_clock")``.
    """

    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    from mission.sandbox import SandboxTerminated  # noqa: E402

    async def fake_sandbox_runner(
        script: str, ctx: Any, tool_dispatcher: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raise SandboxTerminated(
            "MontyDurationError",
            partial_observations=[{"key": "progress", "value": 0.5}],
            partial_events=[{"event_name": "halfway"}],
            partial_script_call_log=[
                {
                    "tool_name": "fake_tool",
                    "args": {"x": 1},
                    "status": "ok",
                    "result_summary": {"ok": True},
                    "duration_ms": 5,
                }
            ],
        )

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"some": "result"}

    session = _make_session(session_id="sess-sandbox-terminated")
    backend.save_session(session)
    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=fake_sandbox_runner,
    )

    def fake_deterministic_strategy(self_engine: Any, sess: dict) -> dict:
        return {"script": "while True:\n    pass\n", "rationale": "fixture"}

    monkeypatch.setattr(
        MissionEngine,
        "_deterministic_strategy",
        fake_deterministic_strategy,
    )

    record = await engine.run_iteration(session["session_id"])

    # The cascade reads ``sandbox_terminated_reason`` before any other
    # branch and produces a ``("terminate", "max_wall_clock")`` tuple.
    assert record["verdict"] == "terminate"
    assert record["verdict_reason"] == "max_wall_clock"
    # Partial logs from the killed script land on the iteration record
    # so the audit trail stays complete even when the script was cut
    # short mid-loop.
    assert record["script_call_log"] == [
        {
            "tool_name": "fake_tool",
            "args": {"x": 1},
            "status": "ok",
            "result_summary": {"ok": True},
            "duration_ms": 5,
        }
    ]
    # The session transitions through the terminal-verdict path.
    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"


# ---------------------------------------------------------------------------
# Test 13 — make_default_sandbox_runner returns the bound MissionSandbox.run
# ---------------------------------------------------------------------------


def test_make_default_sandbox_runner_returns_bound_run_method() -> None:
    """The default factory returns a callable bound to a fresh ``MissionSandbox``.

    The engine takes any callable matching the ``SandboxRunner``
    protocol; the factory wraps a per-session :class:`MissionSandbox`
    and returns its bound ``run`` method so callers do not need to
    know about the sandbox class.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    from mission.sandbox import (  # noqa: E402
        MissionSandbox,
        make_default_sandbox_runner,
    )

    session = _make_session(session_id="sess-factory")
    runner = make_default_sandbox_runner(["fake_tool"], session)

    assert callable(runner)
    # ``__self__`` carries the sandbox instance because Python binds
    # methods that way; checking the instance type pins the factory's
    # contract.
    assert isinstance(runner.__self__, MissionSandbox)  # type: ignore[attr-defined]
    assert runner.__name__ == "run"


# ---------------------------------------------------------------------------
# Test 14 — sampling wiring: SamplingUsed adopts the proposed Strategy
# ---------------------------------------------------------------------------


async def test_engine_uses_sampling_used_strategy_when_sampling_callable_returns_sampling_used(
    backend: FilesystemBackend,
) -> None:
    """``SamplingUsed`` from the sampling callable seeds the next Strategy.

    Set up a session whose first iteration's verdict is ``adjust``
    (driven by injecting a synthetic prior iteration on the session
    JSON). The wired sampling callable returns a
    :class:`mcp.mission.sampling.SamplingUsed` whose
    ``parsed["next_strategy"]`` proposes a single allowlisted tool
    call. The engine adopts the sampler's Strategy verbatim and stamps
    ``parsed["revision_rationale"]`` on the iteration record so the
    audit verdict event carries the model-derived text.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    from mission.sampling import SamplingUsed  # noqa: E402

    sampler_calls: list[dict[str, Any]] = []
    dispatcher_calls: list[tuple[str, dict]] = []

    async def sampling_callable(*, session: dict, ctx: Any) -> SamplingUsed:
        sampler_calls.append({"session_id": session["session_id"]})
        return SamplingUsed(
            output_text='{"revision_rationale":"r","next_strategy":...}',
            parsed={
                "revision_rationale": "model-derived rationale",
                "next_strategy": {
                    "tool_calls": [{"tool_name": "fake_tool", "args": {"k": "v"}}],
                    "rationale": "sampler-proposed",
                },
                "confidence": 0.5,
            },
            backend_name="mcp",
            model_id="test-model",
        )

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        dispatcher_calls.append((tool_name, dict(args)))
        return {"some": "result"}

    session = _make_session(session_id="sess-sampling-used")
    session["use_sampling"] = True
    # Synthetic prior iteration that flipped to ``adjust`` so the
    # next Propose_Phase consults the sampler.
    session["iterations"].append(
        {
            "iteration_index": 0,
            "started_at": "2025-01-01T00:00:00+00:00",
            "ended_at": "2025-01-01T00:00:01+00:00",
            "phases": [],
            "strategy": {"tool_calls": []},
            "observation": {
                "tool_results": [],
                "metrics": {},
                "events": [],
                "phase_started_at": "2025-01-01T00:00:00+00:00",
                "phase_ended_at": "2025-01-01T00:00:01+00:00",
            },
            "criteria_evaluation": [],
            "verdict": "adjust",
            "verdict_reason": "heuristic_unproductive",
            "checkpoint_evaluated": True,
        }
    )
    session["status"] = "running"
    session["started_at"] = "2025-01-01T00:00:00+00:00"
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=sampling_callable,
        sandbox_runner=None,
    )

    record = await engine.run_iteration(session["session_id"])

    # Sampler ran exactly once and the engine adopted its Strategy
    # for the dispatcher.
    assert len(sampler_calls) == 1
    assert dispatcher_calls == [("fake_tool", {"k": "v"})]

    # The model-derived rationale lands on the iteration record so
    # the verdict event surfaces it instead of the deterministic
    # template.
    assert record["revision_rationale"] == "model-derived rationale"

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    new_iter = persisted["iterations"][-1]
    assert new_iter["strategy"]["rationale"] == "sampler-proposed"
    # The executed tool call is recorded back onto the strategy with
    # the engine's standard ``status="ok"`` / ``result_summary``
    # bookkeeping.
    assert new_iter["strategy"]["tool_calls"][0]["tool_name"] == "fake_tool"
    assert new_iter["strategy"]["tool_calls"][0]["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 15 — sampling wiring: SamplingFallback routes to deterministic strategy
# ---------------------------------------------------------------------------


async def test_engine_falls_back_when_sampling_callable_returns_sampling_fallback(
    backend: FilesystemBackend,
) -> None:
    """``SamplingFallback`` triggers the deterministic Propose_Phase fallback.

    The deterministic fallback re-runs the most recent successful tool
    call. We inject a synthetic prior iteration carrying both the
    ``adjust`` verdict (so sampling is consulted) and a successful
    tool call on its strategy (so the deterministic path has
    something to re-run). The sampling callable returns a
    :class:`mcp.mission.sampling.SamplingFallback`; the engine maps it
    to ``None`` and runs the deterministic strategy instead.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    from mission.sampling import SamplingFallback  # noqa: E402

    sampler_calls: list[dict[str, Any]] = []
    dispatcher_calls: list[tuple[str, dict]] = []

    async def sampling_callable(*, session: dict, ctx: Any) -> SamplingFallback:
        sampler_calls.append({"session_id": session["session_id"]})
        return SamplingFallback(
            rationale="deterministic template text",
            reason="schema_mismatch",
            backend_name="mcp",
            model_id="test-model",
        )

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        dispatcher_calls.append((tool_name, dict(args)))
        return {"some": "result"}

    session = _make_session(session_id="sess-sampling-fallback")
    session["use_sampling"] = True
    session["status"] = "running"
    session["started_at"] = "2025-01-01T00:00:00+00:00"
    # Synthetic prior iteration with a successful tool call on its
    # strategy so the deterministic-fallback's "most recent successful
    # call" lookup has something to re-run.
    session["iterations"].append(
        {
            "iteration_index": 0,
            "started_at": "2025-01-01T00:00:00+00:00",
            "ended_at": "2025-01-01T00:00:01+00:00",
            "phases": [],
            "strategy": {
                "tool_calls": [
                    {
                        "tool_name": "fake_tool",
                        "args": {"prior": "args"},
                        "status": "ok",
                        "result_summary": {"some": "result"},
                        "duration_ms": 1,
                    }
                ],
            },
            "observation": {
                "tool_results": [{"some": "result"}],
                "metrics": {},
                "events": [],
                "phase_started_at": "2025-01-01T00:00:00+00:00",
                "phase_ended_at": "2025-01-01T00:00:01+00:00",
            },
            "criteria_evaluation": [],
            "verdict": "adjust",
            "verdict_reason": "heuristic_unproductive",
            "checkpoint_evaluated": True,
        }
    )
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=sampling_callable,
        sandbox_runner=None,
    )

    await engine.run_iteration(session["session_id"])

    # Sampler was consulted but its fallback meant the engine ran
    # the deterministic re-run-prior-args path instead.
    assert len(sampler_calls) == 1
    assert dispatcher_calls == [("fake_tool", {"prior": "args"})]

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    new_iter = persisted["iterations"][-1]
    # Deterministic-fallback rationale text — the engine builds a
    # short tag identifying the fallback path so an operator reading
    # the persisted strategy can tell sampling was rejected.
    assert "deterministic fallback" in new_iter["strategy"]["rationale"]
    # Sampler's own rationale text is *not* on the iteration record
    # because the engine routes through deterministic fallback when
    # SamplingFallback is returned.
    assert (
        "revision_rationale" not in new_iter
        or new_iter["revision_rationale"] != "deterministic template text"
    )


# ---------------------------------------------------------------------------
# Test 16 — sampling wiring: legacy raw-dict sampling_callable still works
# ---------------------------------------------------------------------------


async def test_engine_legacy_dict_sampling_callable_still_works(
    backend: FilesystemBackend,
) -> None:
    """A sampling callable that returns a raw dict keeps working unchanged.

    Existing tests (and operator wiring that pre-dates the
    Phase 6.7 ``SamplingUsed`` / ``SamplingFallback`` types) use
    ``async def sampler(...): return {"tool_calls": [...]}``. The
    engine still recognises that shape and adopts it as the next
    Strategy verbatim — this is the back-compat contract.
    """
    dispatcher_calls: list[tuple[str, dict]] = []

    async def sampling_callable(*, session: dict, ctx: Any) -> dict:
        return {
            "tool_calls": [{"tool_name": "fake_tool", "args": {"legacy": "shape"}}],
            "rationale": "legacy raw-dict path",
        }

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        dispatcher_calls.append((tool_name, dict(args)))
        return {"some": "result"}

    session = _make_session(session_id="sess-legacy-dict")
    session["use_sampling"] = True
    session["status"] = "running"
    session["started_at"] = "2025-01-01T00:00:00+00:00"
    session["iterations"].append(
        {
            "iteration_index": 0,
            "started_at": "2025-01-01T00:00:00+00:00",
            "ended_at": "2025-01-01T00:00:01+00:00",
            "phases": [],
            "strategy": {"tool_calls": []},
            "observation": {
                "tool_results": [],
                "metrics": {},
                "events": [],
                "phase_started_at": "2025-01-01T00:00:00+00:00",
                "phase_ended_at": "2025-01-01T00:00:01+00:00",
            },
            "criteria_evaluation": [],
            "verdict": "adjust",
            "verdict_reason": "heuristic_unproductive",
            "checkpoint_evaluated": True,
        }
    )
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=sampling_callable,
        sandbox_runner=None,
    )

    await engine.run_iteration(session["session_id"])

    assert dispatcher_calls == [("fake_tool", {"legacy": "shape"})]

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    new_iter = persisted["iterations"][-1]
    assert new_iter["strategy"]["rationale"] == "legacy raw-dict path"


# ---------------------------------------------------------------------------
# Test 17 — final-lessons callable: SamplingUsed overlays the report fields
# ---------------------------------------------------------------------------


async def test_engine_final_lessons_callable_overlays_lessons(
    backend: FilesystemBackend, tmp_path: Path
) -> None:
    """``final_lessons_callable`` returning ``SamplingUsed`` overlays the report.

    Drive the session to a terminal verdict (``complete``) by
    supplying a criterion the dispatcher's metrics satisfy on the
    first iteration. The wired final-lessons callable returns a
    :class:`mcp.mission.sampling.SamplingUsed` whose ``parsed`` carries
    a list of lessons and a list of follow-ups. The persisted
    Final_Report's ``lessons`` field includes the joined lesson text;
    ``recommended_followups`` mirrors the sampler's list.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))
    from mission.sampling import SamplingUsed  # noqa: E402

    final_calls: list[dict[str, Any]] = []

    async def final_lessons_callable(*, session: dict) -> SamplingUsed:
        final_calls.append({"session_id": session["session_id"]})
        return SamplingUsed(
            output_text=(
                '{"lessons":["learned X","learned Y"],"recommended_followups":["next Y"]}'
            ),
            parsed={
                "lessons": ["learned X", "learned Y"],
                "recommended_followups": ["next Y"],
            },
            backend_name="mcp",
            model_id="test-model",
        )

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # Metric satisfies the criterion on the first iteration so
        # the verdict is ``complete``.
        return {"metrics": {"loss": 0.05}}

    session = _make_session(
        session_id="sess-final-lessons",
        criteria=[
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            }
        ],
    )
    session["use_sampling"] = True
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
        final_lessons_callable=final_lessons_callable,
    )

    record = await engine.run_iteration(session["session_id"])
    assert record["verdict"] == "complete"

    # Final_Report sibling file produced by ``write_final_report``.
    report_path = tmp_path / f"{session['session_id']}.report.json"
    assert report_path.exists()
    import json

    report = json.loads(report_path.read_text(encoding="utf-8"))

    # Lessons overlay applied — both items are joined into a single
    # string per the engine's adapter.
    assert "learned X" in report["lessons"]
    assert "learned Y" in report["lessons"]
    # Follow-ups overlay applied verbatim.
    assert report["recommended_followups"] == ["next Y"]

    # Final-lessons callable was invoked exactly once.
    assert len(final_calls) == 1


# ---------------------------------------------------------------------------
# Test 18 — final-lessons callable: an exception keeps the templated text
# ---------------------------------------------------------------------------


async def test_engine_final_lessons_callable_failure_keeps_template(
    backend: FilesystemBackend, tmp_path: Path
) -> None:
    """A raising ``final_lessons_callable`` falls back to the templated report.

    The Final_Report is the durable exit artifact — a flaky sampler
    must never block it from landing. The engine swallows the
    exception inside its ``_maybe_sample_final_lessons`` helper and
    proceeds with the deterministic templates from
    :func:`mcp.mission.final_report.build_deterministic_report`.
    """

    async def final_lessons_callable(*, session: dict) -> Any:
        raise RuntimeError("model unavailable")

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        return {"metrics": {"loss": 0.05}}

    session = _make_session(
        session_id="sess-final-lessons-fail",
        criteria=[
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "metrics.loss",
                "op": "<",
                "target": 0.1,
            }
        ],
    )
    session["use_sampling"] = True
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
        final_lessons_callable=final_lessons_callable,
    )

    # No exception must propagate from ``run_iteration``.
    record = await engine.run_iteration(session["session_id"])
    assert record["verdict"] == "complete"

    # Report still landed — with the templated text.
    report_path = tmp_path / f"{session['session_id']}.report.json"
    assert report_path.exists()
    import json

    report = json.loads(report_path.read_text(encoding="utf-8"))

    # Templated lessons end with the marker line that
    # ``_build_lessons_template`` always appends.
    assert isinstance(report["lessons"], str)
    assert "templated text" in report["lessons"]
    # Templated follow-ups are a list (not the raising sampler's
    # output) and end with the same marker.
    assert isinstance(report["recommended_followups"], list)
    assert any("templated" in item for item in report["recommended_followups"])
