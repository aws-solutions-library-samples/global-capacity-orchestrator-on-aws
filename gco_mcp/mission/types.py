"""Domain types for the Mission goal-directed iteration loop.

All structured types live in one module so the engine, validators, sampler,
and tool wrappers share the same shape. ``TypedDict`` (not ``dataclass``) so
``json.dumps`` / ``json.loads`` round-trip without any custom serialization.
The ``version`` field on :class:`SessionState` is checked on every load
against :data:`mcp.mission.SCHEMA_VERSION` (re-exported here as
:data:`SCHEMA_VERSION` for callers that import only this module).

Optional keys use :data:`typing.NotRequired` so that ``mypy --strict`` accepts
absence on dict literals while still rejecting an unknown key.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

# Re-exported here so callers that import only ``mcp.mission.types`` can read
# the schema version without an extra import. The canonical value lives on
# the package ``__init__``.
from . import SCHEMA_VERSION as SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Literal type aliases
# ---------------------------------------------------------------------------

VerdictLabel = Literal["continue", "adjust", "complete", "terminate"]
"""The four possible Decide_Phase outputs.

``continue`` keeps the loop running with the current strategy. ``adjust``
runs another iteration with a Strategy_Revision. ``complete`` ends the
session as success. ``terminate`` ends the session as give-up.
"""

VerdictReason = Literal[
    "in_progress",
    "cadence_skip",
    "criteria_met",
    "forced_complete",
    "heuristic_unproductive",
    "max_iterations",
    "max_wall_clock",
    "no_progress",
    "user_abort",
]
"""The exhaustive set of reasons that pair with a :data:`VerdictLabel`."""

StatusLabel = Literal["pending", "running", "paused", "completed", "terminated", "failed"]
"""The lifecycle states of a :class:`SessionState`."""

CriterionKind = Literal[
    "metric_threshold", "event", "predicate", "tool_call_succeeded", "metric_trend"
]
"""The five Criterion evaluator kinds.

``metric_trend`` is the history-aware kind: rather than comparing a single
point-in-time value to a fixed target (``metric_threshold``), it evaluates the
direction of a metric across iterations using the cumulative metric history the
engine accumulates in :meth:`MissionEngine._build_cumulative_observation`.
"""

MetricTrendDirection = Literal["decreasing", "increasing", "non_increasing", "non_decreasing"]
"""The four trend directions a ``metric_trend`` criterion can require.

``decreasing`` / ``increasing`` require a strict net change across the window
(last < first / last > first); ``non_increasing`` / ``non_decreasing`` allow a
flat series (last <= first / last >= first).
"""

SamplingStatus = Literal["used", "rejected", "fallback", "unavailable", "disabled"]
"""The terminal status of a single sampling attempt on an iteration."""

CadenceKind = Literal["every_iteration", "every_n_iterations", "every_t_seconds", "on_event"]
"""The four supported Checkpoint_Cadence kinds."""

SessionRole = Literal["orchestrator", "child"]
"""The two swarm roles a session can carry.

A session with no ``role`` field is a standalone session — every session
that predates swarm supervision, with behavior identical to before the
field existed. ``orchestrator`` sessions hold a :class:`SwarmConfig` and a
child registry and are the only sessions whose engine receives the
in-process supervisor tools. ``child`` sessions carry
``parent_session_id`` and are otherwise ordinary sessions.
"""

RestartPolicy = Literal["never", "on_failure", "on_failure_with_revision"]
"""The supervision policy fixed on a child slot at spawn time.

``never`` — one shot; the slot is done when its session ends. ``on_failure``
— a child that ends ``failed`` or ``terminated`` without meeting its
criteria is respawned with the same directive, up to ``max_respawns``.
``on_failure_with_revision`` — same, except the replacement directive may be
revised from the failed child's Final_Report lessons (advisory sampling;
falls back to the verbatim directive). The respawn *decision* is always
deterministic policy evaluation — never a sampler output.
"""


# ---------------------------------------------------------------------------
# Terminal-state sets
# ---------------------------------------------------------------------------

TERMINAL_STATES: frozenset[StatusLabel] = frozenset({"completed", "terminated", "failed"})
"""The :data:`StatusLabel` values from which a session cannot transition.

A session in any of these states refuses further ``mission_iterate`` calls
with ``session_terminal``. The engine consults this set on every iteration
entry to short-circuit before performing any work.
"""

TERMINAL_VERDICTS: frozenset[VerdictLabel] = frozenset({"complete", "terminate"})
"""The :data:`VerdictLabel` values that end a session.

When the Decide_Phase emits a verdict in this set, the engine writes a
Final_Report and transitions the session to ``completed`` or ``terminated``
(matching the verdict).
"""


# ---------------------------------------------------------------------------
# Criterion and CriterionResult
# ---------------------------------------------------------------------------


class Criterion(TypedDict):
    """A single machine-checkable success condition.

    The kind-specific keys (``metric``/``op``/``target`` for
    ``metric_threshold``, ``event_name`` for ``event``, ``expression`` for
    ``predicate``, ``tool_name``/``min_count`` for ``tool_call_succeeded``,
    ``metric``/``direction``/``window``/``min_points`` for ``metric_trend``)
    are not declared on the base ``TypedDict`` because they are mutually
    exclusive per ``kind``. Validators in ``mcp.mission.validation`` verify
    the right keys are present for each ``kind`` and may attach a private
    cached AST under ``_parsed_ast`` for ``predicate`` entries.
    """

    criterion_id: str
    kind: CriterionKind
    required: bool
    # Kind-specific keys (validator-enforced):
    metric: NotRequired[str]
    op: NotRequired[Literal["<", "<=", ">", ">=", "==", "!="]]
    target: NotRequired[float]
    event_name: NotRequired[str]
    expression: NotRequired[str]
    tool_name: NotRequired[str]
    min_count: NotRequired[int]
    # metric_trend keys: ``direction`` is required for the kind; ``window``
    # bounds how many of the most-recent points are considered (default: all
    # available); ``min_points`` is the minimum number of numeric points
    # required before the criterion decides met/unmet rather than inconclusive.
    direction: NotRequired[MetricTrendDirection]
    window: NotRequired[int]
    min_points: NotRequired[int]
    # Cached parsed AST attached by ``validate_criteria`` for predicate entries.
    _parsed_ast: NotRequired[Any]


class CriterionResult(TypedDict):
    """The outcome of evaluating one :class:`Criterion` at a checkpoint."""

    criterion_id: str
    status: Literal["met", "unmet", "inconclusive"]
    evidence: Any
    evaluated_at: str  # ISO 8601 UTC


# ---------------------------------------------------------------------------
# Budget controls and cadence
# ---------------------------------------------------------------------------


class BudgetControls(TypedDict):
    """Loop-control caps every Mission_Session declares at start time.

    These are **loop-control** caps — not financial budgets. Mission
    enforces only the caps the loop has direct visibility into:
    iteration count and wall-clock seconds. Cost guardrails live
    out-of-band; configure AWS Budgets and Cost Anomaly Detection at
    the account level for those.

    Both ``max_iterations`` and ``max_wall_clock_seconds`` accept
    either a strictly-positive integer cap or the explicit sentinel
    ``-1`` to opt out of that axis. The validator rejects every other
    shape (zero, other negatives, non-integer types, missing keys),
    and additionally rejects both caps being ``-1`` simultaneously
    (with ``reason="at_least_one_cap_required"``) since that would
    leave the loop with no axis-driven termination — a runaway-loop
    config error.
    """

    max_iterations: int
    max_wall_clock_seconds: int


class Cadence(TypedDict):
    """The Checkpoint_Cadence configuration on a session.

    ``n`` is required for ``every_n_iterations``. ``t`` is required for
    ``every_t_seconds``. ``event_name`` is required for ``on_event``. The
    base ``every_iteration`` requires no extra keys.
    """

    kind: CadenceKind
    n: NotRequired[int]
    t: NotRequired[int]
    event_name: NotRequired[str]


# ---------------------------------------------------------------------------
# Swarm supervision
# ---------------------------------------------------------------------------


class SwarmConfig(TypedDict):
    """Swarm-level rails persisted on an orchestrator session.

    These are **loop-control** rails in the same sense as
    :class:`BudgetControls`: they cap what the supervisor can directly
    observe (fleet size, pooled child iterations, concurrency), never
    money. Cost guardrails live out-of-band (AWS Budgets / Cost Anomaly
    Detection), exactly as documented for Mission budgets.

    The validator normalizes defaults, so a persisted config always
    carries all four keys. ``max_children`` bounds the number of live
    (non-settled) child slots. ``child_iteration_pool`` is the pooled
    iteration budget every spawn reserves from — child budgets reject
    the ``-1`` uncapped sentinel, so the pool is always meaningful.
    ``max_concurrent_children`` bounds how many children advance
    simultaneously. ``allow_overlapping_mutating_tools`` opts out of the
    reject-by-default rule against two live children sharing a
    non-``safe``-tagged tool.
    """

    max_children: int
    child_iteration_pool: int
    max_concurrent_children: int
    allow_overlapping_mutating_tools: bool


class ChildRegistryEntry(TypedDict):
    """One supervised slot in an orchestrator session's child registry.

    A **slot** is the stable supervision identity; the ``session_id`` it
    points at changes on respawn (lineage is kept under
    ``prior_session_ids``). Pool accounting reads two fields:
    ``reserved_iterations`` counts against the pool while the entry is
    live, and ``consumed_iterations`` accumulates the actually-recorded
    iterations of settled (terminal) sessions. ``settled`` marks that the
    current session's consumption has been folded into
    ``consumed_iterations`` — the settle step is what refunds the unused
    remainder of a reservation back to the pool.
    """

    slot: str
    session_id: str
    spawned_at: str  # ISO 8601 UTC
    reserved_iterations: int
    restart_policy: RestartPolicy
    max_respawns: int
    respawn_count: int
    consumed_iterations: int
    settled: NotRequired[bool]
    prior_session_ids: NotRequired[list[str]]


# ---------------------------------------------------------------------------
# Tool calls and strategy
# ---------------------------------------------------------------------------


class ToolCallRecord(TypedDict):
    """A single tool invocation recorded during an Iteration's Execute_Phase.

    Used both for direct ``tool_calls`` strategies and for in-script calls
    captured by the Mission_Sandbox under ``IterationRecord.script_call_log``.
    """

    tool_name: str
    args: dict[str, Any]
    status: Literal["ok", "failed", "skipped_not_allowed"]
    result_summary: Any
    duration_ms: int
    error_message: NotRequired[str]


class Strategy(TypedDict, total=False):
    """The Propose_Phase output. Carries one of ``tool_calls`` or ``script``.

    ``total=False`` because every key is optional in isolation; the
    ``validate_strategy`` validator enforces the mutual-exclusivity rule
    (exactly one of ``tool_calls`` or ``script`` must be present and
    non-empty).
    """

    tool_calls: list[dict[str, Any]]
    script: str
    expected_observation_keys: list[str]
    rationale: str


# ---------------------------------------------------------------------------
# Observation, Phase, Iteration, Session
# ---------------------------------------------------------------------------


class Observation(TypedDict):
    """The Observe_Phase output — a normalized view of Execute_Phase results."""

    tool_results: list[Any]
    metrics: dict[str, Any]
    events: list[dict[str, Any]]
    errors: NotRequired[list[dict[str, Any]]]
    # Cumulative, history-aware view of every numeric metric seen across the
    # session, keyed by metric name and ordered oldest→newest. Present only on
    # the *cumulative* observation the Evaluate_Phase builds (see
    # :meth:`MissionEngine._build_cumulative_observation`); the per-iteration
    # Observation written to ``record["observation"]`` keeps ``metrics``
    # strictly point-in-time and does not carry this key. Consumed by the
    # ``metric_trend`` criterion and available to predicates.
    metric_history: NotRequired[dict[str, list[float]]]
    # Present only on orchestrator sessions: the deterministic, slot-ordered
    # snapshot of supervised child states merged by the swarm observation
    # augmenter at the end of the Observe_Phase. Standalone and child
    # sessions never carry this key. Predicates read it via
    # ``obs['children']``; the paired aggregate counts land as ordinary
    # numeric metrics under ``metrics`` (``children_completed``, ...).
    children: NotRequired[list[dict[str, Any]]]
    phase_started_at: str
    phase_ended_at: str


class PhaseRecord(TypedDict):
    """One row in :attr:`IterationRecord.phases`. One per phase regardless of outcome."""

    phase: Literal["propose", "execute", "observe", "evaluate", "decide"]
    status: Literal["succeeded", "failed"]
    started_at: str
    ended_at: str
    error_message: NotRequired[str]


class IterationRecord(TypedDict):
    """The complete record of one pass through the five-phase cycle.

    Sampling-related fields (``sampling_status``, ``sampling_output``,
    ``sampling_rejection_reason``) are present only when the iteration
    triggered an advisory-path sampling call. ``script_call_log`` is
    present only when the strategy carried a ``script``.
    """

    iteration_index: int
    started_at: str
    ended_at: str
    phases: list[PhaseRecord]
    strategy: Strategy
    observation: Observation
    criteria_evaluation: list[CriterionResult]
    verdict: VerdictLabel
    verdict_reason: VerdictReason
    revision_rationale: NotRequired[str]
    checkpoint_evaluated: bool
    sampling_status: NotRequired[SamplingStatus]
    sampling_output: NotRequired[str]
    sampling_rejection_reason: NotRequired[str]
    script_call_log: NotRequired[list[ToolCallRecord]]
    # Set by ``_execute_script`` when the sandbox runner raises
    # :class:`mcp.mission.sandbox.SandboxTerminated`. The Decide_Phase's
    # cascade reads this sentinel before any other branch and emits
    # ``("terminate", <reason>)`` so a sandbox cap propagates up to the
    # budget-cap path rather than failing the iteration as a phase
    # exception. Carries the wall-clock :data:`VerdictReason`
    # ``max_wall_clock`` for duration / memory / runtime caps.
    sandbox_terminated_reason: NotRequired[VerdictReason]


class SessionState(TypedDict):
    """The durable Mission_Session payload persisted by Mission_State_Backend.

    The ``version`` field carries :data:`SCHEMA_VERSION`; loaders compare it
    against the current value and reject mismatches. Optional fields are
    populated as the session progresses (``started_at`` on first iteration,
    ``ended_at`` and ``final_report_path`` on terminal verdict, etc.).
    """

    version: int
    session_id: str
    directive_text: str
    criteria: list[Criterion]
    budget: BudgetControls
    tool_allowlist: list[str]
    checkpoint_cadence: Cadence
    stagnation_threshold: int
    use_sampling: bool
    sampling_backend_resolved: NotRequired[Literal["bedrock", "none"]]
    bedrock_model_id: NotRequired[str]
    allow_scripted_strategies: bool
    status: StatusLabel
    created_at: str
    started_at: NotRequired[str]
    ended_at: NotRequired[str]
    iterations: list[IterationRecord]
    no_progress_counter: int
    last_checkpoint_at: NotRequired[str]
    final_verdict: NotRequired[VerdictLabel]
    final_report_path: NotRequired[str]
    # Swarm supervision fields. All NotRequired so pre-swarm session files
    # load unchanged (loaders reject only on ``version`` mismatch, and the
    # schema version is deliberately NOT bumped for these additive keys).
    # ``role`` absent means standalone. ``parent_session_id`` is set on
    # child sessions only; ``swarm`` and ``children`` on orchestrators only.
    role: NotRequired[SessionRole]
    parent_session_id: NotRequired[str]
    swarm: NotRequired[SwarmConfig]
    children: NotRequired[list[ChildRegistryEntry]]
