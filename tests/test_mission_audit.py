"""End-to-end audit-log reconstruction test for ``MissionEngine``.

Drives a complete Mission session through several iterations to a
terminal verdict, captures every audit entry the engine emits, and
rebuilds the iteration history from those entries alone. The rebuilt
shape must match the persisted session JSON's ``iterations`` list on
the fields that the audit stream is expected to fully describe:

* ``iteration_index``,
* the ordered list of ``(phase, status)`` pairs,
* the terminal ``verdict`` and ``verdict_reason``.

The Mission package routes every emitter through
``logging.getLogger("gco.mcp.audit")`` and pytest's ``caplog`` fixture
captures records from any propagating logger, so we set the level on
that logger and parse each captured record's message as JSON. We
deliberately filter by ``event_type`` so unrelated audit emitters in
the test process (the MCP tool-invocation decorator, the startup-log
helper, sampling events, etc.) cannot leak into the reconstruction.

Determinism: the dispatcher always returns the same metrics-don't-meet
result, the cadence is ``every_iteration``, and the stagnation
threshold is set high enough that the strategy-revision heuristic
cannot fire inside the four-iteration window. With
``max_iterations=4`` the verdict cascade closes out the run on the
fourth iteration with ``("terminate", "max_iterations")`` — a
predictable shape the assertions can pin against.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

# ``mcp/run_mcp.py`` adds ``mcp/`` to ``sys.path`` at runtime; pytest has
# to do the same before any ``mission.*`` import resolves. Mirrors the
# pattern used by every other ``test_mission_*`` module.
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission.audit import EVENT_TYPE_PHASE, EVENT_TYPE_VERDICT  # noqa: E402
from mission.engine import MissionEngine  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    session_id: str = "sess-audit-recon",
    max_iterations: int = 4,
    tool_allowlist: list[str] | None = None,
    criteria: list[dict[str, Any]] | None = None,
    stagnation_threshold: int = 100,
) -> dict[str, Any]:
    """Build a minimal ``SessionState`` dict by hand for a multi-iteration run.

    Bypasses the validators on purpose: the engine consumes the typed
    fields directly, and the validators are covered by their own test
    module. Defaults are tuned so the loop runs cleanly to a
    ``max_iterations`` termination on the 4th iteration:

    * The criterion's target is unreachable, so it never reports ``met``
      and the completion branch in the verdict cascade never fires.
    * ``every_iteration`` cadence means every iteration is an evaluated
      checkpoint — no synthetic ``cadence_skip`` verdicts to muddy the
      audit stream.
    * A high ``stagnation_threshold`` keeps both clauses of the
      strategy-revision heuristic dormant: clause (a) needs the
      no-progress counter at half the threshold (50) and clause (b)
      needs new observation errors, neither of which can happen in a
      4-iteration window with a stable, non-erroring dispatcher.
    """
    if tool_allowlist is None:
        tool_allowlist = ["fake_tool"]
    if criteria is None:
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
        "directive_text": f"Drive {session_id} through several iterations.",
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
        "accumulated_cost_usd": 0.0,
    }


def _collect_mission_entries(
    caplog: pytest.LogCaptureFixture,
) -> list[dict[str, Any]]:
    """Return the JSON-decoded Mission audit entries from ``caplog``.

    Filters by ``event_type`` so this test does not pick up
    ``mcp.tool.invocation`` or ``mcp.server.startup`` records that may
    flow through the same logger from unrelated code paths. The
    surviving entries preserve the order in which the engine emitted
    them, which is the order the reconstruction algorithm walks.
    """
    wanted_event_types = {EVENT_TYPE_PHASE, EVENT_TYPE_VERDICT}
    entries: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != "gco.mcp.audit":
            continue
        try:
            payload = json.loads(record.getMessage())
        except TypeError, ValueError:
            # Anything that isn't a JSON object is by definition not a
            # Mission audit entry — the emitters in ``mission.audit``
            # always serialise via ``json.dumps``.
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("event_type") in wanted_event_types:
            entries.append(payload)
    return entries


def _reconstruct_iterations(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild the iteration history from a stream of audit entries.

    Each iteration emits exactly five ``mission_phase_event`` entries
    (one per phase) followed by one ``mission_verdict_event`` entry
    that closes it out. The walk accumulates phase events into the
    current iteration's ``phases`` list, then a verdict event stamps
    the verdict + reason and appends the completed record. The
    iteration-boundary safety branch (a phase event arriving with a
    new ``iteration_index`` while the prior iteration's verdict has
    not yet landed) is kept so a malformed audit stream surfaces as a
    visible mismatch rather than a silent merge.

    The reconstruction intentionally keeps only the fields the audit
    stream is expected to fully describe — phase / status pairs, the
    iteration index, the verdict and reason — so the final equality
    check stays focused on the audit-completeness invariant rather
    than on details (timestamps, sampling status, observation
    contents) that the audit log does not pretend to mirror.
    """
    reconstructed: list[dict[str, Any]] = []
    current_iteration: int | None = None
    current_phases: list[dict[str, str]] = []

    for entry in entries:
        event_type = entry.get("event_type")
        iteration_index = entry.get("iteration_index")

        if event_type == EVENT_TYPE_PHASE:
            if current_iteration is not None and current_iteration != iteration_index:
                # Defensive flush: the next phase event jumped to a
                # new iteration before the prior iteration's verdict
                # arrived. Mark the unclosed iteration with sentinel
                # verdict / reason values so a comparison against the
                # persisted shape fails loudly rather than silently
                # absorbing the orphaned phases.
                reconstructed.append(
                    {
                        "iteration_index": current_iteration,
                        "phases": current_phases,
                        "verdict": None,
                        "verdict_reason": None,
                    }
                )
                current_phases = []
            current_iteration = iteration_index
            current_phases.append(
                {
                    "phase": entry["phase"],
                    "status": entry["phase_status"],
                }
            )
        elif event_type == EVENT_TYPE_VERDICT:
            reconstructed.append(
                {
                    "iteration_index": iteration_index,
                    "phases": current_phases,
                    "verdict": entry["verdict"],
                    "verdict_reason": entry["verdict_reason"],
                }
            )
            current_iteration = None
            current_phases = []

    return reconstructed


def _shape_persisted_iterations(
    persisted_iterations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project the persisted iterations down to the reconstruction's shape.

    The persisted ``IterationRecord`` carries far more than the audit
    stream — strategy, observation, criteria evaluation, timestamps,
    optional sampling fields. The audit stream is only expected to
    cover the four scoped fields, so we project both sides to the
    same shape before comparing. Any drift on the audit-completeness
    invariant surfaces as an inequality on the projected dicts.
    """
    shaped: list[dict[str, Any]] = []
    for iteration in persisted_iterations:
        shaped.append(
            {
                "iteration_index": iteration["iteration_index"],
                "phases": [
                    {"phase": phase["phase"], "status": phase["status"]}
                    for phase in iteration["phases"]
                ],
                "verdict": iteration["verdict"],
                "verdict_reason": iteration["verdict_reason"],
            }
        )
    return shaped


# ---------------------------------------------------------------------------
# The reconstruction test
# ---------------------------------------------------------------------------


async def test_audit_log_reconstructs_full_iteration_history(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A complete Mission run can be rebuilt from its audit stream alone.

    The session is configured to terminate cleanly on its fourth
    iteration via the ``max_iterations`` cap. Every iteration emits
    five ``mission_phase_event`` entries plus one
    ``mission_verdict_event``, so a four-iteration run produces 20 +
    4 = 24 Mission audit entries. Reconstructing those 24 entries
    must reproduce the persisted session JSON's iteration history on
    the audit-scoped fields: index, ordered phase / status pairs, and
    terminal verdict + reason.
    """

    backend = FilesystemBackend(root=tmp_path)

    async def dispatcher(tool_name: str, args: dict, ctx: Any) -> dict:
        # Stable, non-erroring metrics that the unreachable criterion
        # cannot satisfy. Same call → same result, so no clause of the
        # strategy-revision heuristic can latch onto an emerging
        # error pattern between iterations.
        return {"metrics": {"loss": 0.5}}

    session = _make_session()
    backend.save_session(session)

    engine = MissionEngine(
        backend=backend,
        tool_dispatcher=dispatcher,
        sampling_callable=None,
        sandbox_runner=None,
        cost_estimators={},
    )

    # Drive iterations until the verdict cascade ends the run. The
    # 10-iteration safety bound is well above the configured
    # ``max_iterations=4`` so a regression in the cap-enforcement
    # logic surfaces as a visible test failure here rather than as
    # an infinite loop.
    with caplog.at_level(logging.INFO, logger="gco.mcp.audit"):
        for _ in range(10):
            record = await engine.run_iteration(session["session_id"])
            if record["verdict"] in ("complete", "terminate"):
                break
        else:  # pragma: no cover - safety bound
            pytest.fail(
                "Mission did not reach a terminal verdict within the safety bound; "
                "verdict cascade may be misconfigured."
            )

    # ------------------------------------------------------------------ #
    # Persisted shape — what we expect the engine to have written.
    # ------------------------------------------------------------------ #

    persisted = backend.load_session(session["session_id"])
    assert persisted is not None
    assert persisted["status"] == "terminated"
    assert len(persisted["iterations"]) == 4

    final_iteration = persisted["iterations"][-1]
    assert final_iteration["verdict"] == "terminate"
    assert final_iteration["verdict_reason"] == "max_iterations"

    # ------------------------------------------------------------------ #
    # Audit shape — what the audit stream alone must let us rebuild.
    # ------------------------------------------------------------------ #

    entries = _collect_mission_entries(caplog)

    phase_entries = [e for e in entries if e["event_type"] == EVENT_TYPE_PHASE]
    verdict_entries = [e for e in entries if e["event_type"] == EVENT_TYPE_VERDICT]

    # Four iterations × five phases = twenty phase events; one verdict
    # event per iteration = four verdict events. Any deviation here
    # signals an emitter regression before we get to the structural
    # equality check below.
    assert len(phase_entries) == 4 * 5
    assert len(verdict_entries) == 4

    # Every Mission entry must carry the session id so consumers can
    # demultiplex multi-session audit streams. Catching this here
    # keeps an emitter regression on ``mission_session_id`` from
    # being papered over by the structural equality below.
    for entry in entries:
        assert entry["mission_session_id"] == session["session_id"]

    reconstructed = _reconstruct_iterations(entries)
    expected = _shape_persisted_iterations(persisted["iterations"])

    assert reconstructed == expected
