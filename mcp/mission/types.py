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
    "max_cost",
    "no_progress",
    "user_abort",
]
"""The exhaustive set of reasons that pair with a :data:`VerdictLabel`."""

StatusLabel = Literal["pending", "running", "paused", "completed", "terminated", "failed"]
"""The lifecycle states of a :class:`SessionState`."""

CriterionKind = Literal["metric_threshold", "event", "predicate"]
"""The three Criterion evaluator kinds."""

SamplingStatus = Literal["used", "rejected", "fallback", "unavailable", "disabled"]
"""The terminal status of a single sampling attempt on an iteration."""

CadenceKind = Literal["every_iteration", "every_n_iterations", "every_t_seconds", "on_event"]
"""The four supported Checkpoint_Cadence kinds."""


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
    ``predicate``) are not declared on the base ``TypedDict`` because they
    are mutually exclusive per ``kind``. Validators in
    ``mcp.mission.validation`` verify the right keys are present for each
    ``kind`` and may attach a private cached AST under ``_parsed_ast`` for
    ``predicate`` entries.
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
    """The mandatory caps every Mission_Session declares at start time.

    ``max_cost_usd`` is required only when the session's ``tool_allowlist``
    contains a tool whose registered tag set includes ``cost-incurring``,
    ``data-upload``, ``image``, or ``infrastructure`` (per Risk_Tier table
    in ``mcp/README.md``). The validator enforces this conditionally.
    """

    max_iterations: int
    max_wall_clock_seconds: int
    max_cost_usd: NotRequired[float]


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
    cost_usd: NotRequired[float]


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
    # exception. Carries one of the budget-cap :data:`VerdictReason`
    # values (``max_wall_clock`` for duration / memory / runtime caps,
    # ``max_cost`` for the cost cap).
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
    sampling_backend_resolved: NotRequired[Literal["mcp", "bedrock", "none"]]
    bedrock_model_id: NotRequired[str]
    allow_scripted_strategies: bool
    sampling_model_preferences: NotRequired[dict[str, Any]]
    status: StatusLabel
    created_at: str
    started_at: NotRequired[str]
    ended_at: NotRequired[str]
    iterations: list[IterationRecord]
    no_progress_counter: int
    accumulated_cost_usd: float
    last_checkpoint_at: NotRequired[str]
    final_verdict: NotRequired[VerdictLabel]
    final_report_path: NotRequired[str]
