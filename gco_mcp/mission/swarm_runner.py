"""The swarm Child_Runner: concurrent child driving under one supervisor.

This is the impure counterpart to :mod:`mission.swarm` (which holds every
pure rule). The runner owns one orchestrator session's fleet for the
lifetime of a drive call:

* **Supervisor tools.** ``mission_spawn`` / ``children_status`` /
  ``child_abort`` exist only as entries in the dispatcher wrapper built
  by :meth:`SwarmRunner.wrap_dispatcher` — injected into the orchestrator
  engine, invisible to FastMCP, unreachable from child or standalone
  sessions. Spawn admission is entirely :func:`mission.swarm.validate_spawn`.
* **Concurrent drivers.** Each spawned child gets an asyncio task looping
  the child's :class:`~mission.engine.MissionEngine` to a terminal state,
  bounded by an ``asyncio.Semaphore(max_concurrent_children)``.
* **Single-writer registry discipline.** The orchestrator's engine saves
  the orchestrator session during its own iterations, so the runner never
  writes the registry mid-iteration: mutations accumulate in memory and
  flush at iteration boundaries (and at finalization) onto a freshly
  loaded copy. Child drivers write only their own child sessions. A crash
  between spawn and flush leaves an orphan child session; the
  startup reconciliation pass adopts any persisted child whose
  ``parent_session_id`` matches but is missing from the registry.
* **Heartbeats and the single-runner guard.** One
  :class:`~tools._task_status.TaskStatusWriter` record per swarm
  (``swarm-{session_id}``) plus one per slot
  (``swarm-{session_id}-{slot}``). The swarm record doubles as the
  advisory same-host lock: a ``running`` record under a live foreign PID
  refuses startup; a dead PID reads as orphaned and is taken over.
* **Terminal cascade.** Whatever terminal verdict the orchestrator's
  unchanged cascade produces, the runner cancels drivers, aborts every
  non-terminal child through the same status transition
  ``mission_abort`` performs, settles and refunds each slot, emits
  lifecycle audit, and only then finishes its heartbeat.

Everything decision-shaped in here delegates to the pure module:
admission (:func:`~mission.swarm.validate_spawn`), pool arithmetic
(:func:`~mission.swarm.settle_entry` / :func:`~mission.swarm.respawn_entry`),
and the restart table (:func:`~mission.swarm.should_respawn`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-03T18:56:22Z
# Generated from Git commit: 37fd4384775eeebf18fea3e5e085cef9645077be
# Flowchart(s) generated from this file:
#   * ``SwarmRunner.run_to_completion`` -> ``diagrams/code_diagrams/gco_mcp/mission/swarm_runner.SwarmRunner_run_to_completion.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/mission/swarm_runner.SwarmRunner_run_to_completion.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


# Match the package's path-injection pattern (see _engine_factory.py):
# gco_mcp/ modules import each other with gco_mcp/ itself on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._task_status import TaskStatusWriter, get_task  # noqa: E402

from mission import audit as mission_audit  # noqa: E402
from mission import final_report  # noqa: E402
from mission import swarm as swarm_rules  # noqa: E402
from mission._engine_factory import EngineDependencies  # noqa: E402
from mission.engine import MissionEngine, ObservationAugmenter  # noqa: E402
from mission.types import (  # noqa: E402
    SCHEMA_VERSION,
    TERMINAL_STATES,
    ChildRegistryEntry,
    SessionState,
    SwarmConfig,
)
from mission.validation import MissionValidationError  # noqa: E402

__all__ = [
    "DepsBuilder",
    "SwarmRunner",
    "SwarmRunnerBusyError",
    "abort_swarm",
    "build_children_snapshot",
    "build_fleet_rollup",
    "list_swarms",
]

#: Async factory the runner calls once per engine it constructs. The CLI
#: binds this to ``build_engine_dependencies`` (live or ``--dry-run`` stub);
#: tests bind a stub. Receiving the session lets the builder resolve
#: sampling and sandbox wiring per session role.
DepsBuilder = Callable[[Mapping[str, Any]], Awaitable[EngineDependencies]]

#: Optional async reviser for ``on_failure_with_revision`` respawns. Takes
#: the failed child's terminal session, returns replacement directive text
#: or ``None`` (fall back to the original directive). Wired by the
#: scaffolder layer; the runner treats it as advisory text supply only —
#: the respawn *decision* never consults it.
DirectiveReviser = Callable[[SessionState], Awaitable[str | None]]

#: Seconds the orchestrator waits for observable fleet progress before
#: taking another iteration anyway. Bounds patience without removing it:
#: a wedged fleet still reaches the stagnation cascade, it just takes
#: ``stagnation_threshold`` windows to get there instead of spinning
#: through them in one event-loop turn.
DEFAULT_FLEET_PROGRESS_TIMEOUT = 30.0


class SwarmRunnerBusyError(RuntimeError):
    """Another live process already drives this swarm.

    Carries the holding record's PID so operator surfaces can print an
    actionable refusal instead of a bare failure.
    """

    def __init__(self, swarm_session_id: str, holder_pid: int | None) -> None:
        self.swarm_session_id = swarm_session_id
        self.holder_pid = holder_pid
        super().__init__(f"swarm {swarm_session_id} is already driven by live pid {holder_pid}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_children_snapshot(
    config: SwarmConfig,
    children: list[ChildRegistryEntry],
    load_child: Callable[[str], SessionState | None],
) -> dict[str, Any]:
    """Build the deterministic Children_Observation contribution.

    Pure given its inputs: slot-ordered entries plus the aggregate
    metrics, built from the registry and whatever ``load_child`` returns.
    A child whose session cannot be loaded surfaces with the distinct
    ``"unreadable"`` status token rather than being omitted, so criteria
    over the fleet read unmet/inconclusive instead of falsely met.
    """
    entries: list[dict[str, Any]] = []
    counts = {"running": 0, "completed": 0, "failed": 0}
    for entry in sorted(children, key=lambda e: e["slot"]):
        child = load_child(entry["session_id"])
        row: dict[str, Any] = {
            "slot": entry["slot"],
            "session_id": entry["session_id"],
            "respawn_count": entry["respawn_count"],
        }
        if child is None:
            row["status"] = "unreadable"
            counts["failed"] += 1
        else:
            status = str(child.get("status", "unreadable"))
            row["iterations_consumed"] = len(child.get("iterations", []))
            final_verdict = child.get("final_verdict")
            if final_verdict is not None:
                row["final_verdict"] = final_verdict
            # Supervision-aware status: a slot whose session ended
            # unmet but whose restart policy still owes it a respawn is
            # "respawning", not "failed" — otherwise a fleet criterion
            # like ``children_failed >= 1`` fires in the window between
            # a child's terminal save and its replacement, and the
            # orchestrator completes out from under its own policy.
            # Deterministic: computed purely from the entry + the
            # persisted child status through the same restart table the
            # runner itself uses.
            if status == "completed":
                row["status"] = status
                counts["completed"] += 1
            elif status in ("terminated", "failed"):
                wants_respawn, _reason = swarm_rules.should_respawn(entry, status)
                if wants_respawn:
                    row["status"] = "respawning"
                    counts["running"] += 1
                else:
                    row["status"] = status
                    counts["failed"] += 1
            else:
                row["status"] = status
                counts["running"] += 1
        entries.append(row)
    balance = swarm_rules.compute_pool_balance(config["child_iteration_pool"], children)
    return {
        "children": entries,
        "metrics": {
            "children_total": len(entries),
            "children_running": counts["running"],
            "children_completed": counts["completed"],
            "children_failed": counts["failed"],
            "iteration_pool_remaining": balance["remaining"],
        },
    }


class SwarmRunner:
    """Drives one orchestrator session's fleet to a terminal verdict."""

    def __init__(
        self,
        *,
        backend: Any,
        orchestrator_id: str,
        deps_builder: DepsBuilder,
        registered_tools: dict[str, Any],
        registered_tags: Mapping[str, set[str]],
        flag_lookup: dict[str, str] | None = None,
        revise_directive: DirectiveReviser | None = None,
        on_orchestrator_iteration: Callable[[Mapping[str, Any]], None] | None = None,
        fleet_progress_timeout: float = DEFAULT_FLEET_PROGRESS_TIMEOUT,
    ) -> None:
        session = backend.load_session(orchestrator_id)
        if session is None:
            raise MissionValidationError(
                "session_not_found", details={"session_id": orchestrator_id}
            )
        if session.get("role") != "orchestrator" or "swarm" not in session:
            raise MissionValidationError(
                "validation_error",
                details={"field": "role", "reason": "not_an_orchestrator"},
            )
        self._backend = backend
        self._orchestrator_id = orchestrator_id
        self._deps_builder = deps_builder
        self._registered_tools = registered_tools
        self._registered_tags = registered_tags
        self._flag_lookup = flag_lookup
        self._revise_directive = revise_directive
        # Optional per-iteration observer (the CLI's JSON-line verdict
        # stream). Called after each orchestrator iteration with the
        # iteration record; exceptions are the caller's problem by
        # design — a broken observer should fail the drive loudly.
        self._on_orchestrator_iteration = on_orchestrator_iteration
        self._config: SwarmConfig = session["swarm"]
        self._registry: list[ChildRegistryEntry] = list(session.get("children", []))
        self._allowlists: dict[str, list[str]] = {}
        self._semaphore = asyncio.Semaphore(self._config["max_concurrent_children"])
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Fleet-progress signal. The orchestrator observes children
        # through their persisted sessions, so it must not out-run them:
        # a real child iteration suspends many times, and an orchestrator
        # that only yielded one event-loop turn per iteration would spend
        # its whole stagnation window watching an untouched "pending"
        # fleet and terminate a swarm that was working fine. Drivers bump
        # the tick on every observable change (iteration recorded, slot
        # settled) and set the event to wake a waiting orchestrator.
        self._fleet_progress_timeout = fleet_progress_timeout
        self._progress_ticks = 0
        self._progress_event = asyncio.Event()
        self._dirty = False
        self._swarm_writer: TaskStatusWriter | None = None
        self._child_writers: dict[str, TaskStatusWriter] = {}

    # ------------------------------------------------------------------ #
    # Registry helpers (single-writer: this class, at await boundaries)
    # ------------------------------------------------------------------ #

    def _entry_index(self, slot: str) -> int:
        for index, entry in enumerate(self._registry):
            if entry["slot"] == slot:
                return index
        raise KeyError(slot)

    def _live_entries(self) -> list[ChildRegistryEntry]:
        return [entry for entry in self._registry if not entry.get("settled")]

    def _flush_registry(self) -> None:
        """Persist the in-memory registry onto a freshly loaded session.

        Loading fresh means the orchestrator engine's own most-recent
        save (iterations, status, report paths) is preserved and only
        the ``children`` key is replaced — the runner is the single
        writer of that key, the engine of everything else.
        """
        if not self._dirty:
            return
        session = self._backend.load_session(self._orchestrator_id)
        if session is None:
            return
        session["children"] = list(self._registry)
        self._backend.save_session(session)
        self._dirty = False

    def _reconcile_orphans(self) -> None:
        """Adopt persisted children missing from the registry.

        A crash between a spawn's child-session write and the next
        registry flush leaves a child on disk that the registry never
        recorded. Adoption rebuilds a conservative entry (reservation =
        the child's own iteration cap, restart ``never``) so pool
        accounting stays honest across the crash.
        """
        known_ids = {entry["session_id"] for entry in self._registry}
        for entry in self._registry:
            known_ids.update(entry.get("prior_session_ids", []))
        # The backend's list filter supports ``status`` only and its
        # summaries carry no parent linkage, so this is deliberately
        # list-all followed by a load-and-verify per candidate — the
        # state protocol stays untouched.
        listed = self._backend.list_sessions()
        for summary in listed:
            child_id = summary.get("session_id")
            if not child_id or child_id in known_ids:
                continue
            child = self._backend.load_session(child_id)
            if child is None or child.get("parent_session_id") != self._orchestrator_id:
                continue
            slot = f"adopted-{child_id[-8:]}"
            adopted: ChildRegistryEntry = {
                "slot": slot,
                "session_id": child_id,
                "spawned_at": str(child.get("created_at", _now_iso())),
                "reserved_iterations": int(child["budget"]["max_iterations"]),
                "restart_policy": "never",
                "max_respawns": 0,
                "respawn_count": 0,
                "consumed_iterations": 0,
            }
            self._registry.append(adopted)
            self._dirty = True
            mission_audit.emit_child_lifecycle_event(
                self._orchestrator_id, child_id, slot, "spawned", reason="adopted_orphan"
            )

    # ------------------------------------------------------------------ #
    # Heartbeats and the single-runner guard
    # ------------------------------------------------------------------ #

    def _swarm_task_id(self) -> str:
        return f"swarm-{self._orchestrator_id}"

    def _child_task_id(self, slot: str) -> str:
        return f"swarm-{self._orchestrator_id}-{slot}"

    def _acquire_guard(self) -> None:
        """Refuse startup when a live foreign process drives this swarm.

        The probe is the swarm heartbeat record itself: ``get_task``
        already performs PID-liveness orphan rewriting, so a record
        still reading ``state=running`` with ``is_alive`` under another
        PID means a genuinely live runner. A dead PID reads as orphaned
        and is taken over. Advisory and same-host by design — the scope
        of the disk-backed task channel.
        """
        record = get_task(self._swarm_task_id())
        if (
            record is not None
            and record.get("state") == "running"
            and record.get("is_alive")
            and record.get("pid") not in (None, os.getpid())
        ):
            raise SwarmRunnerBusyError(self._orchestrator_id, record.get("pid"))
        self._swarm_writer = TaskStatusWriter(
            self._swarm_task_id(),
            "swarm_run",
            [self._orchestrator_id],
            pid=os.getpid(),
        )

    def _child_writer(self, slot: str) -> TaskStatusWriter:
        writer = self._child_writers.get(slot)
        if writer is None:
            writer = TaskStatusWriter(
                self._child_task_id(slot),
                "swarm_child",
                [self._orchestrator_id, slot],
                pid=os.getpid(),
            )
            self._child_writers[slot] = writer
        return writer

    def _heartbeat(self, line: str) -> None:
        if self._swarm_writer is not None:
            self._swarm_writer.record_line(line, stream="stdout")

    # ------------------------------------------------------------------ #
    # Supervisor tools
    # ------------------------------------------------------------------ #

    def wrap_dispatcher(self, inner: Any) -> Any:
        """Route supervisor names in-process; everything else falls through."""

        async def dispatch(tool_name: str, args: dict[str, Any], ctx: Any) -> Any:
            if tool_name == "mission_spawn":
                return await self.spawn(args)
            if tool_name == "children_status":
                return self.children_status()
            if tool_name == "child_abort":
                return await self.abort_child(str(args.get("slot", "")))
            return await inner(tool_name, args, ctx)

        return dispatch

    def observation_augmenter(self) -> ObservationAugmenter:
        """The Children_Observation contribution for the orchestrator engine."""

        def augment(session: SessionState) -> dict[str, Any]:
            del session  # snapshot reads the runner's authoritative registry
            return self.children_status()

        return augment

    def children_status(self) -> dict[str, Any]:
        """Deterministic fleet snapshot (tool result and augmenter payload)."""
        return build_children_snapshot(self._config, self._registry, self._backend.load_session)

    async def spawn(
        self, request: Mapping[str, Any], *, respawn_of_slot: str | None = None
    ) -> dict[str, Any]:
        """Admit, persist, register, and schedule one child.

        Returns the spawn result envelope on success or the standard
        ``{"code", "details"}`` envelope on rejection — the orchestrator
        iteration continues either way, and a sampled strategy sees the
        precise rejection reason in its next revision prompt.
        """
        sibling_allowlists = {
            entry["slot"]: self._allowlists.get(entry["slot"], []) for entry in self._live_entries()
        }
        try:
            spec = swarm_rules.validate_spawn(
                parent_role="orchestrator",
                config=self._config,
                children=self._registry,
                request=request,
                registered_tools=self._registered_tools,
                registered_tags=self._registered_tags,
                sibling_allowlists=sibling_allowlists,
                flag_lookup=self._flag_lookup,
                respawn_of_slot=respawn_of_slot,
            )
        except MissionValidationError as err:
            if respawn_of_slot is not None:
                mission_audit.emit_child_lifecycle_event(
                    self._orchestrator_id,
                    None,
                    respawn_of_slot,
                    "respawn_denied",
                    reason=str((err.details or {}).get("reason", err.code)),
                )
            return {"code": err.code, "details": err.details}

        child_id = f"mission-{secrets.token_hex(8)}"
        child: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "session_id": child_id,
            "directive_text": spec["directive"],
            "criteria": _strip_parsed_asts(spec["criteria"]),
            "budget": spec["budget"],
            "tool_allowlist": spec["tool_allowlist"],
            "checkpoint_cadence": spec["checkpoint_cadence"],
            "stagnation_threshold": 3,
            "use_sampling": spec["use_sampling"],
            "sampling_backend_resolved": "none" if not spec["use_sampling"] else "bedrock",
            "allow_scripted_strategies": False,
            "status": "pending",
            "created_at": _now_iso(),
            "iterations": [],
            "no_progress_counter": 0,
            "role": "child",
            "parent_session_id": self._orchestrator_id,
        }
        self._backend.save_session(cast("SessionState", child))

        if respawn_of_slot is None:
            entry = swarm_rules.new_registry_entry(spec, child_id, _now_iso())
            self._registry.append(entry)
            action = "spawned"
        else:
            index = self._entry_index(respawn_of_slot)
            entry = swarm_rules.respawn_entry(
                self._registry[index],
                new_session_id=child_id,
                reserved_iterations=spec["budget"]["max_iterations"],
                spawned_at=_now_iso(),
            )
            self._registry[index] = entry
            action = "respawned"
        self._allowlists[spec["slot"]] = list(spec["tool_allowlist"])
        self._dirty = True
        mission_audit.emit_child_lifecycle_event(
            self._orchestrator_id, child_id, spec["slot"], action
        )
        self._schedule_child(spec["slot"])
        balance = swarm_rules.compute_pool_balance(
            self._config["child_iteration_pool"], self._registry
        )
        return {
            "spawned": True,
            "slot": spec["slot"],
            "child_session_id": child_id,
            "pool_remaining": balance["remaining"],
        }

    async def abort_child(self, slot: str) -> dict[str, Any]:
        """Abort one live slot: cancel its driver, terminate, settle."""
        try:
            index = self._entry_index(slot)
        except KeyError:
            return {
                "code": "validation_error",
                "details": {"field": "spawn", "reason": "unknown_slot", "slot": slot},
            }
        task = self._tasks.get(slot)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        entry = self._registry[index]
        if not entry.get("settled"):
            consumed = self._terminate_child_session(entry["session_id"])
            self._registry[index] = swarm_rules.settle_entry(entry, consumed)
            self._dirty = True
            mission_audit.emit_child_lifecycle_event(
                self._orchestrator_id, entry["session_id"], slot, "aborted"
            )
        return {"aborted": True, "slot": slot}

    def _terminate_child_session(self, child_id: str) -> int:
        """Apply the ``mission_abort`` terminal transition to a child.

        Returns the child's recorded iteration count for settlement; a
        missing session settles at zero consumption (full refund) since
        nothing demonstrably ran.
        """
        child = self._backend.load_session(child_id)
        if child is None:
            return 0
        if child["status"] not in TERMINAL_STATES:
            child["status"] = "terminated"
            child["final_verdict"] = "terminate"
            child["ended_at"] = _now_iso()
            self._backend.save_session(child)
        return len(child.get("iterations", []))

    # ------------------------------------------------------------------ #
    # Child drivers
    # ------------------------------------------------------------------ #

    def _note_fleet_progress(self) -> None:
        """Record observable child progress and wake a waiting orchestrator."""
        self._progress_ticks += 1
        self._progress_event.set()

    async def _await_fleet_progress(self, observed_ticks: int) -> None:
        """Yield until the fleet changes under the orchestrator's feet.

        ``observed_ticks`` is the tick count captured *before* the
        orchestrator iteration that just ran, so progress that landed
        while that iteration was in flight counts and costs no wait.

        With no live children there is nothing to wait for: yield one
        turn and let the orchestrator's own criteria and cascade decide.
        A wedged fleet falls through on timeout, which is the honest
        outcome — the orchestrator waited and nothing moved.
        """
        if not self._live_entries() or self._progress_ticks != observed_ticks:
            await asyncio.sleep(0)
            return
        if not any(not task.done() for task in self._tasks.values()):
            # Live slots but no running driver (e.g. a cancelled driver
            # left a slot unsettled): nothing can produce progress, so
            # waiting would only stall the cascade.
            await asyncio.sleep(0)
            return
        # No await between the tick check and the clear, so no driver can
        # interleave and have its signal dropped (single-threaded loop).
        self._progress_event.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._progress_event.wait(), timeout=self._fleet_progress_timeout
            )

    def _schedule_child(self, slot: str) -> None:
        existing = self._tasks.get(slot)
        if existing is not None and not existing.done():
            return
        self._tasks[slot] = asyncio.get_running_loop().create_task(self._drive_child(slot))

    async def _drive_child(self, slot: str) -> None:
        writer = self._child_writer(slot)
        entry = self._registry[self._entry_index(slot)]
        child_id = entry["session_id"]
        try:
            child = self._backend.load_session(child_id)
            if child is None:
                self._settle_slot(slot, consumed=entry["reserved_iterations"], status="failed")
                writer.finish(state="failed", error="child session unreadable")
                return
            engine = await self._build_child_engine(child)
            while True:
                current = self._backend.load_session(child_id)
                if current is None or current["status"] in TERMINAL_STATES:
                    break
                async with self._semaphore:
                    record = await engine.run_iteration(child_id)
                writer.record_line(
                    f"slot={slot} verdict={record.get('verdict')}"
                    f" reason={record.get('verdict_reason')}",
                    stream="stdout",
                )
                self._note_fleet_progress()
                # No explicit yield here: when the iteration that just ran
                # was the child's last, the next loop pass must reach the
                # settle + restart-policy step without the orchestrator
                # observing the raw terminal session in between. Real
                # engine work yields at its own await points; the
                # orchestrator loop carries the fairness yield.
            final = self._backend.load_session(child_id)
            final_status = str(final["status"]) if final is not None else "failed"
            consumed = len(final.get("iterations", [])) if final is not None else 0
            self._settle_slot(slot, consumed=consumed, status=final_status)
            writer.finish(state="succeeded" if final_status == "completed" else "failed")
            await self._maybe_respawn(slot, final_status, final)
        except asyncio.CancelledError:
            writer.finish(state="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — a driver bug must not kill the swarm
            # Persist the standard abort transition before settling the slot.
            consumed = self._terminate_child_session(child_id)
            self._settle_slot(slot, consumed=consumed, status="failed")
            writer.finish(state="failed", error=str(exc))

    def _settle_slot(self, slot: str, *, consumed: int, status: str) -> None:
        index = self._entry_index(slot)
        entry = self._registry[index]
        if entry.get("settled"):
            return
        self._registry[index] = swarm_rules.settle_entry(entry, consumed)
        self._dirty = True
        self._note_fleet_progress()
        mission_audit.emit_child_lifecycle_event(
            self._orchestrator_id,
            entry["session_id"],
            slot,
            "terminal",
            final_status=status,
        )

    async def _maybe_respawn(
        self, slot: str, final_status: str, final_session: SessionState | None
    ) -> None:
        entry = self._registry[self._entry_index(slot)]
        decision, reason = swarm_rules.should_respawn(entry, final_status)
        if not decision:
            return
        directive = str(final_session.get("directive_text", "")) if final_session else ""
        if (
            entry["restart_policy"] == "on_failure_with_revision"
            and self._revise_directive is not None
            and final_session is not None
        ):
            with contextlib.suppress(Exception):
                revised = await self._revise_directive(final_session)
                if revised:
                    directive = revised
        request: dict[str, Any] = {
            "slot": slot,
            "directive": directive,
            "criteria": _strip_parsed_asts(list(final_session.get("criteria", [])))
            if final_session
            else [],
            "budget": dict(final_session["budget"]) if final_session else {},
            "tool_allowlist": list(final_session.get("tool_allowlist", []))
            if final_session
            else [],
            "restart_policy": entry["restart_policy"],
            "max_respawns": entry["max_respawns"],
            "use_sampling": bool(final_session.get("use_sampling", False))
            if final_session
            else False,
        }
        await self.spawn(request, respawn_of_slot=slot)

    async def _build_child_engine(self, child: SessionState) -> MissionEngine:
        deps = await self._deps_builder(child)
        return MissionEngine(
            backend=self._backend,
            tool_dispatcher=deps.tool_dispatcher,
            sampling_callable=deps.sampling_callable,
            sandbox_runner=deps.sandbox_runner,
            final_lessons_callable=deps.final_lessons_callable,
            memory_store=deps.memory_store,
        )

    async def _build_orchestrator_engine(self, session: SessionState) -> MissionEngine:
        deps = await self._deps_builder(session)
        return MissionEngine(
            backend=self._backend,
            tool_dispatcher=self.wrap_dispatcher(deps.tool_dispatcher),
            sampling_callable=deps.sampling_callable,
            sandbox_runner=deps.sandbox_runner,
            final_lessons_callable=deps.final_lessons_callable,
            memory_store=deps.memory_store,
            observation_augmenters=[self.observation_augmenter()],
        )

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def _load_orchestrator_session(self) -> SessionState:
        """Load the orchestrator or report concurrent deletion consistently."""
        session = self._backend.load_session(self._orchestrator_id)
        if session is None:
            raise MissionValidationError(
                "session_not_found",
                details={"session_id": self._orchestrator_id},
            )
        return cast("SessionState", session)

    async def run_to_completion(
        self, *, max_orchestrator_iterations: int | None = None
    ) -> SessionState:
        """Drive orchestrator and fleet until the orchestrator is terminal.

        Also the resume path: on startup every live registry slot gets a
        driver scheduled, and children that went terminal while
        unsupervised get their restart policy evaluated on settlement.

        ``max_orchestrator_iterations`` bounds one call's orchestrator
        iterations (the ``mission_iterate`` shape). Hitting the bound
        **detaches** rather than terminates: drivers are cancelled,
        children stay non-terminal and resumable, no abort cascade runs,
        and the swarm heartbeat finishes ``cancelled`` so the next
        runner's guard sees a released fleet. The abort cascade runs
        only on a genuinely terminal (or already-terminal) orchestrator.
        """
        self._acquire_guard()
        try:
            self._reconcile_orphans()
            self._flush_registry()
            session = self._load_orchestrator_session()
            engine = await self._build_orchestrator_engine(session)
            for entry in self._live_entries():
                self._schedule_child(entry["slot"])
            terminal = False
            ran = 0
            observed_ticks = self._progress_ticks
            while True:
                current = self._load_orchestrator_session()
                if current["status"] in TERMINAL_STATES:
                    terminal = True
                    break
                if current["status"] == "paused":
                    break
                if max_orchestrator_iterations is not None and ran >= max_orchestrator_iterations:
                    break
                # Gate *before* the next iteration, never after the last
                # one: every exit above must leave without paying the
                # wait, or bounded (``mission_iterate``-shaped) calls and
                # terminal cascades would stall on a fleet nobody is
                # waiting for. The first iteration is the orchestrator's
                # initial assessment and never waits.
                if ran > 0:
                    await self._await_fleet_progress(observed_ticks)
                observed_ticks = self._progress_ticks
                record = await engine.run_iteration(self._orchestrator_id)
                ran += 1
                self._flush_registry()
                self._heartbeat(
                    f"iteration verdict={record.get('verdict')}"
                    f" reason={record.get('verdict_reason')}"
                )
                if self._on_orchestrator_iteration is not None:
                    self._on_orchestrator_iteration(record)
                if record.get("verdict") in ("complete", "terminate"):
                    terminal = True
                    break
            if terminal:
                await self._cascade_shutdown()
                self._flush_registry()
                final = self._load_orchestrator_session()
                self._refresh_report_children(final)
                if self._swarm_writer is not None:
                    succeeded = final.get("final_verdict") == "complete"
                    self._swarm_writer.finish(state="succeeded" if succeeded else "failed")
                return final
            # Detached (iteration bound or pause): leave the fleet
            # resumable and release the guard record.
            self._flush_registry()
            detached = self._load_orchestrator_session()
            if self._swarm_writer is not None:
                self._swarm_writer.finish(state="cancelled")
            return detached
        finally:
            for task in self._tasks.values():
                if not task.done():
                    task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._flush_registry()

    def _refresh_report_children(self, final: SessionState) -> None:
        """Rewrite the report's per-child table with post-cascade states.

        The engine writes the Final_Report during terminal finalization,
        which runs *before* the abort cascade settles the last registry
        entries. Refreshing the ``swarm_children`` table afterwards makes
        the durable artifact reflect final supervision outcomes.
        Best-effort: a missing or unwritable report file never fails the
        swarm — the session itself already carries the settled registry.
        """
        report_path = final.get("final_report_path")
        registry = final.get("children")
        if not report_path or registry is None:
            return
        with contextlib.suppress(OSError, ValueError):
            path = Path(report_path)
            report = json.loads(path.read_text(encoding="utf-8"))
            report["swarm_children"] = final_report.build_swarm_children_table(final)
            path.write_text(json.dumps(report), encoding="utf-8")

    async def _cascade_shutdown(self) -> None:
        """Cancel drivers and abort every non-terminal child, settling each."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        for entry in list(self._live_entries()):
            slot = entry["slot"]
            consumed = self._terminate_child_session(entry["session_id"])
            index = self._entry_index(slot)
            self._registry[index] = swarm_rules.settle_entry(self._registry[index], consumed)
            self._dirty = True
            mission_audit.emit_child_lifecycle_event(
                self._orchestrator_id, entry["session_id"], slot, "aborted"
            )
            writer = self._child_writers.get(slot)
            if writer is not None:
                writer.finish(state="cancelled")


# ---------------------------------------------------------------------------
# Small local helpers
# ---------------------------------------------------------------------------


def _strip_parsed_asts(criteria: list[Any]) -> list[Any]:
    """Drop the validator's cached ``_parsed_ast`` before persistence.

    Mirrors the ``mission_start`` convention: AST nodes are not
    JSON-serialisable; the engine re-parses from ``expression`` on load.
    """
    cleaned: list[Any] = []
    for criterion in criteria:
        if isinstance(criterion, dict):
            cleaned.append({k: v for k, v in criterion.items() if k != "_parsed_ast"})
        else:
            cleaned.append(criterion)
    return cleaned


# ---------------------------------------------------------------------------
# Shared operator-surface helpers (MCP tools and CLI both consume these)
# ---------------------------------------------------------------------------


def build_fleet_rollup(backend: Any, session: SessionState) -> dict[str, Any]:
    """One-call fleet document for an orchestrator session.

    Swarm summary, rails, pool balance, slot table, runner heartbeat
    state, and a findings list — the ``fleet_status`` shape. Pure given
    the backend reads; the heartbeat probe rides the task-status
    channel's own orphan detection.
    """
    session_id = session["session_id"]
    config = session["swarm"]
    registry = list(session.get("children", []))
    snapshot = build_children_snapshot(config, registry, backend.load_session)
    balance = swarm_rules.compute_pool_balance(config["child_iteration_pool"], registry)
    findings: list[str] = []
    heartbeat = get_task(f"swarm-{session_id}")
    runner_state = heartbeat.get("state") if heartbeat else None
    # Only actionable while the swarm can still be driven. On a terminal
    # orchestrator an orphaned heartbeat is the expected trace of a
    # runner whose swarm went terminal under it (an external
    # ``swarm abort``, say) — recommending ``swarm iterate`` there points
    # at a resume that cannot happen. Matches the pool finding below,
    # which is likewise scoped to non-terminal sessions.
    if runner_state == "orphaned" and session["status"] not in TERMINAL_STATES:
        findings.append(
            "runner heartbeat is orphaned: the driving process died mid-swarm; "
            "swarm iterate resumes the fleet"
        )
    unreadable = [row["slot"] for row in snapshot["children"] if row["status"] == "unreadable"]
    if unreadable:
        findings.append(f"unreadable child sessions: {', '.join(unreadable)}")
    if balance["remaining"] == 0 and session["status"] not in TERMINAL_STATES:
        findings.append("iteration pool exhausted: no further spawns can be admitted")
    return {
        "session_id": session_id,
        "status": session["status"],
        "final_verdict": session.get("final_verdict"),
        "directive_text": session["directive_text"],
        "swarm": config,
        "pool": balance,
        "runner_state": runner_state,
        "children": snapshot["children"],
        "children_metrics": snapshot["metrics"],
        "findings": findings,
    }


def abort_swarm(backend: Any, session: SessionState) -> dict[str, Any]:
    """Terminate an orchestrator and abort every non-terminal child.

    The runnerless abort path (``swarm_abort`` / ``gco swarm abort``):
    applies the standard terminal transition to the orchestrator, then
    the abort transition plus settlement to each live slot, emitting
    child-lifecycle audit per slot. A live runner observing the
    terminal orchestrator at its next boundary stands down; its own
    cascade then finds the children already terminal.
    """
    now_iso = _now_iso()
    session["status"] = "terminated"
    session["final_verdict"] = "terminate"
    session["ended_at"] = now_iso
    registry = list(session.get("children", []))
    aborted = 0
    for index, entry in enumerate(registry):
        if entry.get("settled"):
            continue
        child = backend.load_session(entry["session_id"])
        consumed = 0
        if child is not None:
            if child["status"] not in TERMINAL_STATES:
                child["status"] = "terminated"
                child["final_verdict"] = "terminate"
                child["ended_at"] = now_iso
                backend.save_session(child)
            consumed = len(child.get("iterations", []))
        registry[index] = swarm_rules.settle_entry(entry, consumed)
        mission_audit.emit_child_lifecycle_event(
            session["session_id"], entry["session_id"], entry["slot"], "aborted"
        )
        aborted += 1
    session["children"] = registry
    backend.save_session(session)
    return {
        "session_id": session["session_id"],
        "status": "terminated",
        "children_aborted": aborted,
    }


def list_swarms(backend: Any, *, status: str | None = None) -> list[dict[str, Any]]:
    """Summaries of every orchestrator session on the backend.

    List-all followed by load-and-verify per candidate — the state
    protocol's summaries carry no role field, and swarm counts are
    small, so the extra loads stay cheap.
    """
    rows: list[dict[str, Any]] = []
    for summary in backend.list_sessions(filter={"status": status} if status else None):
        session = backend.load_session(str(summary.get("session_id", "")))
        if session is None or session.get("role") != "orchestrator":
            continue
        registry = list(session.get("children", []))
        rows.append(
            {
                "session_id": session["session_id"],
                "status": session["status"],
                "created_at": session.get("created_at"),
                "children_total": len(registry),
                "children_live": sum(1 for e in registry if not e.get("settled")),
            }
        )
    return rows
