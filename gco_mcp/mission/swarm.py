"""Pure swarm-supervision primitives: validation, pool accounting, policy.

A swarm is one **orchestrator** Mission session supervising N **child**
Mission sessions. This module holds everything about that relationship
that can be expressed as pure functions — no I/O, no clocks, no
environment lookups, no FastMCP imports — so the whole admission and
accounting surface is unit- and property-testable in isolation:

* :func:`validate_swarm_config` — the swarm-level rails
  (:class:`~.types.SwarmConfig`).
* :func:`validate_spawn` — the full spawn-admission pipeline, run for
  every ``mission_spawn`` dispatch and for every scaffolded plan entry.
  First failure wins; every rejection is a
  :class:`~.validation.MissionValidationError` with a stable
  ``details.reason`` token, so a sampled decomposition gets precise
  feedback and a retry prompt can quote the exact rule it broke.
* Pool accounting — :func:`compute_pool_balance`, :func:`settle_entry`,
  :func:`respawn_entry`, :func:`new_registry_entry`. Reservation model:
  a spawn reserves the child's ``max_iterations`` from the pool; the
  settle step on a terminal child folds the actually-recorded iteration
  count into ``consumed_iterations`` and thereby refunds the unused
  remainder.
* :func:`should_respawn` — the deterministic restart-policy table. The
  respawn *decision* never depends on a sampler; only the optional
  replacement-directive text does (elsewhere).

The supervisor tools themselves (``mission_spawn`` / ``children_status``
/ ``child_abort``) are **in-process dispatcher entries** wired by the
runner for orchestrator sessions only. They are never registered with
the MCP server, and — together with the operator-facing ``swarm_*`` MCP
tools and the ``mission_*`` control tools — they are excluded from every
resolvable session allowlist (:data:`SWARM_EXCLUDED_TOOLS`), so no loop
can drive loops except through this validated seam.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final, TypedDict, cast

from .types import (
    BudgetControls,
    Cadence,
    ChildRegistryEntry,
    Criterion,
    RestartPolicy,
    SwarmConfig,
)
from .validation import (
    SUPERVISOR_TOOLS,
    SWARM_EXCLUDED_TOOLS,
    SWARM_MCP_TOOLS,
    MissionValidationError,
    resolve_effective_allowlist,
    validate_cadence,
    validate_criteria,
    validate_directive,
)

__all__ = [
    "DEFAULT_MAX_CONCURRENT_CHILDREN",
    "DEFAULT_RESPAWNS_BY_POLICY",
    "RESTART_POLICIES",
    "SUPERVISOR_TOOLS",
    "SUPERVISOR_TOOL_DOCSTRINGS",
    "SUPERVISOR_TOOL_SCHEMAS",
    "SWARM_EXCLUDED_TOOLS",
    "SWARM_MCP_TOOLS",
    "PoolBalance",
    "SpawnSpec",
    "build_orchestrator_session",
    "compute_pool_balance",
    "new_registry_entry",
    "respawn_entry",
    "settle_entry",
    "should_respawn",
    "validate_spawn",
    "validate_swarm_config",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESTART_POLICIES: Final[frozenset[str]] = frozenset(
    {"never", "on_failure", "on_failure_with_revision"}
)
"""The valid ``restart_policy`` values on a spawn request."""

DEFAULT_MAX_CONCURRENT_CHILDREN: Final[int] = 3
"""Default concurrency bound on simultaneously advancing children.

Deliberately small: it is the swarm's primary throughput control (tool
fan-out and, when children opt into sampling, concurrent Bedrock calls).
"""

DEFAULT_RESPAWNS_BY_POLICY: Final[dict[str, int]] = {
    "never": 0,
    "on_failure": 1,
    "on_failure_with_revision": 1,
}
"""Default ``max_respawns`` per restart policy when the request omits it."""

_SAFE_TAG: Final[str] = "safe"
"""The risk-tier tag marking a tool read-only for the overlap check."""

_SLOT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""Slot names: 1-64 chars, alphanumeric plus ``. _ -``, no whitespace.

Slots become file-name fragments (task-status heartbeats) and audit keys,
so the charset is deliberately conservative.
"""


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


class PoolBalance(TypedDict):
    """A point-in-time view of the child-iteration pool.

    ``reserved`` counts live (non-settled) entries' reservations;
    ``consumed`` sums settled consumption; ``remaining`` is what a new
    spawn may draw from. The admission pipeline keeps ``remaining``
    non-negative by construction.
    """

    pool: int
    reserved: int
    consumed: int
    remaining: int


class SpawnSpec(TypedDict):
    """A fully validated, normalized child specification.

    Everything a runner needs to persist a child session and register the
    slot. Produced only by :func:`validate_spawn`; consuming code may
    trust every field.
    """

    slot: str
    directive: str
    criteria: list[Criterion]
    budget: BudgetControls
    tool_allowlist: list[str]
    checkpoint_cadence: Cadence
    restart_policy: RestartPolicy
    max_respawns: int
    use_sampling: bool


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _is_positive_int(value: Any) -> bool:
    """Return True iff ``value`` is an int (not bool) and strictly > 0."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_negative_int(value: Any) -> bool:
    """Return True iff ``value`` is an int (not bool) and >= 0."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject(field: str, reason: str, **extra: Any) -> MissionValidationError:
    """Build the standard structured rejection for this module."""
    details: dict[str, Any] = {"field": field, "reason": reason}
    details.update(extra)
    return MissionValidationError("validation_error", details=details)


def _is_safe_tool(name: str, registered_tags: Mapping[str, Collection[str]]) -> bool:
    """Return True iff the tool is known and carries the ``safe`` tag.

    Unknown names are treated as **not** safe: the overlap rail fails
    closed when tag information is missing.
    """
    tags = registered_tags.get(name)
    return tags is not None and _SAFE_TAG in tags


# ---------------------------------------------------------------------------
# Swarm config
# ---------------------------------------------------------------------------


def validate_swarm_config(config: dict[str, Any]) -> SwarmConfig:
    """Validate the swarm-level rails and normalize defaults.

    Required: ``max_children`` and ``child_iteration_pool``, each a
    strictly-positive int — there is deliberately **no** ``-1`` uncapped
    sentinel at the swarm level; an unbounded fleet or pool is exactly
    the runaway shape these rails exist to prevent. Optional:
    ``max_concurrent_children`` (default
    :data:`DEFAULT_MAX_CONCURRENT_CHILDREN`) and
    ``allow_overlapping_mutating_tools`` (default ``False``).

    Returns a normalized :class:`~.types.SwarmConfig` carrying all four
    keys.
    """
    if not isinstance(config, dict):
        raise _reject("swarm", "not_a_dict")
    max_children = config.get("max_children")
    if not _is_positive_int(max_children):
        raise _reject("swarm", "missing_or_not_positive_int", subfield="max_children")
    pool = config.get("child_iteration_pool")
    if not _is_positive_int(pool):
        raise _reject("swarm", "missing_or_not_positive_int", subfield="child_iteration_pool")
    concurrency = config.get("max_concurrent_children", DEFAULT_MAX_CONCURRENT_CHILDREN)
    if not _is_positive_int(concurrency):
        raise _reject("swarm", "not_positive_int", subfield="max_concurrent_children")
    allow_overlap = config.get("allow_overlapping_mutating_tools", False)
    if not isinstance(allow_overlap, bool):
        raise _reject("swarm", "not_a_bool", subfield="allow_overlapping_mutating_tools")
    normalized: dict[str, Any] = {
        "max_children": max_children,
        "child_iteration_pool": pool,
        "max_concurrent_children": concurrency,
        "allow_overlapping_mutating_tools": allow_overlap,
    }
    return cast("SwarmConfig", normalized)


# ---------------------------------------------------------------------------
# Pool accounting
# ---------------------------------------------------------------------------


def compute_pool_balance(
    child_iteration_pool: int,
    children: Sequence[ChildRegistryEntry],
) -> PoolBalance:
    """Compute the pool view from the registry alone.

    Live (non-settled) entries hold their full ``reserved_iterations``
    against the pool; settled entries contribute only their
    ``consumed_iterations`` (the settle step already folded the refund).
    """
    reserved = sum(e["reserved_iterations"] for e in children if not e.get("settled"))
    consumed = sum(e["consumed_iterations"] for e in children)
    return {
        "pool": child_iteration_pool,
        "reserved": reserved,
        "consumed": consumed,
        "remaining": child_iteration_pool - reserved - consumed,
    }


def new_registry_entry(spec: SpawnSpec, session_id: str, spawned_at: str) -> ChildRegistryEntry:
    """Build the registry entry for a freshly spawned slot."""
    entry: ChildRegistryEntry = {
        "slot": spec["slot"],
        "session_id": session_id,
        "spawned_at": spawned_at,
        "reserved_iterations": spec["budget"]["max_iterations"],
        "restart_policy": spec["restart_policy"],
        "max_respawns": spec["max_respawns"],
        "respawn_count": 0,
        "consumed_iterations": 0,
    }
    return entry


def settle_entry(entry: ChildRegistryEntry, iterations_recorded: int) -> ChildRegistryEntry:
    """Fold a terminal session's consumption into the slot; refund the rest.

    Consumption is clamped to ``[0, reserved_iterations]`` — the engine's
    own budget cap guarantees a child never records more iterations than
    its reservation, and the clamp keeps the pool arithmetic sound even
    against a corrupted count. Settling an already-settled entry is a
    no-op (idempotent), so a crash between settle and persist cannot
    double-count on replay.

    Returns a new entry; the input is not mutated.
    """
    if entry.get("settled"):
        return entry
    reserved = entry["reserved_iterations"]
    folded = min(max(iterations_recorded, 0), reserved)
    updated = dict(entry)
    updated["consumed_iterations"] = entry["consumed_iterations"] + folded
    updated["reserved_iterations"] = 0
    updated["settled"] = True
    return cast("ChildRegistryEntry", updated)


def respawn_entry(
    entry: ChildRegistryEntry,
    *,
    new_session_id: str,
    reserved_iterations: int,
    spawned_at: str,
) -> ChildRegistryEntry:
    """Point a settled slot at its replacement session.

    The prior session id moves into the lineage list, the respawn count
    increments, and the new reservation goes live. Respawning an
    unsettled entry is a supervision bug, rejected loudly rather than
    silently corrupting the pool.

    Returns a new entry; the input is not mutated.
    """
    if not entry.get("settled"):
        raise _reject("spawn", "respawn_before_settle", slot=entry["slot"])
    updated = dict(entry)
    lineage = list(entry.get("prior_session_ids", []))
    lineage.append(entry["session_id"])
    updated["prior_session_ids"] = lineage
    updated["session_id"] = new_session_id
    updated["spawned_at"] = spawned_at
    updated["reserved_iterations"] = reserved_iterations
    updated["respawn_count"] = entry["respawn_count"] + 1
    updated.pop("settled", None)
    return cast("ChildRegistryEntry", updated)


# ---------------------------------------------------------------------------
# Restart policy
# ---------------------------------------------------------------------------


def should_respawn(entry: ChildRegistryEntry, final_status: str) -> tuple[bool, str]:
    """Deterministic restart-policy table for a slot whose session ended.

    ``final_status`` is the child session's terminal
    :data:`~.types.StatusLabel`. Returns ``(decision, reason)`` where the
    reason token lands in the child-lifecycle audit event:

    * non-terminal status → ``(False, "not_terminal")`` (caller bug guard)
    * ``completed`` → ``(False, "completed_no_respawn")``
    * policy ``never`` → ``(False, "policy_never")``
    * respawn budget exhausted → ``(False, "max_respawns_reached")``
    * otherwise (``failed`` / ``terminated``) → ``(True, "respawn")``

    The pool and fleet-cap checks still apply at respawn time — a ``True``
    here is a policy decision, not an admission.
    """
    if final_status not in ("completed", "terminated", "failed"):
        return (False, "not_terminal")
    if final_status == "completed":
        return (False, "completed_no_respawn")
    if entry["restart_policy"] == "never":
        return (False, "policy_never")
    if entry["respawn_count"] >= entry["max_respawns"]:
        return (False, "max_respawns_reached")
    return (True, "respawn")


# ---------------------------------------------------------------------------
# Spawn admission
# ---------------------------------------------------------------------------


def _validate_child_budget(budget: Any) -> BudgetControls:
    """Validate a child budget: both caps required, strictly positive.

    Children deliberately reject the ``-1`` uncapped sentinel Mission
    budgets accept: a supervised worker must be self-terminating on both
    axes even if its supervisor dies, and the iteration cap doubles as
    the slot's pool reservation, which must be a finite number.
    """
    if not isinstance(budget, dict):
        raise _reject("budget", "not_a_dict")
    max_iterations = budget.get("max_iterations")
    if not _is_positive_int(max_iterations):
        raise _reject("budget", "missing_or_not_positive_int", subfield="max_iterations")
    max_wall = budget.get("max_wall_clock_seconds")
    if not _is_positive_int(max_wall):
        raise _reject("budget", "missing_or_not_positive_int", subfield="max_wall_clock_seconds")
    normalized: dict[str, Any] = {
        "max_iterations": max_iterations,
        "max_wall_clock_seconds": max_wall,
    }
    return cast("BudgetControls", normalized)


def validate_spawn(
    *,
    parent_role: str | None,
    config: SwarmConfig,
    children: Sequence[ChildRegistryEntry],
    request: Mapping[str, Any],
    registered_tools: dict[str, Any],
    registered_tags: Mapping[str, Collection[str]],
    sibling_allowlists: Mapping[str, Sequence[str]],
    flag_lookup: dict[str, str] | None = None,
    respawn_of_slot: str | None = None,
) -> SpawnSpec:
    """Run the full spawn-admission pipeline; first failure wins.

    Pure: the caller supplies every piece of live state — the parent's
    role, the persisted registry, the registered tool names and tag map,
    and the allowlists of **live** sibling children keyed by slot (the
    overlap rail checks only siblings that can still act).

    Admission order (each rejection carries its own ``details.reason``):

    1. depth — the dispatching session must be an orchestrator
    2. slot shape and uniqueness (``respawn_of_slot`` exempts its own slot)
    3. child budget shape (strictly positive; ``-1`` rejected)
    4. restart policy and ``max_respawns``
    5. ``use_sampling`` shape
    6. fleet cap over live (non-settled) slots
    7. iteration-pool balance
    8. directive, criteria, cadence via the shared Mission validators
    9. allowlist resolution (control/supervisor/swarm names unreachable)
    10. mutating-tool overlap against live siblings (unless opted out)

    Returns the normalized :class:`SpawnSpec`.
    """
    # 1. Depth: only orchestrators spawn. Children never receive the
    # supervisor tools in the first place (structural guard); this role
    # check is the second, independent layer of the same rule.
    if parent_role != "orchestrator":
        raise _reject(
            "spawn",
            "spawn_depth_exceeded",
            parent_role=parent_role,
        )

    # 2. Slot.
    slot = request.get("slot")
    if not isinstance(slot, str) or not _SLOT_RE.match(slot):
        raise _reject("spawn", "slot_missing_or_invalid")
    existing_slots = {entry["slot"] for entry in children}
    if slot in existing_slots and slot != respawn_of_slot:
        raise _reject("spawn", "duplicate_slot", slot=slot)

    # 3. Child budget (also the pool reservation).
    budget = _validate_child_budget(request.get("budget"))

    # 4. Restart policy.
    restart_policy = request.get("restart_policy", "never")
    if restart_policy not in RESTART_POLICIES:
        raise _reject("spawn", "restart_policy_invalid", restart_policy=restart_policy)
    max_respawns = request.get("max_respawns")
    if max_respawns is None:
        max_respawns = DEFAULT_RESPAWNS_BY_POLICY[restart_policy]
    elif not _is_non_negative_int(max_respawns):
        raise _reject("spawn", "max_respawns_not_a_non_negative_int")
    if restart_policy == "never":
        max_respawns = 0

    # 5. Sampling default: deterministic leaves unless explicitly opted in.
    use_sampling = request.get("use_sampling", False)
    if not isinstance(use_sampling, bool):
        raise _reject("spawn", "use_sampling_not_a_bool")

    # 6. Fleet cap over live slots. A respawn follows settle, so its old
    # entry is no longer live and counts itself naturally.
    live = [entry for entry in children if not entry.get("settled")]
    if len(live) + 1 > config["max_children"]:
        raise _reject(
            "spawn",
            "fleet_cap_exceeded",
            max_children=config["max_children"],
            live_children=len(live),
        )

    # 7. Pool balance.
    balance = compute_pool_balance(config["child_iteration_pool"], children)
    if budget["max_iterations"] > balance["remaining"]:
        raise _reject(
            "spawn",
            "iteration_pool_exhausted",
            requested=budget["max_iterations"],
            remaining=balance["remaining"],
        )

    # 8. Directive / criteria / cadence via the shared validators.
    directive = validate_directive(cast("str", request.get("directive", "")))
    criteria = validate_criteria(cast("list[dict[str, Any]]", request.get("criteria")))
    cadence = validate_cadence(
        cast("dict[str, Any]", request.get("cadence") or {"kind": "every_iteration"})
    )

    # 9. Allowlist. An explicit list naming a control-plane tool is a
    # loud rejection (precise sampler feedback beats silent stripping);
    # the all-tools expansion excludes them via the control set.
    explicit = request.get("tool_allowlist")
    allow_all = bool(request.get("allow_all_tools", False))
    if isinstance(explicit, list):
        for name in explicit:
            if isinstance(name, str) and name in SWARM_EXCLUDED_TOOLS:
                raise _reject("tool_allowlist", "control_tool_not_allowed", tool_name=name)
    tool_allowlist = resolve_effective_allowlist(
        allow_all_tools=allow_all,
        explicit_allowlist=cast("list[str] | None", explicit),
        registered_tools=registered_tools,
        control_tools=SWARM_EXCLUDED_TOOLS,
        flag_lookup=flag_lookup,
    )

    # 10. Mutating-tool overlap against live siblings, slot-ordered so
    # the first rejection is deterministic.
    if not config["allow_overlapping_mutating_tools"]:
        mutating = {name for name in tool_allowlist if not _is_safe_tool(name, registered_tags)}
        if mutating:
            for sibling_slot in sorted(sibling_allowlists):
                if sibling_slot == respawn_of_slot:
                    continue
                sibling_mutating = {
                    name
                    for name in sibling_allowlists[sibling_slot]
                    if not _is_safe_tool(name, registered_tags)
                }
                overlap = sorted(mutating & sibling_mutating)
                if overlap:
                    raise _reject(
                        "spawn",
                        "mutating_tool_overlap",
                        tools=overlap,
                        sibling_slot=sibling_slot,
                    )

    spec: SpawnSpec = {
        "slot": slot,
        "directive": directive,
        "criteria": criteria,
        "budget": budget,
        "tool_allowlist": tool_allowlist,
        "checkpoint_cadence": cadence,
        "restart_policy": cast("RestartPolicy", restart_policy),
        "max_respawns": max_respawns,
        "use_sampling": use_sampling,
    }
    return spec


# ---------------------------------------------------------------------------
# Supervisor tool schemas
# ---------------------------------------------------------------------------

SUPERVISOR_TOOL_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "mission_spawn": {
        "type": "object",
        "properties": {
            "slot": {"type": "string", "description": "Unique slot name for the child."},
            "directive": {"type": "string", "description": "Child goal, natural language."},
            "criteria": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Mission criteria array for the child.",
            },
            "budget": {
                "type": "object",
                "properties": {
                    "max_iterations": {"type": "integer", "minimum": 1},
                    "max_wall_clock_seconds": {"type": "integer", "minimum": 1},
                },
                "required": ["max_iterations", "max_wall_clock_seconds"],
                "description": "Finite child budget; -1 is rejected on children.",
            },
            "tool_allowlist": {"type": "array", "items": {"type": "string"}},
            "allow_all_tools": {"type": "boolean"},
            "restart_policy": {
                "type": "string",
                "enum": ["never", "on_failure", "on_failure_with_revision"],
            },
            "max_respawns": {"type": "integer", "minimum": 0},
            "use_sampling": {"type": "boolean"},
            "cadence": {"type": "object"},
        },
        "required": ["slot", "directive", "criteria", "budget"],
    },
    "children_status": {"type": "object", "properties": {}},
    "child_abort": {
        "type": "object",
        "properties": {"slot": {"type": "string"}},
        "required": ["slot"],
    },
}
"""JSON schemas for the in-process supervisor tools.

The supervisor tools are never registered with FastMCP, so the sampled
strategy validator (``validate_strategy_against_catalog``) cannot learn
their shapes from the live registry. Callers that build a sampled
orchestrator extend the sampler's catalog with these schemas so spawn
proposals validate at proposal time; the spawn tool itself re-validates
every dispatch through :func:`validate_spawn` regardless.
"""

SUPERVISOR_TOOL_DOCSTRINGS: Final[dict[str, str]] = {
    "mission_spawn": (
        "Spawn one supervised child Mission session. Validated against the "
        "swarm rails (fleet cap, iteration pool, finite child budget, "
        "allowlist exclusions, mutating-tool overlap)."
    ),
    "children_status": (
        "Return the deterministic fleet snapshot: slot-ordered child rows "
        "plus aggregate children_* metrics and the remaining iteration pool."
    ),
    "child_abort": "Abort one live child slot by name, settling its reservation.",
}
"""Prompt-facing docstrings for the supervisor tools (allowlist rendering)."""


# ---------------------------------------------------------------------------
# Orchestrator session construction
# ---------------------------------------------------------------------------


def build_orchestrator_session(
    *,
    session_id: str,
    directive: str,
    criteria: list[Criterion],
    budget: BudgetControls,
    swarm_config: SwarmConfig,
    cadence: Cadence,
    extra_allowlist: Sequence[str] = (),
    stagnation_threshold: int = 3,
    use_sampling: bool = False,
    sampling_backend_resolved: str = "none",
    created_at: str,
) -> dict[str, Any]:
    """Assemble a new orchestrator session dict from validated inputs.

    Pure: every argument is already validated by its own validator; this
    function only fixes the shape shared by the MCP tool and the CLI so
    the two surfaces cannot drift. The effective allowlist brackets the
    caller's extras with the supervisor tools — ``children_status``
    first (the deterministic strategy's target), spawn/abort last —
    which is the only place those names may enter a session allowlist.
    """
    effective = ["children_status"]
    effective.extend(name for name in extra_allowlist if name not in SUPERVISOR_TOOLS)
    effective.extend(["mission_spawn", "child_abort"])
    return {
        "version": _schema_version(),
        "session_id": session_id,
        "directive_text": directive,
        "criteria": [
            {k: v for k, v in criterion.items() if not str(k).startswith("_")}
            for criterion in criteria
        ],
        "budget": budget,
        "tool_allowlist": effective,
        "checkpoint_cadence": cadence,
        "stagnation_threshold": stagnation_threshold,
        "use_sampling": use_sampling,
        "sampling_backend_resolved": sampling_backend_resolved,
        "allow_scripted_strategies": False,
        "status": "pending",
        "created_at": created_at,
        "iterations": [],
        "no_progress_counter": 0,
        "role": "orchestrator",
        "swarm": swarm_config,
        "children": [],
    }


def _schema_version() -> int:
    """Late import so this pure module keeps its type-only dependency."""
    from .types import SCHEMA_VERSION

    return int(SCHEMA_VERSION)
