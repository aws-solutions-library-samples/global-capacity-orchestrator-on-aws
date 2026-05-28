"""Five-phase iteration loop driver for the Mission goal-directed loop.

The :class:`MissionEngine` owns one ``run_iteration`` lifecycle per call: it
loads the persisted session, walks the iteration through propose → execute
→ observe → evaluate → decide, persists the resulting record, and writes a
Final_Report when the verdict is terminal. Every external dependency is
injected at construction time so unit tests can supply mocks for the tool
dispatcher, the sampling callable, and the script sandbox runner.

Why a class rather than a free function? Two reasons.

* Each phase needs the same handful of dependencies (the backend, the
  tool dispatcher, the cost-estimator map, the clock). Threading them
  through every method as positional arguments would be tedious and
  error-prone; the dataclass shape gives every phase one place to look
  for them.
* A test that exercises a single phase in isolation needs to construct
  a ``MissionEngine`` with stubbed dependencies and call the private
  method directly. Having the dependencies on the instance — rather
  than as module-level singletons — keeps the engine pure and free of
  process-global state.

Phase contract:

* Each ``_*_phase`` method is wrapped in a try/finally that emits
  exactly one ``audit.emit_phase_event`` regardless of whether the body
  succeeded or raised. The matching :class:`PhaseRecord` is appended to
  ``record["phases"]`` in the same finally block, so a failed phase
  still produces a structured record on the iteration.
* Any phase that raises propagates the exception out of
  ``run_iteration`` after the engine marks the session as ``failed``,
  appends the partial iteration, and persists. Subsequent calls to
  ``run_iteration`` on a ``failed`` session refuse with
  ``session_failed``.

Determinism: only the Decide_Phase consults a clock, and it does so by
calling ``self.now()`` exactly once per call so the value is observable
and pinnable from tests. The Propose_Phase's deterministic fallback uses
no clock and no random source. The Execute_Phase reads the clock for
the per-phase ``started_at`` / ``ended_at`` timestamps but its outputs
(the tool-call records) do not depend on those values.

The ``Context`` type is from FastMCP and brings a heavy dependency tree
(MCP transport, ``contextvars``, etc.) that unit tests do not need. We
type ``ctx`` as ``Any | None`` so the engine module imports cleanly in
isolation; the production wiring threads a real :class:`fastmcp.Context`
through the dispatcher and sampler callables, where its concrete type
matters. This is the same trade-off the existing ``mcp/tools/*.py``
modules make for tools that take an injected context.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from . import audit, decide, final_report
from .checkpoints import mark_checkpoint
from .predicate import evaluate_predicate
from .sampling import SamplingFallback, SamplingUsed
from .types import (
    TERMINAL_STATES,
    TERMINAL_VERDICTS,
    Criterion,
    CriterionResult,
    IterationRecord,
    Observation,
    PhaseRecord,
    SessionState,
    Strategy,
    ToolCallRecord,
    VerdictLabel,
    VerdictReason,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``MissionEngine.run_iteration`` -> ``diagrams/code_diagrams/mcp/mission/engine.MissionEngine_run_iteration.html``
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


__all__ = [
    "MissionEngine",
    "MissionEngineError",
]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class MissionEngineError(Exception):
    """Raised by the engine for stable, code-keyed lifecycle errors.

    The :attr:`code` attribute carries a short stable string (e.g.
    ``"session_not_found"``, ``"session_terminal"``, ``"session_paused"``,
    ``"session_failed"``) that the MCP tool wrappers and the CLI render
    as a structured tool error. The exception's string form falls back
    to ``code`` so logs always show something meaningful even when the
    caller does not pull the attribute out explicitly.
    """

    def __init__(self, code: str, *, message: str | None = None) -> None:
        self.code: str = code
        super().__init__(message if message is not None else code)


# ---------------------------------------------------------------------------
# Phase-name constants (typed)
# ---------------------------------------------------------------------------

# Centralised so the audit emitter and PhaseRecord constructor share one
# spelling for each phase. Matches the ``Literal`` shape declared on
# :class:`PhaseRecord` and on :func:`audit.emit_phase_event`.
_PROPOSE = "propose"
_EXECUTE = "execute"
_OBSERVE = "observe"
_EVALUATE = "evaluate"
_DECIDE = "decide"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _default_now() -> Callable[[], datetime]:
    """Return a clock callable that yields the current UTC datetime.

    Used as the ``default_factory`` for :attr:`MissionEngine.now`. Wrapping
    the lambda in a function keeps ``mypy --strict`` happy with the
    ``Callable[[], datetime]`` annotation while preserving the
    "constructed once per engine, called many times" semantics.
    """
    return lambda: datetime.now(UTC)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


# Type aliases for the injected callables. Loose on purpose: the precise
# shapes settle in later slices (sampling in slice 6, sandbox in slice 5).
ToolDispatcher = Callable[[str, dict[str, Any], Any], Awaitable[Any]]
SamplingCallable = Callable[..., Awaitable[Any]]
SandboxRunner = Callable[
    [str, Any, ToolDispatcher],
    Awaitable[tuple[dict[str, Any], list[ToolCallRecord]]],
]
CostEstimator = Callable[[dict[str, Any]], float]


@dataclass
class MissionEngine:
    """Driver for the Mission five-phase iteration loop.

    Construction takes every external dependency the engine needs:

    * ``backend`` — the persistence layer (filesystem, DynamoDB, …).
      Engine never reaches outside this protocol for state I/O.
    * ``tool_dispatcher`` — async callable that invokes one MCP tool.
      Signature ``(tool_name, args, ctx) -> result``. The engine routes
      every direct ``tool_calls`` invocation through this callable so
      tests can swap in a stub that returns canned results.
    * ``sampling_callable`` — optional async callable that produces an
      LLM-derived next Strategy when the prior Verdict was ``adjust``
      and the session has ``use_sampling=true``. Loose signature; slice
      6 finalises it. ``None`` (or any failure inside it) routes the
      engine to the deterministic fallback strategy.
    * ``sandbox_runner`` — optional async callable that runs a scripted
      Strategy in the Mission sandbox. Signature ``(script, ctx,
      tool_dispatcher) -> (observation_dict, script_call_log)``. ``None``
      means the engine refuses any scripted strategy with a clear
      error instead of silently executing operator-supplied code.
    * ``cost_estimators`` — per-tool cost-estimator functions keyed by
      tool name. Each estimator takes the tool's args dict and returns
      a USD float. Tools without an estimator contribute ``0.0`` to
      the session's accumulated cost; the validator already warned at
      session start about missing estimators on cost-incurring tools.
    * ``now`` — injectable clock. Defaults to a UTC clock; tests pin it
      so deterministic verdicts (the Decide_Phase consults the clock
      for budget caps and for the cadence resolver) are reproducible.
    """

    backend: Any
    tool_dispatcher: ToolDispatcher
    sampling_callable: SamplingCallable | None
    sandbox_runner: SandboxRunner | None
    cost_estimators: dict[str, CostEstimator]
    now: Callable[[], datetime] = field(default_factory=_default_now)
    # Optional async callable that drives the Final_Report's
    # ``lessons`` and ``recommended_followups`` overlay. Loose typing
    # mirrors :attr:`sampling_callable` so legacy tests that wire a
    # plain async stub keep working. Production wiring binds it to a
    # closure over :func:`mcp.mission.sampling.maybe_sample_final_lessons`
    # — see :meth:`_maybe_sample_final_lessons`. ``None`` (the default)
    # disables the overlay; the deterministic templates from
    # :func:`mcp.mission.final_report.build_deterministic_report` stand
    # on their own in that case.
    final_lessons_callable: SamplingCallable | None = None

    # ------------------------------------------------------------------ #
    # Public surface
    # ------------------------------------------------------------------ #

    async def run_iteration(
        self,
        session_id: str,
        ctx: Any | None = None,
    ) -> IterationRecord:
        """Run one full iteration for ``session_id`` and return its record.

        Lifecycle:

        1. Load the session; raise ``session_not_found`` when missing.
        2. Refuse a session in any terminal state (``failed`` →
           ``session_failed``; ``completed`` / ``terminated`` →
           ``session_terminal``) or in ``paused`` (``session_paused``).
        3. Transition ``pending → running`` on the very first iteration
           and stamp ``session["started_at"]``.
        4. Allocate a fresh :class:`IterationRecord` and run the five
           phases in order. Each phase emits exactly one
           ``audit.emit_phase_event`` regardless of outcome.
        5. On any phase exception: append the partial iteration to
           ``session["iterations"]``, mark the session ``failed``, save,
           and re-raise. The session JSON stays inspectable.
        6. On success: stamp the verdict on the iteration, append it,
           update the no-progress counter, save the session.
        7. On terminal verdict (``complete`` / ``terminate``): transition
           the session status, write the Final_Report, save again.
        8. Emit one ``audit.emit_verdict_event`` regardless of outcome.
        9. Return the iteration record.
        """
        session = self.backend.load_session(session_id)
        if session is None:
            raise MissionEngineError("session_not_found")

        # The terminal-state check distinguishes ``failed`` from the
        # other terminal states because callers (and the tool-error
        # table) treat them differently — a failed session needs manual
        # inspection via ``mission_history``, while a completed /
        # terminated session is simply done.
        status = session["status"]
        if status == "failed":
            raise MissionEngineError("session_failed")
        if status in TERMINAL_STATES:
            raise MissionEngineError("session_terminal")
        if status == "paused":
            raise MissionEngineError("session_paused")

        iteration_start = self.now()

        # First-iteration transition. ``started_at`` is the wall-clock
        # anchor for the wall-clock-budget computation in Decide_Phase
        # so we set it exactly once, on the pending → running edge.
        if session["status"] == "pending":
            session["status"] = "running"
            session["started_at"] = iteration_start.isoformat()

        iteration_index = len(session["iterations"])
        record = self._make_iteration_record(iteration_index, iteration_start)

        try:
            strategy = await self._propose_phase(session, ctx, record)
            executed_calls = await self._execute_phase(session, strategy, ctx, record)
            await self._observe_phase(session, strategy, executed_calls, record)
            await self._evaluate_phase(session, record)
            verdict, reason = await self._decide_phase(session, record)
        except Exception:
            # Persist a failure record so the session JSON remains a
            # complete history of everything the loop attempted. The
            # verdict stays at its placeholder value because no
            # Decide_Phase actually fired; consumers detect the failure
            # through ``session["status"] == "failed"`` and the failed
            # phase entry in ``record["phases"]``.
            record["ended_at"] = self.now().isoformat()
            session["iterations"].append(record)
            session["status"] = "failed"
            session["ended_at"] = record["ended_at"]
            with contextlib.suppress(Exception):
                # A save failure during a failure path must not shadow
                # the original phase exception — the operator's first
                # need is to see what actually went wrong, not what
                # went wrong while reporting what went wrong.
                self.backend.save_session(session)
            raise

        # Stamp the verdict on the iteration record before append; the
        # decide cascade inspects ``len(session["iterations"])`` (i.e.
        # iterations *before* the current one) so we deliberately
        # append after Decide_Phase rather than before.
        record["verdict"] = verdict
        record["verdict_reason"] = reason
        record["ended_at"] = self.now().isoformat()
        session["iterations"].append(record)

        self._update_session_post_iteration(session, record)
        self.backend.save_session(session)

        if verdict in TERMINAL_VERDICTS:
            await self._finalise_terminal_session(session, record, verdict, reason)
            self.backend.save_session(session)

        # One verdict event per iteration regardless of terminal vs
        # in-progress, so audit consumers see a uniform stream.
        audit.emit_verdict_event(
            session_id=session_id,
            iteration_index=iteration_index,
            verdict=verdict,
            verdict_reason=reason,
            revision_rationale=record.get("revision_rationale"),
        )

        return record

    # ------------------------------------------------------------------ #
    # Iteration record bootstrap
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_iteration_record(iteration_index: int, started_at: datetime) -> IterationRecord:
        """Build the empty :class:`IterationRecord` for a new iteration.

        Verdict and reason are placeholder values; the Decide_Phase
        overwrites them before the record is appended. ``ended_at``
        is set to the empty string and rewritten just before append
        so the persisted shape always carries an ISO-8601 timestamp.
        """
        record: IterationRecord = {
            "iteration_index": iteration_index,
            "started_at": started_at.isoformat(),
            "ended_at": "",
            "phases": [],
            "strategy": cast(Strategy, {}),
            "observation": cast(Observation, {}),
            "criteria_evaluation": [],
            "verdict": "continue",
            "verdict_reason": "in_progress",
            "checkpoint_evaluated": False,
        }
        return record

    # ------------------------------------------------------------------ #
    # Phase wrapper
    # ------------------------------------------------------------------ #

    async def _run_phase(
        self,
        session: SessionState,
        record: IterationRecord,
        phase_name: str,
        body: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Execute ``body`` with phase audit + record bookkeeping.

        Centralises the try/finally that every phase needs:

        * stamps ``started_at`` from the engine clock,
        * runs the body,
        * stamps ``ended_at`` from the engine clock again,
        * appends a :class:`PhaseRecord` to ``record["phases"]``,
        * emits exactly one ``audit.emit_phase_event``.

        On exception, the finally block records ``status="failed"``
        with the exception's name + message (truncated to 200 chars to
        match the audit module's existing convention) and re-raises so
        ``run_iteration`` can drive the failure path.
        """
        started_at = self.now().isoformat()
        status: str = "succeeded"
        error_message: str | None = None
        try:
            return await body()
        except Exception as exc:
            status = "failed"
            error_message = f"{type(exc).__name__}: {exc}"[:200]
            raise
        finally:
            ended_at = self.now().isoformat()
            phase_record: PhaseRecord = {
                "phase": cast(Any, phase_name),
                "status": cast(Any, status),
                "started_at": started_at,
                "ended_at": ended_at,
            }
            if error_message:
                phase_record["error_message"] = error_message
            record["phases"].append(phase_record)
            audit.emit_phase_event(
                session_id=session["session_id"],
                iteration_index=record["iteration_index"],
                phase=cast(Any, phase_name),
                status=cast(Any, status),
                started_at=started_at,
                ended_at=ended_at,
                error_message=error_message,
            )

    # ------------------------------------------------------------------ #
    # Phase 1 — propose
    # ------------------------------------------------------------------ #

    async def _propose_phase(
        self,
        session: SessionState,
        ctx: Any | None,
        record: IterationRecord,
    ) -> Strategy:
        """Build the Strategy for this iteration.

        Two paths:

        * **Sampling path** — when the prior verdict was ``adjust`` AND
          the session has ``use_sampling=true`` AND a sampling callable
          is wired, await the callable and adopt its return value as
          the Strategy. Any exception from the callable, or any return
          shape that does not look like a Strategy, falls through to
          the deterministic path. Slice 6 will replace this with a
          richer prompt-builder + validator.
        * **Deterministic path** — re-run the most recent successful
          tool call (using the same args) when one exists, otherwise
          invoke the first tool in the session's allowlist with empty
          args. Pure: no clock, no randomness, no external I/O. The
          resulting Strategy is always a single ``tool_calls`` entry.

        The chosen Strategy is stored on ``record["strategy"]`` and
        returned for the Execute_Phase to consume.
        """

        async def body() -> Strategy:
            strategy = await self._build_strategy(session, ctx, record)
            record["strategy"] = strategy
            return strategy

        return cast(Strategy, await self._run_phase(session, record, _PROPOSE, body))

    async def _build_strategy(
        self, session: SessionState, ctx: Any | None, record: IterationRecord
    ) -> Strategy:
        """Pick the Propose_Phase Strategy via the sampling-or-fallback rule."""
        if self._should_attempt_sampling(session):
            sampled = await self._try_sample_strategy(session, ctx, record)
            if sampled is not None:
                return sampled
        return self._deterministic_strategy(session)

    def _should_attempt_sampling(self, session: SessionState) -> bool:
        """True iff the prior verdict was ``adjust`` and sampling is wired."""
        if self.sampling_callable is None:
            return False
        if not session.get("use_sampling"):
            return False
        iterations = session.get("iterations") or []
        if not iterations:
            return False
        return iterations[-1].get("verdict") == "adjust"

    async def _try_sample_strategy(
        self, session: SessionState, ctx: Any | None, record: IterationRecord
    ) -> Strategy | None:
        """Call the sampling callable and adopt its return as a Strategy.

        Returns ``None`` on any failure (exception, non-dict return, dict
        missing both ``tool_calls`` and ``script``) so the caller can
        fall back to the deterministic strategy. Shape validation here
        is intentionally tight: the engine cannot run a ``script``
        strategy without a sandbox, and we never want a malformed sampler
        result to cascade into Execute_Phase as an opaque error.

        Three return shapes are recognised:

        * :class:`mcp.mission.sampling.SamplingUsed` — the production
          orchestration helper's accepted-output type. The Strategy is
          read from ``parsed["next_strategy"]``; the sampler's
          ``revision_rationale`` is stamped on the iteration ``record``
          so :func:`mcp.mission.audit.emit_verdict_event` surfaces it
          in the next iteration's audit trail.
        * :class:`mcp.mission.sampling.SamplingFallback` — the
          orchestration helper's rejection / fallback type. The engine
          treats this exactly like a missing return: ``None`` so the
          deterministic-fallback path runs. The fallback's own
          rationale stays on the audit event the helper already emitted;
          we deliberately do *not* override the engine's deterministic
          rationale-template here so the verdict path stays fully
          deterministic when sampling rejects.
        * Raw ``dict`` (the legacy / test pattern) — kept verbatim so
          existing engine tests that pass simple async lambdas returning
          ``{"tool_calls": [...]}`` continue to work without churn.
        """
        assert self.sampling_callable is not None  # narrowed by caller
        try:
            result = await self.sampling_callable(session=session, ctx=ctx)
        except Exception:
            return None

        # Phase 6.7 result types — the orchestration helper returns
        # either ``SamplingUsed`` (accept) or ``SamplingFallback``
        # (reject). The engine maps them onto its existing
        # "Strategy or fall back" surface.
        if isinstance(result, SamplingUsed):
            next_strategy = result.parsed.get("next_strategy")
            if not isinstance(next_strategy, dict):
                return None
            self._capture_sampled_rationale(record, result.parsed)
            return self._coerce_strategy_dict(next_strategy)

        if isinstance(result, SamplingFallback):
            # The fallback's own deterministic rationale is already on
            # the emitted audit event. The engine routes through its
            # own deterministic-fallback path so the verdict path stays
            # fully deterministic — returning ``None`` is the signal.
            return None

        # Legacy raw-dict return — preserved verbatim so older tests
        # and callers that wire a simple ``async def: return {...}``
        # stub continue to work unchanged.
        if not isinstance(result, dict):
            return None
        return self._coerce_strategy_dict(result)

    def _coerce_strategy_dict(self, candidate: dict[str, Any]) -> Strategy | None:
        """Adopt ``candidate`` as a :class:`Strategy` if it is well-shaped.

        Centralises the structural check (exactly one of ``tool_calls``
        or ``script`` populated and well-typed) so both the
        :class:`SamplingUsed` path and the legacy raw-dict path land
        through one validator. Returns ``None`` for any malformed
        shape; callers fall back to the deterministic strategy.
        """
        if "tool_calls" in candidate:
            tool_calls = candidate["tool_calls"]
            if isinstance(tool_calls, list) and tool_calls:
                return cast(Strategy, dict(candidate))
            return None
        if "script" in candidate:
            script = candidate["script"]
            if isinstance(script, str) and script:
                # The sandbox runner is the only thing that can
                # safely execute a script. If it isn't wired, the
                # sampled script is unusable — fall back.
                if self.sandbox_runner is None:
                    return None
                return cast(Strategy, dict(candidate))
            return None
        return None

    @staticmethod
    def _capture_sampled_rationale(record: IterationRecord, parsed_payload: dict[str, Any]) -> None:
        """Stamp the sampler's ``revision_rationale`` on ``record`` if present.

        The advisory model's rationale lives at
        ``parsed_payload["revision_rationale"]`` per the
        Strategy_Revision schema. Recording it on the iteration record
        means :func:`mcp.mission.audit.emit_verdict_event` (which the
        engine calls at the end of ``run_iteration`` with
        ``record.get("revision_rationale")``) emits the model-derived
        text instead of the deterministic template that the
        Decide_Phase synthesises for ``adjust`` verdicts. The engine
        only ever calls this from the sampling-success path, so a
        rejection / fallback never overrides the deterministic
        template the Decide_Phase set.
        """
        rationale = parsed_payload.get("revision_rationale")
        if isinstance(rationale, str) and rationale:
            record["revision_rationale"] = rationale

    def _deterministic_strategy(self, session: SessionState) -> Strategy:
        """Build the fallback Strategy when sampling is off or unusable.

        Re-runs the most recent successful tool call (same name, same
        args) so the loop makes a forward attempt without random
        flailing. When no successful call exists yet, the first tool
        in the allowlist runs with empty args — the simplest possible
        forward step. A future slice can replace "same args" with a
        proper widening rule once the design has the per-tool
        widening table.
        """
        prior_call = self._find_most_recent_successful_call(session)
        if prior_call is not None:
            tool_name, args = prior_call
            # Same args verbatim — slice 6 owns the "widening" rule that
            # tweaks numeric params upward / downward to break out of
            # local minima. Until then, repetition is the deterministic
            # default.
            return cast(
                Strategy,
                {
                    "tool_calls": [{"tool_name": tool_name, "args": dict(args)}],
                    "rationale": (f"deterministic fallback: re-run {tool_name} with prior args"),
                },
            )
        allowlist = session.get("tool_allowlist") or []
        if not allowlist:
            # Safety net: a session whose allowlist somehow ended up
            # empty cannot run anything. Surface the misconfiguration
            # rather than silently invent a Strategy.
            raise MissionEngineError("propose_no_tool_available")
        return cast(
            Strategy,
            {
                "tool_calls": [{"tool_name": allowlist[0], "args": {}}],
                "rationale": (
                    "deterministic fallback: invoking first allowlisted "
                    "tool with empty args (no prior successful call)"
                ),
            },
        )

    @staticmethod
    def _find_most_recent_successful_call(
        session: SessionState,
    ) -> tuple[str, dict[str, Any]] | None:
        """Walk the iteration history backwards for a successful tool call.

        Returns ``(tool_name, args)`` for the most recent call whose
        :class:`ToolCallRecord` has ``status="ok"``. Looks at both the
        recorded executed-call list (for tool_calls strategies — kept
        on the iteration's Strategy under ``tool_calls`` after execute)
        and the script call log (for scripted strategies). Skips
        iterations whose strategy was a script with no successful
        embedded tool call.

        Returns ``None`` when no prior successful call exists across
        the entire history.
        """
        for iteration in reversed(session.get("iterations") or []):
            # Scripted strategies record their inner calls on
            # ``script_call_log``; direct tool_calls strategies don't
            # have that key.
            for source_key in ("script_call_log",):
                log = iteration.get(source_key)
                if not log:
                    continue
                for call in reversed(log):
                    if (
                        isinstance(call, dict)
                        and call.get("status") == "ok"
                        and isinstance(call.get("tool_name"), str)
                        and isinstance(call.get("args"), dict)
                    ):
                        return call["tool_name"], dict(call["args"])
            # Direct tool_calls strategies write the executed records
            # back onto the Strategy under ``tool_calls`` (each entry
            # carrying the same status / args fields the script log
            # would carry). This keeps a single lookup path here.
            strategy = iteration.get("strategy") or {}
            tool_calls = strategy.get("tool_calls") or []
            for tc in reversed(tool_calls):
                if (
                    isinstance(tc, dict)
                    and tc.get("status") == "ok"
                    and isinstance(tc.get("tool_name"), str)
                    and isinstance(tc.get("args"), dict)
                ):
                    return tc["tool_name"], dict(tc["args"])
        return None

    # ------------------------------------------------------------------ #
    # Phase 2 — execute
    # ------------------------------------------------------------------ #

    async def _execute_phase(
        self,
        session: SessionState,
        strategy: Strategy,
        ctx: Any | None,
        record: IterationRecord,
    ) -> list[ToolCallRecord]:
        """Run the Strategy and return the list of executed tool calls.

        Two modes:

        * **tool_calls** — iterate the strategy's ``tool_calls`` in
          order. For each: gate by the session's allowlist, dispatch
          via :attr:`tool_dispatcher`, and record the outcome
          (``ok`` / ``failed`` / ``skipped_not_allowed``). One failed
          call does not abort the iteration — the next call still
          runs, the failure lands as one entry, and Observe_Phase
          surfaces it under ``errors``.
        * **script** — hand the script to :attr:`sandbox_runner` along
          with ``ctx`` and the engine's own ``tool_dispatcher`` so the
          sandbox can safely invoke allowlisted tools as native
          callables. The runner returns ``(observation_dict,
          script_call_log)``. The observation is stashed on the record
          for Observe_Phase to use directly; the script_call_log is
          stored on ``record["script_call_log"]``.

        For both modes, every successful call (or each successful
        embedded call in script mode) contributes its cost-estimator
        output to the session's ``accumulated_cost_usd``.
        """

        async def body() -> list[ToolCallRecord]:
            if "script" in strategy:
                return await self._execute_script(session, strategy, ctx, record)
            return await self._execute_tool_calls(session, strategy, ctx, record)

        return cast(
            list[ToolCallRecord],
            await self._run_phase(session, record, _EXECUTE, body),
        )

    async def _execute_tool_calls(
        self,
        session: SessionState,
        strategy: Strategy,
        ctx: Any | None,
        record: IterationRecord,
    ) -> list[ToolCallRecord]:
        """Run the Strategy's ``tool_calls`` list with allowlist gating."""
        allowlist = set(session.get("tool_allowlist") or [])
        executed: list[ToolCallRecord] = []
        for entry in strategy.get("tool_calls", []) or []:
            tool_name = entry.get("tool_name") if isinstance(entry, dict) else None
            args = entry.get("args") if isinstance(entry, dict) else {}
            if not isinstance(tool_name, str) or not tool_name:
                # A malformed tool_calls entry is the operator's bug,
                # but failing the entire iteration over one bad entry
                # is harsher than the loop semantics demand. Record
                # it as a failed call and move on.
                executed.append(
                    {
                        "tool_name": str(tool_name) if tool_name else "<unknown>",
                        "args": args if isinstance(args, dict) else {},
                        "status": "failed",
                        "result_summary": None,
                        "duration_ms": 0,
                        "error_message": "tool_name_missing_or_invalid",
                    }
                )
                continue
            if not isinstance(args, dict):
                args = {}
            if tool_name not in allowlist:
                executed.append(
                    {
                        "tool_name": tool_name,
                        "args": args,
                        "status": "skipped_not_allowed",
                        "result_summary": None,
                        "duration_ms": 0,
                    }
                )
                continue
            executed.append(await self._dispatch_one_call(session, tool_name, args, ctx))

        # Persist the executed records back onto the Strategy so the
        # propose-fallback's "most recent successful call" lookup has
        # a single source of truth on every persisted iteration.
        record["strategy"]["tool_calls"] = [dict(call) for call in executed]
        return executed

    async def _dispatch_one_call(
        self,
        session: SessionState,
        tool_name: str,
        args: dict[str, Any],
        ctx: Any | None,
    ) -> ToolCallRecord:
        """Invoke one allowlisted tool through the dispatcher."""
        started = self.now()
        try:
            result = await self.tool_dispatcher(tool_name, args, ctx)
        except Exception as exc:
            duration_ms = self._elapsed_ms(started)
            return {
                "tool_name": tool_name,
                "args": args,
                "status": "failed",
                "result_summary": None,
                "duration_ms": duration_ms,
                "error_message": f"{type(exc).__name__}: {exc}"[:200],
            }
        duration_ms = self._elapsed_ms(started)
        cost = self._estimate_cost(tool_name, args)
        # ``accumulated_cost_usd`` is a float and the validator caps
        # are USD; we keep the running total here so the Decide_Phase
        # can compare it against ``budget["max_cost_usd"]`` without
        # re-walking the iteration history.
        session["accumulated_cost_usd"] = (session.get("accumulated_cost_usd", 0.0) or 0.0) + cost
        record: ToolCallRecord = {
            "tool_name": tool_name,
            "args": args,
            "status": "ok",
            "result_summary": result,
            "duration_ms": duration_ms,
        }
        if cost:
            record["cost_usd"] = cost
        return record

    async def _execute_script(
        self,
        session: SessionState,
        strategy: Strategy,
        ctx: Any | None,
        record: IterationRecord,
    ) -> list[ToolCallRecord]:
        """Run a scripted strategy through the wired sandbox runner.

        Three failure modes get translated into stable engine error
        codes so the MCP tool wrappers and the CLI render them as
        structured rejections rather than opaque tracebacks:

        * ``sandbox_runner is None`` — the engine was constructed
          without a sandbox. The session-start validator should have
          rejected any script-bearing Strategy already (scripts go
          through ``validate_script_ast`` before they reach here),
          but a sampled-then-injected script could still arrive at
          this method. Treat it as a validation failure with the
          equivalent of ``script_rejected``.
        * :class:`ScriptRejected` from inside the runner — the runner
          re-validated the script just before execution and the AST
          gate fired. Re-raise as ``script_rejected``.
        * :class:`SandboxTerminated` from inside the runner — Monty
          killed the script for exceeding a duration / memory cap.
          The cap is a true budget cap, not a code-quality failure,
          so the engine *swallows* the exception, builds a partial
          Observation from whatever the script collected before being
          killed, stashes the partial ``script_call_log`` and a
          ``sandbox_terminated_reason`` sentinel on the iteration
          record, and returns a list of partial calls. The Decide_Phase
          reads the sentinel and emits ``("terminate",
          "max_wall_clock")`` so the verdict surfaces on the
          budget-cap path rather than via a phase failure.

        The sandbox module is imported lazily inside this method so
        the engine module stays importable on hosts where the
        underlying ``pydantic_monty`` dependency is absent (CLI-only
        environments, dry-run validators, etc.). When the lazy
        import fails, the structured-exception translation is
        skipped and the original exception bubbles up to the
        ``run_iteration`` failure path — the engine still records a
        failed Execute_Phase, which is the right behaviour even
        without per-class translation.
        """
        if self.sandbox_runner is None:
            raise MissionEngineError("script_rejected")
        script = strategy["script"]
        try:
            observation_dict, script_call_log = await self.sandbox_runner(
                script, ctx, self.tool_dispatcher
            )
        except Exception as exc:
            # Late-resolved class lookup: importing the sandbox
            # module at top of file would pull in
            # ``pydantic_monty`` on import, which the engine
            # explicitly does not require (an operator can run
            # ``mission_validate`` against a stored session JSON
            # without a working sandbox). Importing here means the
            # translation is best-effort but the engine module
            # itself stays loadable everywhere.
            try:
                from .sandbox import (
                    SandboxTerminated,
                    ScriptRejected,
                )
            except Exception:
                raise
            if isinstance(exc, ScriptRejected):
                raise MissionEngineError("script_rejected") from exc
            if isinstance(exc, SandboxTerminated):
                # The sandbox cap is a budget cap, not a phase
                # failure: the script ran out of wall clock (or
                # memory, or hit a runtime / typing / syntax error
                # mid-run) under operator-supplied limits. Capture
                # whatever it collected before being killed and
                # route the verdict through the budget-cap path.
                #
                # The sentinel on the iteration record is what the
                # cascade in ``decide_verdict`` reads to short-
                # circuit to ``("terminate", "max_wall_clock")``
                # before any other branch is consulted; without it
                # the cascade would fall through to the default
                # ``("continue", "in_progress")`` because no other
                # cap was breached.
                record["script_call_log"] = cast(
                    "list[ToolCallRecord]", list(exc.partial_script_call_log)
                )
                # Fold per-call costs from the partial log onto the
                # engine's loaded session so a partial script that
                # made cost-incurring calls before being killed
                # contributes those costs to the accumulator. The
                # cost cap path in Decide_Phase reads the same
                # ``accumulated_cost_usd`` value the direct-dispatch
                # path would have produced.
                self._accumulate_script_costs(session, exc.partial_script_call_log)
                # Build a minimal Observation from the partial
                # logs so Evaluate_Phase has the same shape it
                # would have on a successful sandbox run. Missing
                # keys (``metrics``, ``events``, etc.) get default
                # empties; Observe_Phase fills in any timestamps
                # the partial doesn't carry.
                partial_observation: dict[str, Any] = {
                    "tool_results": [
                        call.get("result_summary") for call in exc.partial_script_call_log
                    ],
                    "metrics": {},
                    "events": list(exc.partial_events),
                }
                if exc.partial_observations:
                    partial_observation["metrics"]["observations"] = {
                        entry["key"]: entry["value"]
                        for entry in exc.partial_observations
                        if isinstance(entry, dict) and "key" in entry
                    }
                record["observation"] = cast(Observation, partial_observation)
                record["sandbox_terminated_reason"] = "max_wall_clock"
                return cast("list[ToolCallRecord]", list(exc.partial_script_call_log))
            raise
        # The sandbox already produced a normalized Observation dict,
        # so we cache it on the record for Observe_Phase to pick up
        # directly. This is the only path where Observe_Phase sees a
        # pre-built Observation.
        record["script_call_log"] = cast("list[ToolCallRecord]", list(script_call_log))
        record["observation"] = cast(Observation, dict(observation_dict))
        # Fold per-call costs from the successful run onto the
        # session so the Decide_Phase's ``_cost_exceeded`` check sees
        # the same running total it would on the direct-dispatch
        # path. The wrapper records ``cost_usd`` on each call record;
        # the engine is the single place where those per-call costs
        # land on the live session record.
        self._accumulate_script_costs(session, script_call_log)
        return list(script_call_log)

    def _accumulate_script_costs(
        self,
        session: SessionState,
        call_log: list[dict[str, Any]] | list[ToolCallRecord],
    ) -> None:
        """Fold per-call ``cost_usd`` from a script call log onto the session.

        The in-script tool wrapper records ``cost_usd`` on each
        successful :class:`ToolCallRecord` (or omits it for tools
        without an estimator). Walking the log here, after the
        sandbox returns, lets the engine treat the sandbox as a
        construction-time-immutable component that does not have to
        hold a live reference to the session record. Both the
        successful-return path and the
        :class:`SandboxTerminated` partial-log path call this so a
        script that made cost-incurring calls before being killed
        still contributes those costs to the running total.
        """
        for call in call_log:
            cost = call.get("cost_usd") if isinstance(call, dict) else None
            if cost is None:
                continue
            if not isinstance(cost, (int, float)) or isinstance(cost, bool):
                continue
            session["accumulated_cost_usd"] = (
                session.get("accumulated_cost_usd", 0.0) or 0.0
            ) + float(cost)

    def _estimate_cost(self, tool_name: str, args: dict[str, Any]) -> float:
        """Return the cost estimate for one successful call, or 0.0.

        Tools without a registered estimator contribute 0.0; the
        validator already warned about missing estimators on
        cost-incurring tools at session start, so the engine's job
        here is simply to honour whatever map the caller supplied.
        Any exception raised by the estimator is treated as a 0.0
        cost — a flaky estimator must not be allowed to fail an
        Execute_Phase.
        """
        estimator = self.cost_estimators.get(tool_name)
        if estimator is None:
            return 0.0
        try:
            value = estimator(args)
        except Exception:
            return 0.0
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return 0.0
        return float(value)

    def _elapsed_ms(self, started: datetime) -> int:
        """Return integer milliseconds elapsed since ``started``."""
        delta = self.now() - started
        return max(int(delta.total_seconds() * 1000), 0)

    # ------------------------------------------------------------------ #
    # Phase 3 — observe
    # ------------------------------------------------------------------ #

    async def _observe_phase(
        self,
        session: SessionState,
        strategy: Strategy,
        executed_calls: list[ToolCallRecord],
        record: IterationRecord,
    ) -> None:
        """Normalise tool-call outputs into an :class:`Observation`.

        Two paths:

        * **Script strategy** — Execute_Phase already stashed the
          sandbox's Observation on ``record["observation"]``. Observe
          fills in any missing required keys (``tool_results``,
          ``metrics``, ``events``, ``phase_started_at`` /
          ``phase_ended_at``) so downstream Evaluate_Phase consumers
          can rely on the shape.
        * **Tool-calls strategy** — build the Observation from the
          executed-call records: ``tool_results`` is the list of
          ``result_summary`` values (one per call, including failed
          ones for stable indexing); ``metrics`` and ``events`` are
          merged from any call result that carries those keys at the
          top level; ``errors`` is appended for failed or skipped
          calls. This is intentionally permissive — a Strategy that
          doesn't produce metrics or events leaves those slots empty
          rather than raising.
        """

        async def body() -> None:
            phase_started = self.now()
            if "script" in strategy:
                # The sandbox already produced the Observation. Fill
                # in any timestamp slots it didn't populate so the
                # shape is uniform for evaluators.
                obs = cast(dict[str, Any], record.get("observation") or {})
                obs.setdefault("tool_results", [])
                obs.setdefault("metrics", {})
                obs.setdefault("events", [])
                obs.setdefault("phase_started_at", phase_started.isoformat())
                obs.setdefault("phase_ended_at", self.now().isoformat())
                record["observation"] = cast(Observation, obs)
                return
            record["observation"] = self._build_observation(executed_calls, phase_started)

        await self._run_phase(session, record, _OBSERVE, body)

    def _build_observation(
        self,
        executed_calls: list[ToolCallRecord],
        phase_started: datetime,
    ) -> Observation:
        """Merge a list of :class:`ToolCallRecord` into an :class:`Observation`."""
        tool_results: list[Any] = []
        metrics: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for call in executed_calls:
            tool_results.append(call.get("result_summary"))
            if call.get("status") == "ok":
                result = call.get("result_summary")
                # Permissive merge: when a tool's result happens to
                # include a top-level ``metrics`` dict or ``events``
                # list, lift them into the Observation. Anything else
                # stays only in ``tool_results``.
                if isinstance(result, dict):
                    result_metrics = result.get("metrics")
                    if isinstance(result_metrics, dict):
                        metrics.update(result_metrics)
                    result_events = result.get("events")
                    if isinstance(result_events, list):
                        for event in result_events:
                            if isinstance(event, dict):
                                events.append(event)
            else:
                # ``failed`` and ``skipped_not_allowed`` both surface as
                # errors; the heuristic in decide.py uses "errors that
                # didn't appear in the prior Observation" to drive the
                # adjust verdict, so a stable shape per error matters.
                errors.append(
                    {
                        "tool_name": call.get("tool_name"),
                        "status": call.get("status"),
                        "error_message": call.get("error_message"),
                    }
                )

        observation: Observation = {
            "tool_results": tool_results,
            "metrics": metrics,
            "events": events,
            "phase_started_at": phase_started.isoformat(),
            "phase_ended_at": self.now().isoformat(),
        }
        if errors:
            observation["errors"] = errors
        return observation

    # ------------------------------------------------------------------ #
    # Phase 4 — evaluate
    # ------------------------------------------------------------------ #

    async def _evaluate_phase(self, session: SessionState, record: IterationRecord) -> None:
        """Walk the session's Criteria and produce :class:`CriterionResult` rows.

        The kinds dispatch to per-kind helpers:

        * ``metric_threshold`` — dot-path lookup on the Observation,
          numeric comparison via the declared operator.
        * ``event`` — scan the Observation's ``events`` list for an
          entry whose ``event_name`` matches the criterion's target.
        * ``predicate`` — evaluate the cached parsed AST against the
          Observation. A raised exception lands as ``inconclusive`` so
          a malformed predicate cannot crash the loop.

        Order in the output list matches the declared order of
        ``session["criteria"]`` so iteration audit consumers can pair
        results with criteria positionally.
        """

        async def body() -> None:
            observation = cast(dict[str, Any], record.get("observation") or {})
            results: list[CriterionResult] = []
            for criterion in session.get("criteria") or []:
                results.append(self._evaluate_one_criterion(criterion, observation))
            record["criteria_evaluation"] = results

        await self._run_phase(session, record, _EVALUATE, body)

    def _evaluate_one_criterion(
        self, criterion: Criterion, observation: dict[str, Any]
    ) -> CriterionResult:
        """Dispatch to the right evaluator and produce a result row."""
        criterion_id = criterion["criterion_id"]
        kind = criterion["kind"]
        evaluated_at = self.now().isoformat()

        if kind == "metric_threshold":
            status, evidence = self._evaluate_metric_threshold(criterion, observation)
        elif kind == "event":
            status, evidence = self._evaluate_event(criterion, observation)
        elif kind == "predicate":
            status, evidence = self._evaluate_predicate(criterion, observation)
        else:
            # Unreachable when the validator has run — but if a
            # malformed session somehow lands here, surface the bad
            # kind as inconclusive rather than raising and tearing
            # down the entire iteration.
            status = "inconclusive"
            evidence = f"unknown_criterion_kind:{kind!r}"

        return {
            "criterion_id": criterion_id,
            "status": cast(Any, status),
            "evidence": evidence,
            "evaluated_at": evaluated_at,
        }

    @staticmethod
    def _evaluate_metric_threshold(
        criterion: Criterion, observation: dict[str, Any]
    ) -> tuple[str, Any]:
        """Look up the metric by dot-path and compare to ``target``."""
        path = criterion.get("metric") or ""
        op = criterion.get("op")
        target = criterion.get("target")
        value: Any = observation
        for segment in path.split("."):
            if isinstance(value, dict) and segment in value:
                value = value[segment]
            else:
                return "inconclusive", f"metric_path_missing:{path!r}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "inconclusive", value
        try:
            met = _compare_numbers(value, cast(str, op), cast(float, target))
        except ValueError:
            return "inconclusive", value
        return ("met" if met else "unmet"), value

    @staticmethod
    def _evaluate_event(criterion: Criterion, observation: dict[str, Any]) -> tuple[str, Any]:
        """Scan ``observation['events']`` for the named event."""
        if "events" not in observation:
            return "inconclusive", "events_field_missing"
        events = observation.get("events")
        if not isinstance(events, list):
            return "inconclusive", "events_field_not_a_list"
        target = criterion.get("event_name")
        for event in events:
            if isinstance(event, dict) and event.get("event_name") == target:
                return "met", event
        return "unmet", None

    @staticmethod
    def _evaluate_predicate(criterion: Criterion, observation: dict[str, Any]) -> tuple[str, Any]:
        """Run the cached parsed AST against the Observation."""
        parsed = criterion.get("_parsed_ast")
        if parsed is None:
            # The validator should always cache the AST; if it didn't,
            # treat the criterion as inconclusive rather than skipping.
            return "inconclusive", "predicate_ast_not_cached"
        try:
            value = evaluate_predicate(parsed, observation)
        except Exception as exc:
            return "inconclusive", f"{type(exc).__name__}: {exc}"
        return ("met" if value else "unmet"), value

    # ------------------------------------------------------------------ #
    # Phase 5 — decide
    # ------------------------------------------------------------------ #

    async def _decide_phase(
        self, session: SessionState, record: IterationRecord
    ) -> tuple[VerdictLabel, VerdictReason]:
        """Run the deterministic verdict cascade and stamp the record."""

        async def body() -> tuple[VerdictLabel, VerdictReason]:
            now_value = self.now()
            verdict, reason = decide.decide_verdict(session, record, now_value)
            checkpoint_evaluated = reason != "cadence_skip"
            record["checkpoint_evaluated"] = checkpoint_evaluated
            if verdict == "adjust":
                record["revision_rationale"] = decide.build_revision_rationale_template(
                    session, record
                )
            if checkpoint_evaluated:
                # ``last_checkpoint_at`` anchors the every_t_seconds
                # cadence; only real (non-skip) verdicts advance it.
                mark_checkpoint(session, now_value)
            return verdict, reason

        return cast(
            tuple[VerdictLabel, VerdictReason],
            await self._run_phase(session, record, _DECIDE, body),
        )

    # ------------------------------------------------------------------ #
    # Post-iteration housekeeping
    # ------------------------------------------------------------------ #

    def _update_session_post_iteration(
        self, session: SessionState, record: IterationRecord
    ) -> None:
        """Advance or reset the no-progress counter.

        Counter semantics (matching the Decide_Phase's stagnation cap):

        * Synthetic ``cadence_skip`` iterations leave the counter
          alone — a session whose cadence is ``every_n_iterations``
          must not be able to reach ``stagnation_threshold`` purely
          because most iterations skip the criteria check.
        * On evaluated iterations, compute the per-criterion
          improvement against the immediately prior evaluated
          iteration. A criterion improved iff its prior status was
          ``unmet`` or ``inconclusive`` AND its current status is
          ``met``. Any improvement resets the counter to 0; otherwise
          the counter increments by 1.

        ``record`` has already been appended to
        ``session["iterations"]`` by the caller, so the prior
        iteration is at index ``-2``.
        """
        if not record.get("checkpoint_evaluated"):
            return

        prior_eval = self._previous_evaluated_iteration(session)
        if prior_eval is None:
            # No prior evaluated iteration: the loop has nothing to
            # measure improvement against. Treat as no-improvement
            # rather than a forced reset, so the stagnation counter
            # tracks "how long since we made measurable progress"
            # uniformly across the run.
            session["no_progress_counter"] = (session.get("no_progress_counter", 0) or 0) + 1
            return

        if self._criteria_improved(prior_eval, record["criteria_evaluation"]):
            session["no_progress_counter"] = 0
        else:
            session["no_progress_counter"] = (session.get("no_progress_counter", 0) or 0) + 1

    @staticmethod
    def _previous_evaluated_iteration(
        session: SessionState,
    ) -> list[CriterionResult] | None:
        """Return the criteria evaluation of the most recent evaluated iteration.

        "Evaluated" here means ``checkpoint_evaluated=True``. Skipping
        cadence-skip iterations is what makes the no-progress counter
        immune to the cadence configuration: it only ever measures
        movement between *real* checkpoints. The current iteration is
        already appended to ``session["iterations"]`` by the caller,
        so we look strictly before it.
        """
        iterations = session.get("iterations") or []
        # Skip the just-appended current iteration (last index).
        for prior in reversed(iterations[:-1]):
            if prior.get("checkpoint_evaluated"):
                return list(prior.get("criteria_evaluation") or [])
        return None

    @staticmethod
    def _criteria_improved(
        prior: list[CriterionResult],
        current: list[CriterionResult],
    ) -> bool:
        """Return True iff any criterion went from not-met to met."""
        prior_status = {
            result["criterion_id"]: result["status"]
            for result in prior
            if isinstance(result, dict) and "criterion_id" in result
        }
        for result in current:
            if not isinstance(result, dict):
                continue
            current_status = result.get("status")
            if current_status != "met":
                continue
            prior_value = prior_status.get(result.get("criterion_id"))
            if prior_value in ("unmet", "inconclusive", None):
                # ``None`` covers a criterion that did not appear in
                # the prior evaluation — treating it as "not met
                # before" is consistent with first-time-met being an
                # improvement.
                return True
        return False

    # ------------------------------------------------------------------ #
    # Terminal-verdict finalisation
    # ------------------------------------------------------------------ #

    async def _finalise_terminal_session(
        self,
        session: SessionState,
        record: IterationRecord,
        verdict: VerdictLabel,
        reason: VerdictReason,
    ) -> None:
        """Transition the session to its terminal status and write the report.

        Called by ``run_iteration`` only when the verdict is in
        :data:`TERMINAL_VERDICTS`. The status mapping is fixed:
        ``complete`` → ``completed``, ``terminate`` → ``terminated``.
        ``ended_at`` is anchored on the iteration's own ``ended_at``
        so the session's lifecycle window matches the last persisted
        iteration's window without an extra clock read.

        When :attr:`final_lessons_callable` is wired, the engine awaits
        it once to fetch a ``{"lessons": ..., "recommended_followups":
        ...}`` overlay and synthesises a tiny synchronous sampler
        closure that returns the pre-fetched overlay; the closure is
        then handed to :func:`mcp.mission.final_report.write_final_report`
        which keeps its existing sync-callable contract. This pre-fetch
        bridge is needed because the production helper
        (:func:`mcp.mission.sampling.maybe_sample_final_lessons`) is
        async while ``write_final_report`` is sync.
        """
        if verdict == "complete":
            session["status"] = "completed"
        else:  # verdict == "terminate"
            session["status"] = "terminated"
        session["ended_at"] = record["ended_at"]
        session["final_verdict"] = verdict
        # Optional sampling overlay for the lessons / followups fields.
        # Pre-fetch so the (sync) report writer never has to await.
        overlay = await self._maybe_sample_final_lessons(session, verdict, reason)
        sampler: Callable[..., dict[str, Any] | None] | None
        if overlay is not None:

            def _pre_fetched_sampler(
                _session: SessionState,
                _verdict: VerdictLabel,
                _reason: VerdictReason,
            ) -> dict[str, Any] | None:
                return overlay

            sampler = _pre_fetched_sampler
        else:
            sampler = None
        # The Final_Report is the durable exit artifact. We write it
        # via the report helper so the persistence path (filesystem
        # sibling vs. embedded-on-session for non-filesystem backends)
        # is owned by one module.
        final_report.write_final_report(self.backend, session, verdict, reason, sampler=sampler)

    async def _maybe_sample_final_lessons(
        self,
        session: SessionState,
        verdict: VerdictLabel,
        reason: VerdictReason,
    ) -> dict[str, Any] | None:
        """Fetch the optional Final_Report ``lessons`` / ``followups`` overlay.

        Calls :attr:`final_lessons_callable` once (when wired and when
        the session opted into sampling) and adapts the return value
        into the ``{"lessons": str, "recommended_followups": list[str]}``
        shape that
        :func:`mcp.mission.final_report.write_final_report` expects.

        Three return shapes are recognised:

        * :class:`mcp.mission.sampling.SamplingUsed` — production path.
          ``parsed["lessons"]`` is a list of strings; the engine joins
          them with double newlines so the report's ``lessons`` field
          stays a single string. ``parsed["recommended_followups"]`` is
          forwarded as a list verbatim.
        * Raw ``dict`` (legacy / test pattern) — passed straight
          through. The downstream sampler-overlay code in
          ``write_final_report`` already validates and silently drops
          malformed fields.
        * Anything else (including :class:`SamplingFallback`,
          ``None``, exceptions) — returns ``None`` so the
          deterministic templates from
          :func:`mcp.mission.final_report.build_deterministic_report`
          stand on their own.

        The method swallows any exception raised by the callable
        because the Final_Report is the durable exit artifact and a
        flaky sampler must not block it from landing.
        """
        del verdict, reason  # forwarded only for symmetry with the legacy Sampler shape
        if self.final_lessons_callable is None:
            return None
        if not session.get("use_sampling"):
            return None
        try:
            result = await self.final_lessons_callable(session=session)
        except Exception:
            return None
        if isinstance(result, SamplingUsed):
            lessons = result.parsed.get("lessons")
            followups = result.parsed.get("recommended_followups")
            overlay: dict[str, Any] = {}
            if isinstance(lessons, list) and all(isinstance(item, str) for item in lessons):
                # write_final_report expects ``lessons`` as a single
                # string; join with blank lines so multi-bullet output
                # from the model stays readable on render.
                overlay["lessons"] = "\n\n".join(lessons)
            if isinstance(followups, list) and all(isinstance(item, str) for item in followups):
                overlay["recommended_followups"] = list(followups)
            return overlay or None
        if isinstance(result, SamplingFallback):
            return None
        if isinstance(result, dict):
            # Legacy raw-dict path — let the downstream overlay
            # validator do the structural check.
            return result
        return None


# ---------------------------------------------------------------------------
# Comparison helper
# ---------------------------------------------------------------------------


def _compare_numbers(value: float, op: str, target: float) -> bool:
    """Apply one of the six allowed numeric comparison operators."""
    if op == "<":
        return value < target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    if op == ">=":
        return value >= target
    if op == "==":
        return value == target
    if op == "!=":
        return value != target
    raise ValueError(f"unknown comparison operator: {op!r}")
