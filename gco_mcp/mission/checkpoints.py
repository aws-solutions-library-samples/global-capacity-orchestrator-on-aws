"""Pure cadence resolver for the Mission progress-checkpoint schedule.

Two pure functions, both deliberately deterministic and side-effect-free:

* :func:`should_evaluate_now` answers "should the engine produce a real
  Verdict on this iteration, or emit a synthetic ``cadence_skip``?" given
  the session, the current iteration index, and the wall-clock value the
  caller has already measured. The function performs no I/O, reads no
  clocks, and consults no globals — every input it needs is on the call
  signature. That keeps the verdict-cascade unit-testable from the
  outside without monkeypatching ``datetime.now``.

* :func:`mark_checkpoint` records "we just produced a real Verdict at this
  wall-clock value" by writing ``session["last_checkpoint_at"]`` in place.
  The engine calls it after every Decide_Phase whose verdict was *not* a
  cadence-skip — that is, every Decide_Phase where the loop actually
  consulted the Criteria — so the next ``every_t_seconds`` check measures
  the right window.

Cadence semantics (per the validators in :mod:`mcp.mission.validation`):

* ``every_iteration`` — every iteration evaluates. Always returns True.
* ``every_n_iterations`` — evaluates on iterations whose 1-indexed
  position is divisible by ``n``. Concretely the function returns True
  iff ``(iteration_index + 1) % n == 0`` — so with ``n=3`` and a 0-indexed
  ``iteration_index``, the evaluating iterations are 2, 5, 8, … (the
  third, sixth, ninth iteration in the run). This matches Requirement
  6.3's wording ``iteration_index % n == n - 1``: the two formulations
  are algebraically identical and we use the ``+1`` form here because it
  matches the operator's mental model of "every Nth iteration".
* ``every_t_seconds`` — evaluates when ``now - last_checkpoint_at >= t``
  seconds. The first call returns True (no prior checkpoint) so the loop
  always produces a real Verdict on iteration 0 regardless of cadence.
  Subsequent calls compare ``now`` against the stored timestamp.
* ``on_event`` — evaluates when the most recent Iteration's Observation
  carries an event whose ``event_name`` matches the cadence's configured
  ``event_name``. A missing observation, an empty events list, or the
  absence of any prior iteration all return False.

Time-zone handling for ``every_t_seconds``: the stored
``last_checkpoint_at`` is parsed with :meth:`datetime.fromisoformat`. If
the parsed value has no tzinfo, we treat it as UTC — every other piece of
the Mission state writes ISO-8601 UTC (the audit emitters use
``datetime.now(UTC).isoformat()``), so a missing tzinfo is almost always
the result of a manual fixture write rather than a real on-disk session.
The caller is expected to pass a tz-aware ``now``; if both ends are
naive the comparison still works because Python forbids subtracting an
aware from a naive datetime, which would raise — and that's a bug we
want loud rather than silent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .types import SessionState


def should_evaluate_now(
    session: SessionState,
    iteration_index: int,
    now: datetime,
) -> bool:
    """Return True iff Decide_Phase should consult the Criteria this iteration.

    The four cadence kinds are dispatched on
    ``session["checkpoint_cadence"]["kind"]``. The validators in
    :mod:`mcp.mission.validation` guarantee the kind is one of the four
    supported values and that the kind-specific keys (``n``, ``t``,
    ``event_name``) are present and well-typed, so this function does no
    defensive validation of its own — it trusts the validated session and
    fails noisily on a malformed payload.
    """
    cadence = session["checkpoint_cadence"]
    kind = cadence["kind"]

    if kind == "every_iteration":
        # Every iteration evaluates — the loop's default behaviour and the
        # cheapest cadence to reason about.
        return True

    if kind == "every_n_iterations":
        # Validators guarantee ``n`` is a positive int. The +1 form matches
        # the operator's "every Nth iteration" mental model: with n=3 the
        # evaluating 0-indexed iterations are 2, 5, 8, … i.e. (idx+1) % n == 0.
        n = cadence["n"]
        return (iteration_index + 1) % n == 0

    if kind == "every_t_seconds":
        # First call has no prior checkpoint — evaluate so the loop always
        # produces a real Verdict on iteration 0 regardless of cadence.
        last_iso = session.get("last_checkpoint_at")
        if not last_iso:
            return True
        last = datetime.fromisoformat(last_iso)
        # Treat a missing tzinfo as UTC. Every Mission writer emits
        # tz-aware ISO-8601 UTC; this branch covers hand-written fixtures
        # and historical states that pre-date the convention.
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        t_seconds = cadence["t"]
        return now - last >= timedelta(seconds=t_seconds)

    if kind == "on_event":
        # Need at least one completed iteration to have an Observation.
        iterations = session.get("iterations") or []
        if not iterations:
            return False
        latest = iterations[-1]
        observation = latest.get("observation")
        if not observation:
            return False
        events = observation.get("events") or []
        target = cadence["event_name"]
        # Match on the ``event_name`` field of any event entry. We don't
        # constrain the rest of the event shape — the Observe_Phase
        # contract carries arbitrary event payloads — so any entry whose
        # ``event_name`` matches is sufficient to fire the cadence.
        return any(
            isinstance(event, dict) and event.get("event_name") == target for event in events
        )

    # Unreachable when validators have been applied. Surface the bad value
    # rather than silently returning False so a malformed session shows up
    # immediately in the caller's traceback.
    raise ValueError(f"unknown checkpoint cadence kind: {kind!r}")


def mark_checkpoint(session: SessionState, now: datetime) -> None:
    """Record that a real (non-cadence-skip) Verdict just fired at ``now``.

    Updates ``session["last_checkpoint_at"]`` in place to the ISO-8601
    serialisation of ``now``. Called by the engine after every
    Decide_Phase whose verdict was produced by consulting the Criteria,
    so the next ``every_t_seconds`` check measures from the most recent
    real checkpoint rather than from session start. Caller is responsible
    for skipping this call on cadence-skip iterations.
    """
    session["last_checkpoint_at"] = now.isoformat()


__all__ = ["mark_checkpoint", "should_evaluate_now"]
