"""Mission-specific audit emitters.

Three thin wrappers over the existing ``audit_logger`` from ``mcp/audit.py``.
Each emitter builds a structured dict matching the Mission audit-fields
schema and writes it as a single ``audit_logger.info(json.dumps(entry))``
call — the same pattern that ``_build_audit_entry`` and ``emit_startup_log``
use in ``mcp/audit.py``.

Three event types, one helper each:

* ``emit_phase_event`` — one per phase (propose / execute / observe /
  evaluate / decide), regardless of success or failure. Carries the
  per-phase ``started_at`` / ``ended_at`` timestamps that the engine
  measures around the phase body, plus an ``error_message`` field on
  ``failed`` events.
* ``emit_verdict_event`` — one per Decide_Phase outcome. Carries the
  ``verdict`` label and ``verdict_reason``, plus a ``revision_rationale``
  field when the verdict is ``adjust``.
* ``emit_sampling_event`` — one per sampling call (whether the call was
  used, rejected, fell back, was unavailable, or was disabled). The second
  positional argument is ``iteration_index_or_purpose`` so callers can
  pass either an integer iteration index (during the loop) or a string /
  ``None`` for sampling that happens outside any single iteration (e.g.
  Final_Report fill-in).

Every entry carries a fresh ``timestamp`` set to ``datetime.now(UTC).isoformat()``
at emit time — the supplied phase ``started_at`` / ``ended_at`` are recorded
in their own dedicated fields and are not used as the entry timestamp.

The Mission package is loaded via ``sys.path``-on-``mcp/`` (the same trick
``run_mcp.py`` uses for the rest of the MCP modules), so ``audit_logger`` is
imported as ``from audit import audit_logger`` — matching every
``mcp/tools/*.py`` module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from audit import audit_logger

# ---------------------------------------------------------------------------
# Event-type tags
# ---------------------------------------------------------------------------

# Stable ``event_type`` strings so audit consumers can filter Mission events
# without parsing the rest of the entry. The values are part of the public
# audit contract — tests, dashboards, and the reconstruction test in
# ``tests/test_mission_audit.py`` match on these literals.
EVENT_TYPE_PHASE = "mission_phase_event"
EVENT_TYPE_VERDICT = "mission_verdict_event"
EVENT_TYPE_SAMPLING = "mission_sampling_event"
EVENT_TYPE_SCRIPT_CALL = "mission_script_call_event"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Centralised so every Mission emitter records ``timestamp`` in the same
    format. Matches the pattern used by ``_build_audit_entry`` and
    ``emit_startup_log`` in ``mcp/audit.py``.
    """
    return datetime.now(UTC).isoformat()


def _emit(entry: dict[str, Any]) -> None:
    """Serialise ``entry`` and route through the shared audit logger.

    Centralised so the three public emitters share one log path — and so a
    future swap to a structured handler only needs to change one site.
    """
    audit_logger.info(json.dumps(entry))


# ---------------------------------------------------------------------------
# Public emitters
# ---------------------------------------------------------------------------


def emit_phase_event(
    session_id: str,
    iteration_index: int,
    phase: Literal["propose", "execute", "observe", "evaluate", "decide"],
    status: Literal["succeeded", "failed"],
    started_at: str,
    ended_at: str,
    error_message: str | None = None,
) -> None:
    """Emit one ``mission_phase_event`` audit entry.

    Called by ``MissionEngine`` exactly once per phase from a try/finally
    block, so a failed phase still produces an entry with
    ``phase_status="failed"`` and the exception's ``error_message``.

    The ``started_at`` / ``ended_at`` arguments are recorded in their own
    fields — they describe the phase body, not the audit emit. The entry's
    own ``timestamp`` field is a fresh ``_now_iso()`` value set at call
    time so the audit log retains a faithful emission ordering.
    """
    entry: dict[str, Any] = {
        "event_type": EVENT_TYPE_PHASE,
        "mission_session_id": session_id,
        "iteration_index": iteration_index,
        "phase": phase,
        "phase_status": status,
        "phase_started_at": started_at,
        "phase_ended_at": ended_at,
        "timestamp": _now_iso(),
    }
    if error_message:
        # Match the 200-char truncation that ``_build_audit_entry`` applies
        # to its own ``error`` field so phase errors don't blow up the log
        # line on a long traceback summary.
        entry["error_message"] = error_message[:200]
    _emit(entry)


def emit_verdict_event(
    session_id: str,
    iteration_index: int,
    verdict: str,
    verdict_reason: str,
    revision_rationale: str | None = None,
) -> None:
    """Emit one ``mission_verdict_event`` audit entry.

    Called by ``MissionEngine`` once per Decide_Phase outcome. The
    ``revision_rationale`` is meaningful only on the ``adjust`` verdict
    (the rationale describes why the next iteration is being asked to
    revise the strategy), but this helper records it whenever the caller
    supplies a non-empty string — the engine decides when to populate
    it. This keeps the helper a pure formatter.
    """
    entry: dict[str, Any] = {
        "event_type": EVENT_TYPE_VERDICT,
        "mission_session_id": session_id,
        "iteration_index": iteration_index,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "timestamp": _now_iso(),
    }
    if revision_rationale:
        entry["revision_rationale"] = revision_rationale
    _emit(entry)


def emit_sampling_event(
    session_id: str,
    iteration_index_or_purpose: int | str | None,
    sampling_purpose: str,
    sampling_status: str,
    sampling_backend: str,
    sampling_model_id: str | None = None,
    model_output_bytes: int | None = None,
    validation_error: str | None = None,
) -> None:
    """Emit one ``mission_sampling_event`` audit entry.

    The second positional argument carries the iteration index when the
    sampling call happens inside the loop body, or a string / ``None`` for
    out-of-loop calls (e.g. final-report ``lessons`` fill-in). The helper
    routes the value to the right field:

    * ``int`` → ``iteration_index`` (matches the design's audit-fields
      table, which lists ``iteration_index`` as present on sampling
      events that occur during iterations).
    * non-empty ``str`` → ``sampling_context`` (an out-of-loop label).
    * ``None`` or empty string → neither field is recorded.

    ``sampling_model_id`` and ``model_output_bytes`` are recorded only when
    the sampler actually produced output (typically ``sampling_status="used"``).
    ``validation_error`` is recorded only when present (typically
    ``sampling_status="rejected"``). The conditional emission keeps the
    audit entry from carrying empty / null fields that downstream consumers
    would otherwise have to filter out.
    """
    entry: dict[str, Any] = {
        "event_type": EVENT_TYPE_SAMPLING,
        "mission_session_id": session_id,
        "sampling_purpose": sampling_purpose,
        "sampling_status": sampling_status,
        "sampling_backend": sampling_backend,
        "timestamp": _now_iso(),
    }

    # Route the iteration-or-purpose argument. ``int`` → numeric
    # ``iteration_index``; ``str`` (non-empty) → ``sampling_context``;
    # ``None`` and empty strings → omitted. ``bool`` is excluded
    # explicitly because it is a subclass of ``int`` in Python and would
    # otherwise be silently recorded as ``iteration_index=True``.
    if isinstance(iteration_index_or_purpose, int) and not isinstance(
        iteration_index_or_purpose, bool
    ):
        entry["iteration_index"] = iteration_index_or_purpose
    elif isinstance(iteration_index_or_purpose, str) and iteration_index_or_purpose:
        entry["sampling_context"] = iteration_index_or_purpose

    if sampling_model_id:
        entry["sampling_model_id"] = sampling_model_id
    if model_output_bytes is not None:
        entry["model_output_bytes"] = model_output_bytes
    if validation_error:
        entry["validation_error"] = validation_error[:200]

    _emit(entry)


def emit_script_call_event(
    session_id: str,
    iteration_index: int,
    tool_name: str,
    status: str,
    duration_ms: int,
    error_message: str | None = None,
) -> None:
    """Emit one ``mission_script_call_event`` audit entry.

    Called by the in-script tool wrapper after each invocation of an
    operator-allowlisted tool from inside a Mission script. The
    underlying tool call already produced its own ``@audit_logged``
    entry through the registered tool function; this helper layers a
    second, distinct audit row tagged ``via_script=True`` so consumers
    can tell at a glance which calls were driven from a script versus
    a direct ``tool_calls`` strategy.

    The ``status`` argument carries the call's terminal state (``ok``
    / ``failed`` / ``skipped_not_allowed``) and ``duration_ms`` mirrors
    the per-call timing the wrapper records on its own
    ``script_call_log`` entries. ``error_message`` is recorded only
    when supplied and is truncated to 200 characters to match the
    existing convention in :func:`emit_phase_event` and
    :func:`emit_sampling_event`.
    """
    entry: dict[str, Any] = {
        "event_type": EVENT_TYPE_SCRIPT_CALL,
        "via_script": True,
        "mission_session_id": session_id,
        "iteration_index": iteration_index,
        "tool_name": tool_name,
        "tool_status": status,
        "duration_ms": duration_ms,
        "timestamp": _now_iso(),
    }
    if error_message:
        entry["error_message"] = error_message[:200]
    _emit(entry)


__all__ = [
    "EVENT_TYPE_PHASE",
    "EVENT_TYPE_SAMPLING",
    "EVENT_TYPE_SCRIPT_CALL",
    "EVENT_TYPE_VERDICT",
    "emit_phase_event",
    "emit_sampling_event",
    "emit_script_call_event",
    "emit_verdict_event",
]
